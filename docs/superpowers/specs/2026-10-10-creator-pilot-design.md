# Creator pilot hypothesis — economics not established

Status: RESEARCH ONLY / NOT AUTHORIZED. This corrects earlier earned-fee,
payback and Phase 0 completion claims; retained captures and logs are unchanged.
Nothing in this document changes execution policy or authorizes a launch.

## Objective and evidence boundary

Study whether a creator can recover launch expenditure through independently
verified fee collections. Positive expected value, organic demand, pool-specific
revenue and capital recovery have not been established.

The old estimate multiplied selected pools' historical volume by an assumed fee
rate. The subsequent tracker recorded creator-wide WSOL vault balances, not
pool-attributed cumulative fees. Shared vaults, deposits, withdrawals, missing
accounts encoded as zero, and incomplete identities invalidate the old income,
pacing-causality and payback interpretations. A lower balance does not identify
a collection transaction or recipient.

## Phase 0 — corrected read-only measurement

`track_creator_fees.py` appends schema-v2 observations with:

- full pool/mint/creator/vault/ATA identity and raw WSOL units;
- explicit null censors for missing, invalid or unavailable accounts;
- immediate same-identity baselines and actual elapsed intervals;
- deduplicated creator-vault reads, without summing pool rows as earnings.

Legacy rows remain immutable and unverified. They cannot establish new v2
baselines. Even a valid paired balance change is not attributed fees or a payout.
The passive `summarize_creator_fees.py` checks retained schema/baseline consistency,
not independent account authenticity or on-chain PDA derivations.

**Economics gate: NOT PASSED.** A prospective study still needs frozen,
failure-inclusive launch selection, creator-wide deposit/collection accounting,
transaction-linked recipients, defensible pool attribution and a complete
capital/fee/inventory ledger. Existing capture cadence and provider/request
bounds are unchanged. New acquisition needs separately approved bounds.

## Phase 1 — offline mechanics only

The existing launch and creator-fee claim instruction builders can be exercised
without signing, RPC or wallet access:

```bash
uv run --offline --no-sync python -B learning-examples/token-lifecycles/simulate_creator_pilot_launch.py
uv run --offline --no-sync python -B learning-examples/token-lifecycles/verify_creator_fee_snapshots.py
```

The launch example uses synthetic fee inputs and conditional quote arithmetic.
Its modeled creator allocation is not a verified rebate and is not deducted from
gross funding. Building and decoding instructions does not attest deployed-program
execution, live fees, successful collection, profitability or production readiness.

## Phase 2 — not approved

No funded launch, self-sweep, claim, cap increase or wallet action is authorized.
The standing single-trade live-readiness exception does not cover this creator
pilot. Any future funded experiment requires its own explicit operator decision,
expected wallet, complete risk policy, legal/platform review and independently
satisfied economics gate. Lack of capital is not the only blocker.

## Risks and next decision

Launch expenditure, transaction fees and inventory may remain unrecovered.
Selected winners do not establish a repeatable launch process; descriptive
pacing/retention associations are not causal instructions for attracting buyers.
Platform terms, market-integrity concerns and content/demand remain unresolved.

Continue safe observation and evidence repair. Do not promote this hypothesis
or request a deposit on the basis of historical vault movements.

