"""
Phidias Escrow Client — interacts with the on-chain escrow program
without anchorpy, using raw instruction discriminators.
Program: DetvUJkb8mzH1vAoYTXz7uyjS2Sw7eZXdC6UPP4uBS3y
"""
import struct, hashlib, logging
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.system_program import ID as SYS_PROG
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed

log = logging.getLogger("phidias.escrow")

PROGRAM_ID = Pubkey.from_string("DetvUJkb8mzH1vAoYTXz7uyjS2Sw7eZXdC6UPP4uBS3y")

DISC_CREATE  = bytes.fromhex("fdd7a574246c4450")
DISC_RELEASE = bytes.fromhex("92fd81e91491b5ce")
DISC_REFUND  = bytes.fromhex("6bba59631ac217cc")

def escrow_pda(order_id: str, client_pubkey: Pubkey):
    """Derive the escrow PDA for a given order_id and client pubkey."""
    seeds = [b"escrow", order_id.encode(), bytes(client_pubkey)]
    pda, bump = Pubkey.find_program_address(seeds, PROGRAM_ID)
    return pda, bump

async def create_escrow(
    client: AsyncClient,
    payer: Keypair,
    order_id: str,
    amount_lamports: int,
    file_hash_u64: int,
    order_id_u64: int,
    deadline: int,
) -> str | None:
    """Create an escrow account for an order. Returns tx signature or None."""
    pda, _ = escrow_pda(order_id, payer.pubkey())
    data = (
        DISC_CREATE
        + struct.pack("<Q", amount_lamports)
        + struct.pack("<Q", file_hash_u64)
        + struct.pack("<Q", order_id_u64)
        + struct.pack("<q", deadline)
    )
    ix = Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=pda,            is_signer=False, is_writable=True),
            AccountMeta(pubkey=payer.pubkey(), is_signer=True,  is_writable=True),
            AccountMeta(pubkey=SYS_PROG,       is_signer=False, is_writable=False),
        ],
        data=bytes(data),
    )
    try:
        bh = (await client.get_latest_blockhash()).value.blockhash
        tx  = Transaction([payer], [ix], bh)
        sig = (await client.send_transaction(tx)).value
        await client.confirm_transaction(sig, commitment=Confirmed)
        log.info(f"escrow created for {order_id}: {pda} sig={str(sig)[:16]}")
        return str(sig)
    except Exception as e:
        log.error(f"create_escrow failed for {order_id}: {e}")
        return None

async def release_escrow(
    client: AsyncClient,
    agent: Keypair,
    order_id: str,
    client_pubkey: Pubkey,
    delivered_hash_u64: int,
) -> str | None:
    """Release escrow funds to agent after delivery. Returns tx sig or None."""
    pda, _ = escrow_pda(order_id, client_pubkey)
    data = DISC_RELEASE + struct.pack("<Q", delivered_hash_u64)
    ix = Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=pda,             is_signer=False, is_writable=True),
            AccountMeta(pubkey=agent.pubkey(),  is_signer=True,  is_writable=True),
            AccountMeta(pubkey=SYS_PROG,        is_signer=False, is_writable=False),
        ],
        data=bytes(data),
    )
    try:
        bh = (await client.get_latest_blockhash()).value.blockhash
        tx  = Transaction([agent], [ix], bh)
        sig = (await client.send_transaction(tx)).value
        await client.confirm_transaction(sig, commitment=Confirmed)
        log.info(f"escrow released for {order_id}: sig={str(sig)[:16]}")
        return str(sig)
    except Exception as e:
        log.error(f"release_escrow failed for {order_id}: {e}")
        return None

async def refund_escrow(
    client: AsyncClient,
    payer: Keypair,
    order_id: str,
    current_time: int,
) -> str | None:
    """Refund escrow to client after deadline. Returns tx sig or None."""
    pda, _ = escrow_pda(order_id, payer.pubkey())
    data = DISC_REFUND + struct.pack("<q", current_time)
    ix = Instruction(
        program_id=PROGRAM_ID,
        accounts=[
            AccountMeta(pubkey=pda,            is_signer=False, is_writable=True),
            AccountMeta(pubkey=payer.pubkey(), is_signer=True,  is_writable=True),
            AccountMeta(pubkey=SYS_PROG,       is_signer=False, is_writable=False),
        ],
        data=bytes(data),
    )
    try:
        bh = (await client.get_latest_blockhash()).value.blockhash
        tx  = Transaction([payer], [ix], bh)
        sig = (await client.send_transaction(tx)).value
        await client.confirm_transaction(sig, commitment=Confirmed)
        log.info(f"escrow refunded for {order_id}: sig={str(sig)[:16]}")
        return str(sig)
    except Exception as e:
        log.error(f"refund_escrow failed for {order_id}: {e}")
        return None

def hash_to_u64(file_hash: str) -> int:
    """Convert a sha256 hex string to a u64 for on-chain storage."""
    return int(file_hash[:16], 16) % (2**64)

def order_id_to_u64(order_id: str) -> int:
    """Convert a memo string to a u64."""
    return int(hashlib.sha256(order_id.encode()).hexdigest()[:16], 16) % (2**64)
