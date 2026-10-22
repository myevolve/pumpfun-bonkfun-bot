# Strategy Inventory — the regime-switched arsenal

The operator's thesis: hold a large inventory of measured strategies, each
with pre-registered regime conditions, so that as markets ebb and flow the
system can deterministically identify the current regime and strike when a
strategy's risk is tolerant and acceptable.

This file is the inventory. Rules:

1. Every strategy enters only with a **measured** verdict — a hypothesis is
   not an inventory entry (the threshold-miner lesson: 126 one-variable
   candidates were not "all hypotheses").
2. Every entry ships with its **regime detector** — a deterministic,
   pre-registered condition readable in real time. No discretionary
   "it feels hot" switches.
3. Every entry ships with its **worst case**, quantified from the record.
4. Verdicts are CONDITIONAL, not terminal: "dead in regime R" is the
   honest form. The detector names R.
5. Only the operator moves a strategy to ARMED. Nothing here writes risk
   limits or deploys capital.

---

## 1. Creator annuity (pump.fun launch → self-sweep → hold)

- **Mechanism**: create a coin, sweep ~85 SOL to graduation, hold the
  pool; earn the creator fee stream (30 bps of pool volume).
- **Measured**: +EV — median 85 SOL sweep → ~5.6 SOL/day at the measured
  1,858 SOL/24h pool volume; payback ~15 days. 66% of whale sweeps are
  creators doing exactly this; the census found scripted launch services
  (4 wallets × exactly 170 SOL) operating profitably at scale.
- **Regime detector**: PumpSwap 24h aggregate volume ≥ a floor (the
  annuity scales with volume; the fee stream exists in any regime with
  churn). Readable live from the fee program.
- **Worst case**: capital lockup (~85 SOL), launch-service competition
  compressing the payback, the creator's own pool dump risk (the churn
  that pays the annuity also craters the price — the position is the fee
  stream, not the tokens).
- **Status**: MEASURED-LIVE — Phase 0 1-week gate completed (`234.7h` / `9.78 days` / `52` snapshots across `65/66` graduated `grad_marks` pools: `8/65` (`12.3%`) earned `1.3..36.8 SOL` each, `5/65` (`7.7%`) earned `15.1..36.8 SOL`, with `FLAMBO` `91174` accruing **`24.73 SOL`** (`23.46 SOL` drained by creator) and `SPOOF` reaching **`36.80 SOL`**; **`129.96 SOL` combined across the top 8**, while median pool earned `~0.0001 SOL` — lottery structure confirmed). Phase 1 launch, self-sweep, and creator-fee claim builders (`collect_creator_fee_v2`, `collect_coin_creator_fee`) shipped (`simulate_creator_pilot_launch.py`). Phase 2 blocked on capital (~86 SOL) + explicit operator approval.

- **Mechanism**: hold the pool position; exit when the pool drops ≥5%
  below its running peak, with `peak` seeded at graduation `open_price`
  (`bbb6510`) so open-and-dump pools trigger on the first poll.
- **Measured**:
  - **Cumulative post-fix 5% cohort (`id >= 66872`, n=50 clean resolved)**:
    **45W / 5L (90.0%)**, **+3,054.6 points** (+61.1% mean, +53.4% median)
    vs +2,246.2 pts at 60s (34W/16L), +2,768.0 pts at 300s (23W/27L), and +812.7 pts at 900s (13W/37L, −91.4% median).
  - **Focused 6-hour live test (2026-10-14, n=4)**: **3W / 1L**, **+175.1 points**
    (+43.8% mean) vs −82.2 pts at 60s (1W/3L), −37.0 pts at 300s (1W/3L), and
    −365.7 pts at 900s (0W/4L) — **+257.3 pts** over 60s and **+540.7 pts** over 900s.
- **Regime detector**: none needed — it is per-position and
  self-arming (`exit_strategy: "rollover"` in `UniversalTrader`, with
  `max_hold_time` curve-stall backstop and immediate `open_price <= entry_price`
  protection). Requires an ENTRY to exit from.
- **Worst case**:
  - Second-wave continuation giveback (`BUPE` 75143: exited +45.8% at G+14.2s,
    ran to +119.0% at 300s before crashing to −91.7% at 900s; `QUANTUMJAK` 68368).
  - Below-entry graduation open (`SEALYS` 74705: opened at −20.6%, exited −43.7%;
    `ROBINJEAN` 77960: opened at −43.9%, exited −91.4%; `DRAINLOVE` 81542:
    opened at −91.0%, exited −91.0%; all 6 historical `open_price < entry_price`
    graduations lost — UniversalTrader's live monitor stops out immediately at open if `open_price <= entry_price`).
## 3. Crossing-entry sniping (in-band 60–85 SOL accept) — band-gated

- **Mechanism**: accept a coin at the 60-SOL crossing, ride to graduation,
  exit via the rollover rule — gated by the measured buyer band:
  **2–3 distinct non-creator buyers at accept, non-mayhem**.
- **Measured** (the buyer-count census, n=37, TRAIN/LOCK split):

  | buyers at accept | n | mean r60 | mean r900 |
  |---|---|---|---|
  | 1 | 2 | −92.0% | −92.0% |
  | **2–3** | **8** | **+102.5%** | **+30.0%** |
  | 4–10 | 21 | −10.4% | −45.1% |
  | >10 | 6 | −23.1% | −86.2% |

  The band survives the split: TRAIN +1.07 vs −0.25 others; LOCK +0.98
  vs −0.06 others. Mayhem is an independent killer (all 4 mayhem accepts
  lost 80–98% regardless of buyers) — hence `exclude_mayhem`.
- **Regime detector**: same as before — the trailing-30-day net-of-cost
  marks re-test. The band rule is now implemented in the gate
  (`min_buyers`/`max_buyers`/`exclude_mayhem`) and re-measures live once
  armed; the clue either confirms on the next cohort or dies.
- **Worst case**: the measured latency drift inside the band; the
  WICK/68368 rebound class (the rollover exit gave back +98% while the
  timeline ran to +742% on the largest resolved winner).
- **Status**: ARMED IN DRY-RUN (`whaleride-paper`: `min_buyers: 2, max_buyers: 3, exclude_mayhem: true`). Across `79` post-clock accepts (`id >= 62798`), the 2–3 non-mayhem band (`n=42`) achieved **`92.9%` graduation (`39/42`)**, **`35W / 7L` (`83.3%` win)**, **`+2,767.1 pts` (`+65.9%` mean, `+75.3%` median)** vs **`+114.8 pts` (`+3.1%` mean)** on the `37` out-of-band accepts. Sub-refinement: when the 60-SOL crossing trade itself is a whale sweep (`last_buy_sol >= 10 SOL`, `n=39`, `38/39` `buyers == 3`), graduation is **`97.4%` (`38/39`)** and live return is **`34W / 5L` (`87.2%`), `+2,707.4 pts` (`+69.4%` mean)**. Moreover, on-chain audit of all `28` `buyers == 4` skips (`85935..98965`) proved `8/28` (`28.6%`) never graduated and `20/28` were drained to `1.4..7.4 SOL` in quote vault with `0.0000..0.0037 SOL` creator fees (`0/28` organic winners).

## 4. Follower LP — PumpSwap graduated pools

- **Mechanism**: deposit into the migrated PumpSwap pool, earn fees.
- **Measured**: structurally dead — the live fee config gives LPs
  **0 bps**; protocol 95 / creator 30 split everything.
- **Regime detector**: the fee config itself. If the config ever grants
  LPs bps, the route re-opens (the instrument reads the config PDA).
- **Worst case**: n/a — 0 bps caps it.
- **Status**: MEASURED-DEAD (structural). Detector armed on the fee
  config.

## 5. Follower LP — letsbonk migrated CPMM pools

- **Mechanism**: deposit into the migrated Raydium CPMM pool; fees
  accrue to LPs pro-rata; the seed LP is locked (no platform rug).
- **Measured**: economically dead at the current regime mix — the
  pool's SOL side decays a median **−73%** post-migration (21 pools,
  mean ratio 0.352); one +27% riser in 21.
- **Regime detector**: riser-share of recent migrations — if the
  +27%-style cohort (SOL side ≥ fund) dominates the trailing window,
  LPing risers is conditionally alive. Measured from the same PDA reads
  as the decay census, repeatable daily.
- **Worst case**: the measured −73%; the locked seed LP protects the
  base but not the follower's deposit from the dump.
- **Status**: MEASURED-DEAD at current mix — the riser-share detector
  is the re-entry condition.

## 6. News / sentiment LLM pipelines

- **Mechanism**: multi-LLM research team (the six-bot pattern).
- **Measured**: structurally slower than the market's 18-second liquidity
  half-life; the measured edges here are latency- and structure-bound,
  not information-bound.
- **Status**: DEAD BY ARCHITECTURE. No detector can fix latency.

## 7. Threshold mining / self-improving prompts

- **Status**: FORBIDDEN by discipline — invalid methodology (in-sample
  candidate mining with no held-out test). This inventory grows by
  census, not by mining.

---

## Completed hypothesis censuses (measured 2026-10-14, `lifecycles_24h.jsonl`, n=24,015 coins)

All four queued hypotheses have been measured on the 24,015-coin exact-CPMM tape with strict out-of-sample (TRAIN first 12,007 coins → LOCK last 12,008 coins) evaluation:

- **Leaderboard winner census** (top-100 consistent realized-cashflow winners, `≥10` coins bought, unsold bags valued at `0 SOL`):
  - **31/100** (`+1,662.8 SOL`): slot-0 bundled launch insiders (`med_dslot = 0.0`, `65.3%` median win rate). Inaccessible to external followers.
  - **1/100** (`+19.4 SOL`): `dslot 1..2` ultra-early sniper (`20.6%` win rate).
  - **15/100** (`+292.2 SOL`): Mayhem `sol_vault` (`BwWK17cb…`) automated buyback scalpers (`med_dslot = 90.5`, `med_rsol = 1.15 SOL`, `50.0%` win rate). Cloning at retail latency (`dslot 40..75`, `0.5..5.0 SOL`) yields `+2.99%` median on LOCK (`55.2%` win rate, `n=882`) but **`−4.16%` mean** due to left-tail dumps when the automated buyback doesn't fire.
  - **53/100** (`+460.3 SOL`): non-Mayhem mid-curve cluster traders (`med_dslot = 41.5`, `med_rsol = 15.29 SOL`, `58.8%` win rate). Selecting the 161 profitable non-Mayhem wallets strictly on TRAIN and following `≥2` wallets buying within 3 slots on LOCK (`n=209`) collapses out-of-sample: **`29.7%` win rate, `−4.33%` mean, `−10.67%` median**. **Verdict: MEASURED-DEAD OOS** (in-sample survivorship + follower latency penalty).
- **Creation-window re-test by regime** (`dslot 1..3`, `4..10`, `11..25` at `+15` / `+50` / `+150` slots, exact CPMM reserves + `1.25%` fee/side + `65k` lamports tx fees):
  - **Non-Mayhem (`14,950` coins)**: negative across all 9 cells (`dslot 1..3`: `−8.08%` mean / `−3.20%` median at `+15sl`, `−10.84%` / `−3.20%` at `+50sl`, `−16.57%` / `−7.00%` at `+150sl`, win rates `19.4%..24.7%`).
  - **Mayhem (`9,065` coins)**: even worse (`−12.26%` to `−47.80%` mean, `−16.00%` to `−88.52%` median).
  - **Verdict: MEASURED-DEAD in both non-Mayhem and Mayhem regimes.** Creation-window sniping at retail latency (`dslot >= 1`) is structurally negative regardless of Mayhem mode.
- **Buyback fee stream** (`BUYBACK_FEE_RECIPIENTS` / `update_buyback_config` in `pump_fun_idl.json`):
  - **Verdict: NOT CREATOR-ACCESSIBLE.** The 8 `BUYBACK_FEE_RECIPIENTS` are hardcoded protocol-authority accounts (`authority` on `Global`), not creator-owned PDAs, and execute `0` curve trades on the 24h tape. The only creator-owned fee stream is `creator_vault` (30 bps PumpSwap / curve creator fee, already tracked in Strategy #1).
- **Serial-sniper clone** (`50` wallets with `≥10` coins bought and `0` on-chain curve sells):
  - Splits into two populations: (a) slot-0 graduation bundlers (`6A59APPs…` 32/32 graduated, `HT3WHerj…` 23/23 graduated, `med_dslot = 0.0`, `med_rsol = 82.5..85.0 SOL`) that sweep their own launches and exit or hold on PumpSwap, and (b) high-volume retail-slot buyers (`7JCe3GHw…` 779 coins, `7iVCXQn4…` 776 coins, `Em8J3gBW…` 756 coins, `med_dslot = 158..174`) that transfer tokens off-wallet rather than selling directly.
  - Cloning the top 8 non-slot-0 never-selling snipers at `+15` / `+50` / `+150` slots loses across every wallet (`−1.87%` to `−44.80%` mean, `−3.19%` to `−84.62%` median, `9.4%..28.2%` win rate). **Verdict: MEASURED-DEAD for follower cloning.**
---

## The regime panel (the deterministic "what conditions are we in")

Read live, all from on-chain/public data, no discretion:

- **Platform volume** (24h): arms/disarms the creator annuity.
- **Fee config** (PumpSwap PDA): arms/disarms follower LP #4.
- **Riser-share** (trailing migrations): arms/disarms follower LP #5.
- **30-day net accept marks**: arms/disarms crossing-entry #3.
- **Coupling indicator** (X=30→60): the whale-activity regime already
  measured — arms the whale-ride instrument.
- **CPMM fee config** (letsbonk): arms/disarms follower LP #5's fee
  share.

The bot reads the panel each cycle; the operator sees which strategies
are ARMED. Strike = the operator's call, per the standing risk rules.
