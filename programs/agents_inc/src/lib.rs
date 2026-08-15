use anchor_lang::prelude::*;
use anchor_spl::token::{self, Token, TokenAccount, Transfer};

declare_id!("E6oC1Dm5UymCQ5Uq3EpZQgUaDS1PB1KSrGKGFXiBfVsS");

#[cfg(not(feature = "no-entrypoint"))]
solana_security_txt::security_txt! {
    name: "Agents Inc",
    project_url: "https://agentsinc.app",
    contacts: "email:ram@nthmom.ent",
    policy: "https://github.com/NthMOMENT/AgentsInc",
    source_code: "https://github.com/NthMOMENT/AgentsInc"
}

/// Agents Inc treasury — receives 1% cut on every settlement
const TREASURY: &str = "6qh5tiYjFXgnga2ErRrAgb94UK7F35fQ8UDj1ZDnpinb";
const FEE_BPS: u64 = 100; // 1% = 100 basis points out of 10000

#[program]
pub mod agents_inc {
    use super::*;

    /// Instruction 1: AUTHORIZE
    /// Buyer signs at job posting time.
    /// Records job details on-chain. No funds move.
    /// Buyer must have already set Agents Inc as delegate
    /// on their USDC token account before calling this.
    pub fn authorize(
        ctx: Context<Authorize>,
        job_id: [u8; 8],
        amount: u64,
        deadline: i64,
    ) -> Result<()> {
        require!(amount > 0, AgentsIncError::InvalidAmount);
        require!(deadline > Clock::get()?.unix_timestamp, AgentsIncError::DeadlineInPast);

        let job = &mut ctx.accounts.job_authorization;
        job.job_id = job_id;
        job.buyer = ctx.accounts.buyer.key();
        job.seller = ctx.accounts.seller.key();
        job.buyer_token_account = ctx.accounts.buyer_token_account.key();
        job.seller_token_account = ctx.accounts.seller_token_account.key();
        job.amount = amount;
        job.deadline = deadline;
        job.settled = false;
        job.bump = ctx.bumps.job_authorization;

        emit!(JobAuthorized {
            job_id,
            buyer: ctx.accounts.buyer.key(),
            seller: ctx.accounts.seller.key(),
            amount,
            deadline,
        });

        Ok(())
    }

    /// Instruction 2: SETTLE
    /// Agents Inc signs when job is delivered.
    /// Atomically splits payment:
    ///   99% → seller agent token account
    ///   1%  → Agents Inc treasury token account
    /// Buyer's token account must still have Agents Inc as delegate.
    pub fn settle(
        ctx: Context<Settle>,
        job_id: [u8; 8],
        delivered_hash: [u8; 8],
    ) -> Result<()> {
        let job = &mut ctx.accounts.job_authorization;

        require!(!job.settled, AgentsIncError::AlreadySettled);
        require!(job.job_id == job_id, AgentsIncError::JobIdMismatch);
        require!(
            Clock::get()?.unix_timestamp <= job.deadline,
            AgentsIncError::DeadlineExpired
        );

        let total = job.amount;
        let fee = total
            .checked_mul(FEE_BPS)
            .unwrap()
            .checked_div(10_000)
            .unwrap();
        let seller_amount = total.checked_sub(fee).unwrap();

        // Transfer 99% to seller
        token::transfer(
            CpiContext::new(
                ctx.accounts.token_program.to_account_info(),
                Transfer {
                    from: ctx.accounts.buyer_token_account.to_account_info(),
                    to: ctx.accounts.seller_token_account.to_account_info(),
                    authority: ctx.accounts.agents_inc.to_account_info(),
                },
            ),
            seller_amount,
        )?;

        // Transfer 1% to treasury
        token::transfer(
            CpiContext::new(
                ctx.accounts.token_program.to_account_info(),
                Transfer {
                    from: ctx.accounts.buyer_token_account.to_account_info(),
                    to: ctx.accounts.treasury_token_account.to_account_info(),
                    authority: ctx.accounts.agents_inc.to_account_info(),
                },
            ),
            fee,
        )?;

        job.settled = true;
        job.delivered_hash = delivered_hash;

        emit!(JobSettled {
            job_id,
            buyer: job.buyer,
            seller: job.seller,
            total,
            fee,
            delivered_hash,
        });

        Ok(())
    }

    /// Instruction 3: CANCEL
    /// Buyer can cancel after deadline if job was never settled.
    /// Closes the authorization account, returns rent to buyer.
    /// No funds were held by contract so nothing else to return.
    pub fn cancel(ctx: Context<Cancel>, job_id: [u8; 8]) -> Result<()> {
        let job = &ctx.accounts.job_authorization;

        require!(!job.settled, AgentsIncError::AlreadySettled);
        require!(job.job_id == job_id, AgentsIncError::JobIdMismatch);
        require!(
            Clock::get()?.unix_timestamp > job.deadline,
            AgentsIncError::DeadlineNotReached
        );

        emit!(JobCancelled {
            job_id,
            buyer: job.buyer,
        });

        // Account is closed via `close = buyer` constraint below
        Ok(())
    }
}

// ── Account Structures ────────────────────────────────────────────────────────

#[account]
pub struct JobAuthorization {
    pub job_id: [u8; 8],               // unique job identifier
    pub buyer: Pubkey,                  // who authorized payment
    pub seller: Pubkey,                 // winning agent wallet
    pub buyer_token_account: Pubkey,    // buyer's USDC token account
    pub seller_token_account: Pubkey,   // seller's USDC token account
    pub amount: u64,                    // total USDC in micro-units
    pub deadline: i64,                  // unix timestamp
    pub settled: bool,                  // true after settlement
    pub delivered_hash: [u8; 8],        // hash of delivered output
    pub bump: u8,                       // PDA bump seed
}

impl JobAuthorization {
    // 8 discriminator + 8 + 32 + 32 + 32 + 32 + 8 + 8 + 1 + 8 + 1
    pub const LEN: usize = 8 + 8 + 32 + 32 + 32 + 32 + 8 + 8 + 1 + 8 + 1;
}

// ── Contexts ──────────────────────────────────────────────────────────────────

#[derive(Accounts)]
#[instruction(job_id: [u8; 8])]
pub struct Authorize<'info> {
    #[account(
        init,
        payer = buyer,
        space = JobAuthorization::LEN,
        seeds = [b"job", job_id.as_ref(), buyer.key().as_ref()],
        bump
    )]
    pub job_authorization: Account<'info, JobAuthorization>,

    #[account(mut)]
    pub buyer: Signer<'info>,

    /// CHECK: seller is just recorded, not signing at this stage
    pub seller: UncheckedAccount<'info>,

    #[account(mut)]
    pub buyer_token_account: Account<'info, TokenAccount>,

    /// CHECK: seller token account recorded for settlement
    pub seller_token_account: UncheckedAccount<'info>,

    pub system_program: Program<'info, System>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
#[instruction(job_id: [u8; 8])]
pub struct Settle<'info> {
    #[account(
        mut,
        seeds = [b"job", job_id.as_ref(), job_authorization.buyer.as_ref()],
        bump = job_authorization.bump,
        has_one = buyer_token_account,
        has_one = seller_token_account,
    )]
    pub job_authorization: Account<'info, JobAuthorization>,

    /// Agents Inc operator signs settlement
    #[account(
        constraint = agents_inc.key().to_string() == "6qh5tiYjFXgnga2ErRrAgb94UK7F35fQ8UDj1ZDnpinb"
            @ AgentsIncError::Unauthorized
    )]
    pub agents_inc: Signer<'info>,

    #[account(mut)]
    pub buyer_token_account: Account<'info, TokenAccount>,

    #[account(mut)]
    pub seller_token_account: Account<'info, TokenAccount>,

    #[account(
        mut,
        constraint = treasury_token_account.owner.to_string() == TREASURY
            @ AgentsIncError::InvalidTreasury
    )]
    pub treasury_token_account: Account<'info, TokenAccount>,

    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
#[instruction(job_id: [u8; 8])]
pub struct Cancel<'info> {
    #[account(
        mut,
        seeds = [b"job", job_id.as_ref(), buyer.key().as_ref()],
        bump = job_authorization.bump,
        close = buyer
    )]
    pub job_authorization: Account<'info, JobAuthorization>,

    #[account(mut)]
    pub buyer: Signer<'info>,

    pub system_program: Program<'info, System>,
}

// ── Events ────────────────────────────────────────────────────────────────────

#[event]
pub struct JobAuthorized {
    pub job_id: [u8; 8],
    pub buyer: Pubkey,
    pub seller: Pubkey,
    pub amount: u64,
    pub deadline: i64,
}

#[event]
pub struct JobSettled {
    pub job_id: [u8; 8],
    pub buyer: Pubkey,
    pub seller: Pubkey,
    pub total: u64,
    pub fee: u64,
    pub delivered_hash: [u8; 8],
}

#[event]
pub struct JobCancelled {
    pub job_id: [u8; 8],
    pub buyer: Pubkey,
}

// ── Errors ────────────────────────────────────────────────────────────────────

#[error_code]
pub enum AgentsIncError {
    #[msg("Amount must be greater than zero")]
    InvalidAmount,
    #[msg("Deadline must be in the future")]
    DeadlineInPast,
    #[msg("Deadline has not been reached yet")]
    DeadlineNotReached,
    #[msg("Deadline has expired")]
    DeadlineExpired,
    #[msg("Job already settled")]
    AlreadySettled,
    #[msg("Job ID mismatch")]
    JobIdMismatch,
    #[msg("Unauthorized: only Agents Inc can settle")]
    Unauthorized,
    #[msg("Invalid treasury account")]
    InvalidTreasury,
}
