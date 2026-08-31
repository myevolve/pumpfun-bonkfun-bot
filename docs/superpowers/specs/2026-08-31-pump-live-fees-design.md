# Pump.fun Live Dynamic Fees Design

**Status:** Approved 2026-08-31

## Goal

Enable live Pump.fun bonding-curve trading for both WSOL- and USDC-paired coins without guessing protocol or creator fees, while preserving the extreme-fast path's no-request dependency between a verified CreateEvent and transaction submission.

## Current boundary

Live Pump.fun execution is blocked in `src/config_loader.py` and `src/trading/platform_aware.py`. `PumpFunCurveManager` returns pre-fee constant-product upper bounds, so its outputs cannot safely become executable token or quote floors.

The deployed Pump fee program exposes a `FeeConfig` PDA derived from `[b"fee_config", pump_program_id]`. Its current account has the expected owner and discriminator and contains independent `fee_tiers` and `stable_fee_tiers`. At design time, both vectors contain one tier at threshold zero with `lp_fee_bps=0`, `protocol_fee_bps=95`, and `creator_fee_bps=30`. Those values are observations, not constants.

## Decision

Use a hybrid attested cache:

1. Strictly decode the deployed `FeeConfig` account locally.
2. In live mode, attest the decoded tier results against read-only simulations of the fee program's `get_fees` instruction at startup, whenever account bytes change, and periodically while unchanged.
3. Use only an immutable, recently observed and recently attested snapshot for the extreme-fast path.
4. Include the fee account in normal curve/mint `getMultipleAccounts` reads. A changed account must be attested before it can produce a quote.
5. Never fall back to hardcoded fees or the legacy Pump Global fee fields.

This keeps fee lookup off the detection-to-submission dependency path while detecting both account-data changes and deployed program-semantic drift.

## Fee account model

Add `src/platforms/pumpfun/fee_schedule.py` with immutable, slotted dataclasses:

- `PumpFees(lp_fee_bps, protocol_fee_bps, creator_fee_bps)`
- `PumpFeeTier(market_cap_threshold_raw, fees)`
- `PumpFeeConfig(flat_fees, regular_tiers, stable_tiers, digest)`
- `PumpFeeSnapshot(config, observed_at, attested_at)`
- `PumpQuote(...)` carrying output, fee breakdown, market cap, and config digest

The account decoder validates:

- account owner equals the Pump fee program;
- discriminator equals the IDL `FeeConfig` discriminator;
- every fixed field and vector entry is in bounds;
- both tier vectors are non-empty and strictly increasing by threshold;
- every BPS value is a valid u64 no greater than 10,000;
- protocol plus creator BPS does not exceed 10,000;
- unused preallocated bytes are zero, so an unknown appended nonzero layout fails closed.

The shared `IDLParser` is not expanded for this work. It currently cannot decode the current Anchor shape `Vec<{defined: FeeTier}>`; a purpose-built decoder keeps this fund-moving boundary small and strict.

## Tier selection and units

For a bonding curve:

```text
market_cap_raw = virtual_quote_reserves * token_total_supply // virtual_token_reserves
```

The value remains in the quote asset's raw units. WSOL curves select `fee_tiers`; USDC curves select `stable_fee_tiers`. There is no SOL/USD oracle conversion. The fee program receives `is_new_quote_mint=false` for WSOL and `true` for USDC during attestation.

Selection matches Pump's documented algorithm: use the highest tier whose threshold is less than or equal to market cap, falling back to the first tier below the first threshold.

Bonding-curve quotes reject a selected nonzero LP fee because Pump's documented bonding-curve formulas define only protocol and creator fees. Creator fees apply only when the authoritative creator is not `Pubkey::default()`.

## Exact integer quote math

All calculations use raw integers.

For exact quote-in buys, follow the current Pump IDL rounding contract:

```text
net = floor(spendable * 10_000 / (10_000 + protocol_bps + creator_bps))
protocol_fee = ceil(net * protocol_bps / 10_000)
creator_fee = ceil(net * creator_bps / 10_000)
if net + protocol_fee + creator_fee > spendable:
    net -= net + protocol_fee + creator_fee - spendable
tokens_out = floor((net - 1) * virtual_token_reserves /
                   (virtual_quote_reserves + net - 1))
```

Cap token output by `real_token_reserves`. Reject a nonpositive net trade.

For exact token-out buys used by extreme-fast mode:

```text
net_quote = ceil(tokens * virtual_quote_reserves /
                 (virtual_token_reserves - tokens)) + 1
required_quote = net_quote
               + ceil(net_quote * protocol_bps / 10_000)
               + ceil(net_quote * creator_bps / 10_000)
```

The existing configured quote amount plus slippage remains the instruction's hard maximum. If the fixed token target cannot fit under that cap, skip before submission.

For exact token-in sells:

```text
gross_quote = floor(tokens * virtual_quote_reserves /
                    (virtual_token_reserves + tokens))
net_quote = gross_quote
          - ceil(gross_quote * protocol_bps / 10_000)
          - ceil(gross_quote * creator_bps / 10_000)
```

Apply user slippage only after deriving the fee-adjusted output.

## State and data flow

### Normal buy and sell

`PumpFunCurveManager.get_pool_state_and_token_program` reads curve, mint, and fee config in one `getMultipleAccounts` call. It validates all three accounts, attests a changed fee config, and attaches the immutable snapshot to the decoded pool state. Quote methods accept this already-read state, eliminating the current second curve read and its slot inconsistency.

The LetsBonk implementation accepts the same optional state argument and retains its existing math, keeping the shared `CurveManager` contract uniform.

### Extreme-fast buy

Persist all quote inputs already present in Pump's correlated CreateEvent:

- virtual token reserves;
- virtual quote reserves;
- real token reserves;
- token total supply;
- creator and quote mint;
- mayhem/cashback flags and incomplete state.

These fields round-trip through recovery serialization. A verified event plus a fresh attested fee snapshot can quote the configured fixed token target without a trade-triggered account read. Incomplete, uncorrelated, malformed, or stale data continues through the existing refresh-or-skip path.

## Runtime lifecycle

For live Pump mode, startup must fetch and attest a fee snapshot before starting token processing or listeners. The schedule refresh loop then:

- observes the fee account every 2 seconds;
- requires observation freshness within 10 seconds for cached extreme-fast quotes;
- re-attests unchanged program semantics every 60 seconds;
- allows at most 120 seconds since successful attestation;
- serializes validation and snapshot promotion under one async lock.

A transient read failure retains the prior snapshot only until its freshness limits expire. An owner, discriminator, layout, tier, or program-result mismatch invalidates the candidate and prevents its use. Cleanup cancels the refresh loop before closing the RPC client.

`get_fees` attestation is simulation-only with signature verification disabled. It probes regular and stable tier boundaries and more than one trade size. Any trade-size-dependent result fails attestation because the local account schema carries no rule capable of reproducing it.

## Failure behavior

Live Pump quotes fail closed on:

- missing, stale, or unattested fee config;
- fee account owner/discriminator/layout mismatch;
- empty, unsorted, or impossible tiers;
- local `get_fees` mismatch;
- unsupported quote mint;
- missing event reserves or creator eligibility;
- nonzero bonding-curve LP fee;
- a fixed extreme-fast target exceeding its configured quote cap.

A stale quote cannot exceed the buy max or weaken the sell minimum because those constraints remain instruction arguments. The likely failure is a pre-submit skip or an on-chain revert, not unbounded spend.

## Verification

- Unit tests for strict account decoding, tier boundaries, WSOL/stable selection, creator eligibility, every rounding edge, malformed inputs, freshness, and attestation mismatch.
- Integration tests proving normal quotes reuse one state read and extreme-fast quotes make no trade-triggered fee read.
- Recovery serialization tests for all event quote fields.
- Offline verifier comparing account layout, local math, and IDL instruction encoding.
- Read-only mainnet simulations of `get_fees`, `buy_v2`, and `sell_v2` for both WSOL and USDC curves. No transaction submission.
- Existing full regression suite, protocol safety verifiers, scoped Ruff, compile checks, and Python review before promotion.
