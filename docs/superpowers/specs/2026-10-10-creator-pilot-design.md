# Creator Pilot Design — the measured supply-side play

Status: DESIGN — nothing here moves funds. Every phase gate needs the
operator's explicit approval.

## Objective

Validate and, if validated, operate the only follower-measured +EV role
this lab has found: **launch a coin on pump.fun, self-sweep the curve to
graduation, and hold the creator fee annuity** (30 bps of pool volume).

## Measured basis (the evidence)

| claim | number | source |
|---|---|---|
| Whale sweeps are mostly self-sweeps | 66% of 477 whale sweeps | whale census, Oct 7 |
| Sweep size | median **85.0 SOL** (also the exact letsbonk migration figure) | census + forward test |
| Creator fee share | 30 bps of pool volume | live fee_config PDA read |
| Pool volume (whale-swept cohort) | 1,858 SOL/24h median | lifecycles tape |
| Estimated annuity | ~5.6 SOL/day → **~15-day payback** on 85 SOL | derived |
| Graduation threshold | real_quote fills at exactly 85.0 SOL | forward test |
| Pool decay | dumps to −91% within ~25 min, volume continues during churn | rollover cohort |

**Survivorship caveat**: the 1,858 SOL/24h volume was measured on pools
that attracted whale sweeps — successful launches. A random creator's
pool may see materially less. This is the single largest unknown, and it
is measurable **before any money is spent** (Phase 0).

## Phase 0 — creator fee tracker (read-only, ~1 week)

The census identified the self-sweep creators by wallet. Their fee
income is readable: the creator's fee account accrues 30 bps of their
pool's volume; balances are on-chain.

1. Pick N≈20 graduated pools from the whale census (spread across the
   fund bands and ages).
2. Daily: read each pool's creator fee account balance → the daily
   accrual per pool.
3. Output: the **measured annuity curve** — accrual by pool age (day 0,
   day 1, day 2...), by fund size. This replaces the survivorship-tainted
   1,858 SOL estimate with the real decay function.

**Gate**: if the measured day-1..7 accrual implies payback > ~45 days on
the median 85 SOL sweep, STOP — the annuity does not survive contact
with the decay data. If payback lands in the 10–20 day band, proceed.

Build: one script, existing RPC patterns, read-only. ~150 lines.

## Phase 1 — mechanics (no funds) [EXECUTED 2026-10-14]

Ported the proven `mint_and_buy_v2.py` builders into the bot's
`instruction_builder` and verified offline:

1. `create_v2` (16 accounts) + `extend_account` (5 accounts) added to
   `PumpFunAddressProvider.get_create_v2_instruction_accounts` and
   `PumpFunInstructionBuilder.build_create_v2_instructions`, machine-checked
   against `idl/pump_fun_idl.json` in `learning-examples/verify_v2_account_layout.py`.
2. Self-sweep dry-run simulator (`learning-examples/token-lifecycles/simulate_creator_pilot_launch.py`):
   steps a fresh curve across 4 `buy_v2` tranches using `quote_buy_exact_out`
   to exact completion (`real_token_reserves == 0`, `real_quote_sol == 85.0054 SOL`,
   `gross_spend == 86.0679 SOL`, `creator_self_rebate == 0.2550 SOL`,
   `net_lockup == 85.8129 SOL`).
3. Post-graduation hold: derives the canonical PumpSwap pool and creator vault
   without building any sell or LP withdrawal instruction.

Gate: PASSED (`uv run learning-examples/token-lifecycles/simulate_creator_pilot_launch.py`).
## Phase 2 — bounded live pilot (operator approval required)

- **Capital**: 85 SOL sweep + ~1.25% fees + buffer ≈ **90 SOL**.
- **One launch**. The coin needs real content — the annuity is paid by
  followers' churn; a dead launch earns nothing. (The Jev quality score
  is a candidate pre-launch screen for the name/meta.)
- **No raises**: no cap increases, no second launch, no size changes —
  the standing live-test rules apply.
- **Success criteria**: measured creator-fee accrual on the pilot pool
  ≥ Phase 0's median cohort accrual × 0.5 over the first 7 days.
- **Abort**: any failure, timeout, or unknown outcome stops the flow
  permanently (the standing rule). The position after graduation is
  held, not traded.
- **Sweep-pacing rule (measured in Phase 0 across 49 graduated pools, 2026-10-14)**:
  - **Paced sweeps (`grad_elapsed_s >= 8s`, `n=13`)**: **3 of 13 (`23.1%`)** earned `>15 SOL` (`SPOOF` `62.7s` → `28.27 SOL`; `INTENTS` `52.7s` → `25.54 SOL`; `SEED` `10.9s` → `15.09 SOL`; `Incognijak` `45.4s` → `0.72 SOL`; `PEA` `28.3s` → `0.39 SOL`), **`6 / 13` (`46.2%`)** earned `≥0.1 SOL`, mean **`5.40 SOL`**, median **`0.0150 SOL`** (`405x` higher median than atomic sweeps).
  - **Atomic/instant sweeps (`grad_elapsed_s < 8s`, `n=36`)**: **1 of 36 (`2.8%`)** earned `>1 SOL` (`IOF`), **`3 / 36` (`8.3%`)** earned `≥0.1 SOL`, mean **`0.54 SOL`**, median **`0.000037 SOL`**.
  - **Post-graduation retention**: **4 of 28 (`14.3%`)** pools that held `≥50%` of `open_price` at `G+120s` earned `>15 SOL` (mean `3.16 SOL`), vs **`0 of 20 (0%)`** pools that dumped `<50%` by `G+120s` (mean `0.05 SOL`).
  - **Implication for Phase 2**: space the 4 self-sweep tranches across **15–60 seconds** (`4–15s` between tranches) rather than bundling them in a single slot, so the rising curve remains visible on the bonding-curve feed before migrating to PumpSwap.

## Risks

1. **Capital lockup**: the 85 SOL becomes the pool's liquidity. Withdrawing
   it rugs the pool — rejected by design. The realistic outcome is a slow
   fee annuity on a decaying pool, not a refund.
2. **Volume survivorship**: the core unknown; Phase 0 exists to measure it.
3. **Competition**: the census found scripted launch services (170 SOL
   sweeps). They operate at scale — the margin is real but crowded.
4. **Content risk**: a launch nobody trades earns nothing; the coin
   quality problem is real and unsolved (the Jev screen is a candidate,
   unmeasured).
5. **Platform/ToS risk**: operating as a self-sweeping creator may violate
   pump.fun terms; the operator accepts this explicitly before Phase 2.
6. **Ethical line**: the design never withdraws follower liquidity and
   never markets the coin to the lab's audience.

## Sequencing

Phase 0 starts immediately (read-only, no approval needed beyond this
design being accepted as the plan). Phase 1 builds while Phase 0
measures. Phase 2 waits for the operator's explicit yes AND a Phase 0
pass.
