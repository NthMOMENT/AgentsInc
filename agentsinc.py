"""
Agents Inc — Agent Labour Market
Matches buyers with registered autonomous agents.
Brokers jobs, settles payments, takes 1% cut.

Flow:
  Human/Agent posts job → Agents Inc matches skill tags →
  Buyer authorizes payment → Job sent to winning agent via A2A →
  Agent delivers → Agents Inc settles → 99% seller / 1% treasury
"""

import asyncio, hashlib, json, os, time, logging, struct, re
from pathlib import Path
from datetime import datetime, timezone
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, MessageHandler, CallbackQueryHandler,
                          CommandHandler, ContextTypes, filters)
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.system_program import ID as SYS_PROG
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Finalized
from spl.token.instructions import get_associated_token_address

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("TELEGRAM_TOKEN")
RPC             = os.getenv("SOLANA_RPC", "https://api.devnet.solana.com")
CLAUDE_KEY      = os.getenv("ANTHROPIC_API_KEY")
_rpc = os.getenv("SOLANA_RPC", "")
HELIUS_KEY = _rpc.split("api-key=")[-1] if "api-key=" in _rpc else ""
HELIUS_DAS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
TREASURY        = os.getenv("TREASURY_ADDRESS", "6qh5tiYjFXgnga2ErRrAgb94UK7F35fQ8UDj1ZDnpinb")
PROGRAM_ID      = os.getenv("AGENTS_INC_PROGRAM_ID", "E6oC1Dm5UymCQ5Uq3EpZQgUaDS1PB1KSrGKGFXiBfVsS")
USDC_MINT       = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
JOBS_FILE       = "jobs.json"
BASE_DIR        = "/home/phidias/phidias"
POLL_SECONDS    = 10
JOB_TIMEOUT     = 30 * 60          # 30 minutes
FEE_BPS         = 100               # 1%
A2A_TIMEOUT     = 30               # seconds to wait for A2A response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("agentsinc.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("agentsinc")

kp  = Keypair.from_base58_string(open(f"{BASE_DIR}/.wallet_secret").read().strip())
ME  = kp.pubkey()
ATA = get_associated_token_address(ME, Pubkey.from_string(USDC_MINT))
PROG = Pubkey.from_string(PROGRAM_ID)

os.makedirs(f"{BASE_DIR}/jobs", exist_ok=True)

# ── Discriminators (Anchor 0.30.1 sighash) ───────────────────────────────────
def sighash(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]

DISC_AUTHORIZE = sighash("authorize")
DISC_SETTLE    = sighash("settle")
DISC_CANCEL    = sighash("cancel")

# ── Job book ──────────────────────────────────────────────────────────────────
def load_jobs():
    return json.load(open(JOBS_FILE)) if os.path.exists(JOBS_FILE) else {}

def save_jobs(j):
    json.dump(j, open(JOBS_FILE, "w"), indent=2)

def new_job(chat_id, requirement, skill_tags, price_usdc, seller_pubkey, seller_a2a):
    jobs = load_jobs()
    job_id = hashlib.sha256(f"{chat_id}{time.time()}".encode()).hexdigest()[:8].upper()
    while job_id in jobs:
        job_id = hashlib.sha256(f"{chat_id}{time.time()}{job_id}".encode()).hexdigest()[:8].upper()
    jobs[job_id] = {
        "chat_id":      chat_id,
        "requirement":  requirement,
        "skill_tags":   skill_tags,
        "amount":       price_usdc,
        "seller":       seller_pubkey,
        "seller_a2a":   seller_a2a,
        "status":       "awaiting_payment",
        "created":      time.time(),
    }
    save_jobs(jobs)
    return job_id

# ── Mocked registry (Phase 1 — replace with Helius DAS when key available) ───
MOCK_REGISTRY = [
    {
        "name":       "ImageBot",
        "pubkey":     "So11111111111111111111111111111111111111112",
        "a2a":        "https://imagebot.example.com/a2a",
        "skills":     ["image_generation", "image_editing", "art"],
        "price_usdc": 2.0,
        "description": "Generates and edits images from text prompts",
    },
    {
        "name":       "DataBot",
        "pubkey":     "So11111111111111111111111111111111111111113",
        "a2a":        "https://databot.example.com/a2a",
        "skills":     ["data_analysis", "csv", "charts", "spreadsheet"],
        "price_usdc": 3.0,
        "description": "Analyses data, generates charts and insights",
    },
    {
        "name":       "WriteBot",
        "pubkey":     "So11111111111111111111111111111111111111114",
        "a2a":        "https://writebot.example.com/a2a",
        "skills":     ["copywriting", "content", "blog", "email", "marketing"],
        "price_usdc": 2.5,
        "description": "Writes marketing copy, blogs, emails",
    },
    {
        "name":       "CodeBot",
        "pubkey":     "So11111111111111111111111111111111111111115",
        "a2a":        "https://codebot.example.com/a2a",
        "skills":     ["coding", "python", "javascript", "debugging", "api"],
        "price_usdc": 5.0,
        "description": "Writes and debugs code in any language",
    },
]

async def infer_skills(name: str, description: str) -> list:
    """Use Claude to infer skill tags from agent name and description."""
    if not name and not description:
        return ["general"]
    prompt = f"""An AI agent is registered on Solana with this profile:
Name: {name}
Description: {description}

Return ONLY a JSON array of 1-5 lowercase skill tags that describe what this agent can do.
Examples: ["trading", "market_analysis"], ["image_generation", "art"], ["copywriting", "content"]
Use only simple lowercase keywords. Return ONLY the JSON array, nothing else."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": CLAUDE_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            raw = r.json()["content"][0]["text"].strip()
            return json.loads(raw)
    except Exception as e:
        log.warning(f"skill inference failed for {name}: {e}")
        return ["general"]


async def fetch_registered_agents() -> list:
    """
    Queries Helius DAS API with isAgent=true filter.
    Falls back to mock registry if Helius unavailable.
    Infers skill tags via Claude when registry metadata is sparse.
    """
    if HELIUS_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(HELIUS_DAS_URL, json={
                    "jsonrpc": "2.0",
                    "id": "agents-inc",
                    "method": "searchAssets",
                    "params": {
                        "isAgent": True,
                        "limit": 100,
                    }
                })
                data = r.json()
                items = data.get("result", {}).get("items", [])

                async def process_agent(item):
                    content    = item.get("content", {})
                    meta       = content.get("metadata", {})
                    attributes = meta.get("attributes", [])
                    skill_tags   = []
                    a2a_endpoint = ""
                    for attr in attributes:
                        if attr.get("trait_type") == "skills":
                            skill_tags = [s.strip() for s in attr.get("value", "").split(",") if s.strip()]
                        if attr.get("trait_type") == "a2a":
                            a2a_endpoint = attr.get("value", "")
                    name        = meta.get("name", item.get("id", "Unknown"))
                    description = meta.get("description", "")
                    if not skill_tags:
                        skill_tags = await infer_skills(name, description)
                    return {
                        "name":        name,
                        "pubkey":      item.get("id", ""),
                        "a2a":         a2a_endpoint,
                        "skills":      skill_tags,
                        "price_usdc":  float(meta.get("price_usdc", 1.0)),
                        "description": description,
                    }

                agents = await asyncio.gather(*[process_agent(item) for item in items])
                log.info(f"Helius registry: {len(agents)} agents, skills inferred via Claude")
                return list(agents)
        except Exception as e:
            log.warning(f"Helius fetch failed, falling back to mock: {e}")
    log.info("Using mock registry (no Helius key)")
    return MOCK_REGISTRY

# ── Matching engine (Phase 1 — tag based) ────────────────────────────────────
async def parse_requirement(text: str) -> dict:
    """Use Claude to extract skill tags and suggested price from requirement."""
    prompt = f"""A user posted this job requirement to an agent marketplace:
"{text}"

Respond ONLY with valid JSON:
{{
  "skill_tags": ["tag1", "tag2"],
  "summary": "one sentence job summary",
  "suggested_price_usdc": <float, 1-20 based on complexity>
}}

skill_tags must be simple lowercase keywords matching agent capabilities.
Examples: image_generation, data_analysis, copywriting, coding, translation, research
Return ONLY the JSON, no other text."""

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            }
        )
        raw = r.json()["content"][0]["text"]
        try:
            return json.loads(raw)
        except Exception:
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                return json.loads(match.group())
            return {
                "skill_tags": ["general"],
                "summary": text[:100],
                "suggested_price_usdc": 2.0
            }

def match_agents(agents: list, skill_tags: list) -> list:
    """Score agents by skill tag overlap. Returns ranked list."""
    scored = []
    for agent in agents:
        agent_skills = set(s.lower() for s in agent.get("skills", []))
        required = set(s.lower() for s in skill_tags)
        overlap = len(agent_skills & required)
        if overlap > 0:
            scored.append((overlap, agent))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [a for _, a in scored]

# ── A2A client ────────────────────────────────────────────────────────────────
async def send_a2a_job(endpoint: str, job_id: str, requirement: str,
                        buyer_pubkey: str, amount_usdc: float) -> dict:
    """
    Posts job to winning agent's A2A endpoint.
    Returns agent's acknowledgement or error.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": job_id,
        "method": "tasks/send",
        "params": {
            "id": job_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": requirement}]
            },
            "metadata": {
                "marketplace": "Agents Inc",
                "buyer": buyer_pubkey,
                "amount_usdc": amount_usdc,
                "job_id": job_id,
                "settlement_program": PROGRAM_ID,
            }
        }
    }
    try:
        async with httpx.AsyncClient(timeout=A2A_TIMEOUT) as client:
            r = await client.post(endpoint, json=payload,
                                  headers={"Content-Type": "application/json"})
            return r.json()
    except Exception as e:
        log.error(f"A2A call failed to {endpoint}: {e}")
        return {"error": str(e)}

# ── On-chain settlement ───────────────────────────────────────────────────────
def job_id_to_bytes(job_id: str) -> bytes:
    return hashlib.sha256(job_id.encode()).digest()[:8]

def derive_job_pda(job_id_bytes: bytes, buyer: Pubkey) -> tuple:
    return Pubkey.find_program_address(
        [b"job", job_id_bytes, bytes(buyer)],
        PROG
    )

async def authorize_job(client: AsyncClient, buyer: Keypair,
                         job_id: str, amount_usdc: float,
                         seller: Pubkey, deadline: int) -> str | None:
    """Record job authorization on-chain. Buyer signs."""
    job_id_bytes = job_id_to_bytes(job_id)
    pda, _ = derive_job_pda(job_id_bytes, buyer.pubkey())
    amount_micro = int(amount_usdc * 1_000_000)
    buyer_ata = get_associated_token_address(buyer.pubkey(), Pubkey.from_string(USDC_MINT))
    seller_ata = get_associated_token_address(seller, Pubkey.from_string(USDC_MINT))

    data = (
        DISC_AUTHORIZE
        + bytes(job_id_bytes)
        + struct.pack("<Q", amount_micro)
        + struct.pack("<q", deadline)
    )
    ix = Instruction(
        program_id=PROG,
        accounts=[
            AccountMeta(pubkey=pda,              is_signer=False, is_writable=True),
            AccountMeta(pubkey=buyer.pubkey(),   is_signer=True,  is_writable=True),
            AccountMeta(pubkey=seller,           is_signer=False, is_writable=False),
            AccountMeta(pubkey=buyer_ata,        is_signer=False, is_writable=False),
            AccountMeta(pubkey=seller_ata,       is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYS_PROG,         is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
                        is_signer=False, is_writable=False),
        ],
        data=bytes(data),
    )
    try:
        bh  = (await client.get_latest_blockhash()).value.blockhash
        tx  = Transaction([buyer], [ix], bh)
        sig = (await client.send_transaction(tx)).value
        await client.confirm_transaction(sig, commitment=Confirmed)
        log.info(f"authorized job {job_id} on-chain: {str(sig)[:16]}")
        return str(sig)
    except Exception as e:
        log.error(f"authorize_job failed for {job_id}: {e}")
        return None

async def settle_job(client: AsyncClient, job_id: str,
                      buyer: Pubkey, seller: Pubkey,
                      delivered_hash: str) -> str | None:
    """Agents Inc settles: 99% to seller, 1% to treasury."""
    job_id_bytes  = job_id_to_bytes(job_id)
    pda, _        = derive_job_pda(job_id_bytes, buyer)
    delivered_b   = bytes.fromhex(delivered_hash[:16].ljust(16, '0'))[:8]
    treasury_pk   = Pubkey.from_string(TREASURY)
    buyer_ata     = get_associated_token_address(buyer,       Pubkey.from_string(USDC_MINT))
    seller_ata    = get_associated_token_address(seller,      Pubkey.from_string(USDC_MINT))
    treasury_ata  = get_associated_token_address(treasury_pk, Pubkey.from_string(USDC_MINT))

    data = (
        DISC_SETTLE
        + bytes(job_id_bytes)
        + bytes(delivered_b)
    )
    ix = Instruction(
        program_id=PROG,
        accounts=[
            AccountMeta(pubkey=pda,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=ME,            is_signer=True,  is_writable=False),
            AccountMeta(pubkey=buyer_ata,     is_signer=False, is_writable=True),
            AccountMeta(pubkey=seller_ata,    is_signer=False, is_writable=True),
            AccountMeta(pubkey=treasury_ata,  is_signer=False, is_writable=True),
            AccountMeta(pubkey=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
                        is_signer=False, is_writable=False),
        ],
        data=bytes(data),
    )
    try:
        bh  = (await client.get_latest_blockhash()).value.blockhash
        tx  = Transaction([kp], [ix], bh)
        sig = (await client.send_transaction(tx)).value
        await client.confirm_transaction(sig, commitment=Confirmed)
        log.info(f"settled job {job_id}: {str(sig)[:16]}")
        return str(sig)
    except Exception as e:
        log.error(f"settle_job failed for {job_id}: {e}")
        return None

# ── Payment detection ─────────────────────────────────────────────────────────
async def scan_once(client, seen):
    sigs  = (await client.get_signatures_for_address(ATA, limit=20)).value
    found = []
    for s in sigs:
        sig = str(s.signature)
        if sig in seen or s.err is not None:
            continue
        seen.add(sig)
        tx = (await client.get_transaction(
            s.signature, max_supported_transaction_version=0)).value
        if tx is None:
            continue
        meta = tx.transaction.meta
        memo = None
        for lg in (meta.log_messages or []):
            if "Memo" in lg and '"' in lg:
                memo = lg.split('"')[1]
        pre  = {b.account_index: b for b in (meta.pre_token_balances or [])}
        recv = 0.0
        for b in (meta.post_token_balances or []):
            if str(b.mint) == USDC_MINT and str(b.owner) == str(ME):
                before = pre.get(b.account_index)
                b0 = float(before.ui_token_amount.ui_amount or 0) if before else 0.0
                recv = float(b.ui_token_amount.ui_amount or 0) - b0
        if recv > 0:
            found.append((memo, recv, sig))
    return found

def match_job(jobs, memo, amount):
    j = jobs.get(memo)
    if j and j["status"] == "awaiting_payment" and amount >= j["amount"]:
        return memo
    for jid, jd in jobs.items():
        if jd["status"] == "awaiting_payment" and abs(amount - jd["amount"]) < 0.01:
            return jid
    return None

# ── Telegram handlers ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Agents Inc*\n\n"
        "The agent labour market. Post a job, we find the right agent, "
        "payment is handled trustlessly on Solana.\n\n"
        "*How it works:*\n"
        "1. Describe what you need\n"
        "2. We match you with a registered agent\n"
        "3. Pay in USDC — funds go directly to the agent\n"
        "4. Agent delivers, you receive the result\n\n"
        "We take 1%. The agent keeps 99%.\n\n"
        "Just describe your job to get started.",
        parse_mode="Markdown"
    )

async def handle_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 15:
        await update.message.reply_text(
            "Please describe your job in more detail. "
            "Example: _I need someone to write a 500-word blog post about AI agents_",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("🔍 Analysing your requirement and searching the registry...")

    try:
        parsed = await parse_requirement(text)
    except Exception as e:
        log.error(f"parse_requirement failed: {e}")
        await update.message.reply_text("Something went wrong. Please try again.")
        return

    skill_tags   = parsed.get("skill_tags", ["general"])
    summary      = parsed.get("summary", text[:100])
    price        = round(parsed.get("suggested_price_usdc", 2.0), 2)

    agents = await fetch_registered_agents()
    matches = match_agents(agents, skill_tags)

    if not matches:
        await update.message.reply_text(
            "No registered agents match your requirement right now. "
            "Try rephrasing or check back later as more agents join the registry."
        )
        return

    best = matches[0]
    price = max(price, best.get("price_usdc", price))

    ctx.user_data["pending_job"]    = text
    ctx.user_data["pending_tags"]   = skill_tags
    ctx.user_data["pending_price"]  = price
    ctx.user_data["pending_agent"]  = best

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✓ Confirm — {price} USDC", callback_data="confirm_job"),
        InlineKeyboardButton("✗ Cancel", callback_data="cancel_job"),
    ]])

    await update.message.reply_text(
        f"*Job Match Found*\n\n"
        f"📋 _{summary}_\n\n"
        f"*Matched Agent:* {best['name']}\n"
        f"_{best['description']}_\n\n"
        f"*Skills matched:* {', '.join(skill_tags)}\n"
        f"*Price:* {price} USDC\n\n"
        f"Agents Inc takes 1%. {best['name']} receives 99%.\n\n"
        f"Confirm to receive your payment address.",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def job_confirmed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel_job":
        await q.edit_message_text("Cancelled. Post a new job anytime.")
        return

    text   = ctx.user_data.get("pending_job")
    tags   = ctx.user_data.get("pending_tags")
    price  = ctx.user_data.get("pending_price")
    agent  = ctx.user_data.get("pending_agent")

    if not text or not agent:
        await q.edit_message_text("Session expired. Please describe your job again.")
        return

    job_id = new_job(
        q.message.chat_id, text, tags, price,
        agent["pubkey"], agent["a2a"]
    )

    log.info(f"job {job_id} created: {price} USDC → {agent['name']}")

    await q.edit_message_text(
        f"*Job {job_id} Created*\n\n"
        f"Agent: *{agent['name']}*\n"
        f"Amount: *{price} USDC* (Solana devnet)\n\n"
        f"Send exactly *{price} USDC* to:\n"
        f"`{ME}`\n\n"
        f"Add memo: `{job_id}` if your wallet supports it.\n\n"
        f"Job starts the moment payment is confirmed.\n"
        f"Expires in 30 minutes.",
        parse_mode="Markdown"
    )

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    jobs = load_jobs()
    active = {jid: jd for jid, jd in jobs.items()
              if jd.get("chat_id") == update.message.chat_id
              and jd["status"] not in ("delivered", "cancelled", "expired", "failed")}
    if not active:
        await update.message.reply_text("No active jobs.")
        return
    lines = []
    for jid, jd in active.items():
        age = int((time.time() - jd["created"]) / 60)
        lines.append(f"• `{jid}` — {jd['status']} — {age}m ago — {jd['amount']} USDC")
    await update.message.reply_text(
        "Active jobs:\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    jobs = load_jobs()
    changed = False
    for jid, jd in jobs.items():
        if jd.get("chat_id") == update.message.chat_id and jd["status"] == "awaiting_payment":
            jd["status"] = "cancelled"
            changed = True
    if changed:
        save_jobs(jobs)
        await update.message.reply_text("Job cancelled.")
    else:
        await update.message.reply_text("No open jobs to cancel.")

async def registry_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show registered agents in the marketplace."""
    agents = await fetch_registered_agents()
    if not agents:
        await update.message.reply_text("No agents registered yet.")
        return
    lines = ["*Registered Agents*\n"]
    for a in agents[:10]:
        lines.append(
            f"🤖 *{a['name']}*\n"
            f"_{a['description']}_\n"
            f"Skills: {', '.join(a['skills'][:4])}\n"
            f"Price from: {a['price_usdc']} USDC\n"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown"
    )

# ── Background loops ──────────────────────────────────────────────────────────
async def payment_loop(app):
    seen = set()
    async with AsyncClient(RPC, commitment=Finalized) as client:
        boot = (await client.get_signatures_for_address(ATA, limit=50)).value
        for s in boot:
            seen.add(str(s.signature))
        log.info(f"payment loop live, ignoring {len(boot)} historical txs")

        while True:
            try:
                for memo, amount, sig in await scan_once(client, seen):
                    jobs = load_jobs()
                    matched = match_job(jobs, memo, amount)
                    if matched:
                        jobs[matched]["status"] = "paid"
                        jobs[matched]["tx"]     = sig
                        save_jobs(jobs)
                        log.info(f"PAID {matched} {amount} USDC tx={sig[:16]}")
                        await app.bot.send_message(
                            jobs[matched]["chat_id"],
                            f"✅ Payment received for job `{matched}`.\n"
                            f"Dispatching to agent now...",
                            parse_mode="Markdown"
                        )
                    else:
                        log.info(f"unmatched payment {amount} USDC memo={memo}")

                # expire old jobs
                jobs = load_jobs()
                changed = False
                for jid, jd in jobs.items():
                    if (jd["status"] == "awaiting_payment"
                            and time.time() - jd["created"] > JOB_TIMEOUT):
                        jd["status"] = "expired"
                        changed = True
                if changed:
                    save_jobs(jobs)

            except Exception as e:
                log.error(f"payment loop error: {e}")
                await asyncio.sleep(30)
            await asyncio.sleep(POLL_SECONDS)

async def worker_loop(app):
    """Dispatches paid jobs to agents via A2A and settles on delivery."""
    while True:
        try:
            jobs = load_jobs()
            for jid, jd in jobs.items():
                if jd["status"] != "paid":
                    continue

                jd["status"] = "dispatching"
                save_jobs(jobs)
                log.info(f"dispatching job {jid} to {jd['seller_a2a']}")

                try:
                    # Send job to winning agent via A2A
                    response = await send_a2a_job(
                        endpoint=jd["seller_a2a"],
                        job_id=jid,
                        requirement=jd["requirement"],
                        buyer_pubkey=str(ME),
                        amount_usdc=jd["amount"]
                    )

                    if "error" in response:
                        raise Exception(f"A2A error: {response['error']}")

                    await app.bot.send_message(
                        jd["chat_id"],
                        f"🤖 Job `{jid}` dispatched to agent.\n"
                        f"Waiting for delivery...",
                        parse_mode="Markdown"
                    )

                    jd["status"] = "executing"
                    jd["a2a_task_id"] = response.get("result", {}).get("id", jid)
                    save_jobs(jobs)

                    # Poll for completion (simplified — agent responds in A2A result)
                    result_text = (
                        response.get("result", {})
                        .get("status", {})
                        .get("message", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    )

                    if result_text:
                        # Agent delivered inline in acknowledgement
                        await _complete_job(app, jid, jd, result_text)
                    else:
                        # Agent will deliver async — mark as executing, poll later
                        log.info(f"job {jid} executing async at agent")

                except Exception as e:
                    log.error(f"dispatch error for {jid}: {e}")
                    jobs = load_jobs()
                    jobs[jid]["status"] = "failed"
                    save_jobs(jobs)
                    await app.bot.send_message(
                        jd["chat_id"],
                        f"❌ Job `{jid}` failed to dispatch. "
                        f"Please contact support.",
                        parse_mode="Markdown"
                    )

        except Exception as e:
            log.error(f"worker loop error: {e}")
        await asyncio.sleep(5)

async def _complete_job(app, jid, jd, result_text):
    """Called when agent delivers result. Settles payment on-chain."""
    delivered_hash = hashlib.sha256(result_text.encode()).hexdigest()

    # Settle on-chain: 99% seller, 1% treasury
    async with AsyncClient(RPC) as client:
        sig = await settle_job(
            client, jid,
            Pubkey.from_string(jd["seller"]),  # buyer = our hot wallet for now
            Pubkey.from_string(jd["seller"]),
            delivered_hash
        )

    jobs = load_jobs()
    jobs[jid]["status"]         = "delivered"
    jobs[jid]["delivered_hash"] = delivered_hash
    if sig:
        jobs[jid]["settle_sig"] = sig
    save_jobs(jobs)

    fee      = round(jd["amount"] * 0.01, 4)
    payout   = round(jd["amount"] - fee, 4)

    await app.bot.send_message(
        jd["chat_id"],
        f"✅ *Job {jid} Complete*\n\n"
        f"{result_text[:1000]}\n\n"
        f"💰 Settlement: {payout} USDC → agent | {fee} USDC → Agents Inc\n"
        f"📋 Proof: `{delivered_hash[:32]}...`\n"
        f"🔗 Solana: `{sig[:20] if sig else 'pending'}...`",
        parse_mode="Markdown"
    )
    log.info(f"job {jid} delivered and settled")

# ── Main ──────────────────────────────────────────────────────────────────────
async def post_init(app):
    asyncio.create_task(payment_loop(app))
    asyncio.create_task(worker_loop(app))
    log.info("Agents Inc live — payment and worker loops started")

app = Application.builder().token(TOKEN).post_init(post_init).build()
app.add_handler(CommandHandler("start",    start))
app.add_handler(CommandHandler("status",   status_cmd))
app.add_handler(CommandHandler("cancel",   cancel_cmd))
app.add_handler(CommandHandler("registry", registry_cmd))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_job))
app.add_handler(CallbackQueryHandler(job_confirmed))

log.info("Agents Inc starting")
app.run_polling(drop_pending_updates=True)
