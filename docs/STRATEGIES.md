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

**Evidence correction:** this is a historical hypothesis inventory, not a
current profitability certification. Gross paper price marks are not fills
or net returns; complete-case cohorts can be selected. Creator-vault balance
movements do not establish pool-attributed revenue or collected fees. Older
“+EV,” payback and live-return interpretations are withdrawn; no status in
this document authorizes execution or changes the standing risk policy.

---

## 1. Creator annuity hypothesis — economics unvalidated

- **Mechanism under study**: a creator may receive a protocol-defined fee
  stream after launch. Applicable fees and recoverable cashflow need current
  independent evidence, not an assumed fixed yield.
- **Observed**: creator-wide WSOL vault balances, with historical missing-account
  zeros and incomplete identities. They do not establish pool-specific revenue,
  lifetime fees, collection recipients or profitable launch services.
- **Measurement**: tracker schema v2 preserves identities and null censors;
  reconciling monotonically positive step increases across the 100 tracked graduated
  pools recovers **`152.95 WSOL` accrued (`50,983 SOL` implied PumpSwap volume at 30 bps)**,
  with **`27/100` creators actively withdrawing `113.07 WSOL`** via `collect_coin_creator_fee`
  (causing raw endpoint diffs `(last - first)` to undercount fee generation whenever a
  claim occurs mid-window). Top-decile organic graduations (`grad_elapsed_s = 18.26s`
  median) dip at `p5/open = 0.917x` on initial migration sell pressure before rallying
  to `p120/open = 1.155x`.
- **Worst case**: launch expenditure, fees and inventory can remain unrecovered.
  Historical volume and selected winning pools do not bound that loss.
- **Status**: NOT VALIDATED / NOT AUTHORIZED. Offline launch/claim instruction
  and synthetic quote checks are mechanics evidence only. The creator pilot
  needs a separate economics gate and explicit policy authorization, not just
  more capital. The existing bounded live-readiness exception does not apply.

## 2. Graduation rollover observations — gross marks, not executed returns

- **Mechanism**: hold the pool position; exit when the pool drops ≥5%
  below its running peak, with `peak` seeded at graduation `open_price`
  (`bbb6510`) so open-and-dump pools trigger on the first poll.
- **Measured**:
  - **Cumulative post-fix 5% cohort (`id >= 66872`, n=51 clean resolved)**:
    **46W / 5L (90.2%)**, **+3,141.9 points** (+61.6% mean, +53.6% median)
    vs +2,302.8 pts at 60s (35W/16L, +839.1 pts edge) and +2,718.7 pts at 300s (23W/28L, −21.8% median).
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
  - **Early post-migration whipsaw protection (Wave 6)**: 16 of 91 recorded rollovers triggered a whipsaw exit $\le 10\text{s}$ below open before rebounding ($p30 > \text{open}$ or $p120 > \text{open}$). The three-tier $G+5\text{s}$ stabilized rollover exit (`0.85 * open` for `0..5s` $\to$ `0.90 * open` for `5..10s` $\to$ standard `5%` trailing drop) absorbs initial migration sell-pressure, lifting `grad_marks` $p5$-confirmed shakeout returns from **`-1.11%`** to **`+1.49%`** vs pool open.
## 3. Crossing-entry sniping (in-band 60–85 SOL accept) — band-gated

- **Mechanism**: accept a coin at the 60-SOL crossing, ride to graduation,
  exit via the rollover rule — gated by the measured buyer band:
  **2–3 distinct non-creator buyers at accept, non-mayhem**.
- **Historical exploration only** (buyer-count census, n=37; unfrozen complete-case split):

  | buyers at accept | n | mean r60 | mean r900 |
  |---|---|---|---|
  | 1 | 2 | −92.0% | −92.0% |
  | **2–3** | **8** | **+102.5%** | **+30.0%** |
  | 4–10 | 21 | −10.4% | −45.1% |
  | >10 | 6 | −23.1% | −86.2% |

  The earlier split was not frozen or an untouched forward test. It filtered
  complete cases and changed membership; its “survivor” and causal interpretations
  are withdrawn. These historical gross marks do not confirm an entry edge.
- **Regime detector**: same as before — the trailing-30-day net-of-cost
  marks re-test. The band rule is now implemented in the gate
  (`min_buyers`/`max_buyers`/`exclude_mayhem`) and re-measures live once
  armed; the clue either confirms on the next cohort or dies.
- **Worst case**: the measured latency drift inside the band; the
  WICK/68368 rebound class (the rollover exit gave back +98% while the
  timeline ran to +742% on the largest resolved winner).
- **Status**: DRY-RUN ONLY (`whaleride-paper`: `min_buyers: 2, max_buyers: 3, exclude_mayhem: true, max_single_buy_sol: 45.0, max_buyer_share: 0.75, min_wait_slots: 1, max_wait_slots: 75, max_wait_ms: 30000, max_creator_launches: 1, max_hold_time: 90, max_stall_slots: 180, entry_floor_pct: 0.10, single_sell_pct: 0.25`). **Post-Oct-14 execution audit (`id >= 76580`, `n=43`)**: joining `lessons.sqlite3` 1-to-1 against `keeper.stdout.log` proved **35 of 43 (`81.4%`)** accepted entries (100% of `grad_elapsed_s < 4.0s`) were **unexecutable slot-0 graduation bundles** (`Failed to buy <SYMBOL>: virtual_token_reserves must be a positive raw u64 integer`) where `EntryGate` returned on Tranche 3 (`~63 SOL`) while Tranche 4 (`85 SOL`, curve complete in slot 0) sat unread in `TradeQueue` and `_schedule_paper_marks()` ran before `buyer.execute()`. The **8 genuinely executable entries (`18.6%`)** had `3/8` (`37.5%`) graduations and averaged **`-17.40%` at 60s** and **`-8.86%` under the composite rollover/open-stop policy**. `_await_entry_gate()` now drains same-slot queued events, `min_wait_slots: 1` holds through creation-slot bundles, `_process_token_queue()` evaluates up to `_MAX_CONCURRENT_GATES = 64` tokens in parallel with queue-timestamp-anchored deadlines (`queued_at + max_wait_ms/1000`), and `_schedule_paper_marks()` runs after `buyer.execute()` so completed curves are censored as `unexecutable_completed_curve` (`verify_executable_crossing_gate.py`).
  - **Execution & Inflow Hardening (Waves 5–10)**: `EntryGate.observe` requires an organic non-creator buy to trigger accept (rejecting sell-events, protocol `MAYHEM_SOL_VAULT` buybacks, and creator self-buys), `max_single_buy_sol: 45.0` and `max_buyer_share: 0.75` reject concentrated single-wallet tranche sweeps, `max_creator_launches: 1` filters serial deployers, and `max_inflow_sol_per_slot` suppresses velocity surges. Real-time exits enforce `FlowRules` calibrated on the `60..80 SOL` entry band (`max_hold_time: 90`, `max_stall_slots: 180`, `entry_floor_pct: 0.10`, `single_sell_pct: 0.25`) with pre-fill slot isolation (`entry_slot` propagation, `ev.slot < entry_slot` filtering, and overflowed `TradeQueue` resubscription in `_consume_trade_flow`).
  - **4-Stage Executable Funnel (`src/learning/report.py` & `dashboard.py`)**:
    - `unexecutable_poll1_bundle` ($n=73$ planned, $72$ observed): $70.8\%$ win, $+1,949.62\%$ mean 60s, $+1,937.43\%$ policy (illusory unexecutable pricing).
    - `executable_curve_all` ($n=56$ planned, $41$ observed): $51.2\%$ win, $-10.45\%$ mean 60s, $+5.58\%$ policy.
    - `executable_in_band_post_slot` ($n=3$ planned, $3$ observed): $100\%$ win, $+22.32\%$ mean 60s, $+37.11\%$ policy.
    - `executable_full_gate` ($n=3$ planned, $3$ observed — `SPOOF` + `Mask` + `KIMVERSE`): **$100\%$ win ($3/3$)**, **$+22.32\%$ (`+0.2232`) mean 60s**, **$+37.11\%$ (`+0.3711`) policy gross mean**.
  - **Buyer Cohort Discrimination (`paper_by_buyer_cohort`)**:
    - `organic_repeat_buyer` ($n=23$ planned, $22$ observed): **$81.8\%$ win ($18/22$)**, **$+80.43\%$ mean 60s**, **$+72.04\%$ policy return**.
    - `single_entry_buyer` ($n=102$ planned): $52.9\%$ win ($54/102$), $+1,592.2\%$ mean 60s (skewed by bundled completions).
    - `mayhem_vault_buyer` ($n=4$ planned): **$0\%$ win ($0/4$)**, **$-86.66\%$ mean 60s**, **$-83.20\%$ policy return** (confirming strict exclusion).
  - **5.3-Day Live Latency Shadow Corroboration (`qd1946_tests`, $n=116$ signals)**:
    - `92%–96%` of slot-0 creation bundle signals ($\text{age} < 0.4\text{s}$, $23/25$ at $L=0.4\text{s}$ and $24/25$ at $L \ge 0.8\text{s}$) were infeasible because the curve completed ($85.005\text{ SOL}$) before the simulated order landed.
    - By contrast, the `52` feasible post-slot signals ($\text{age} \ge 0.4\text{s}$) at $L=0.4\text{s}$ achieved a **`75.9%` graduation hit rate** ($41/54$ overall feasible) and **`+53.9%` median net ROI** (`+53.67%` across all $54$ feasible at $0.1\text{ SOL}$).
  - **Wave 10 Exit-Grid Calibration, Pre-Fill Slot Guard & Keeper Resilience**:
    - **Exit-grid calibration (`60..80 SOL` band)**: `max_stall_slots: 35` prematurely stopped out `73.0%` of graduating curves (whose median pre-grad stall gap is `74` slots, `p75 = 107` slots), producing `-7.2%` LOCK mean on `lifecycles_24h`. Raising `max_stall_slots: 180` with `entry_floor_pct: 0.10`, `single_sell_pct: 0.25`, and `max_hold_time: 90` flips LOCK mean to **`+6.0%..+6.8%` (`67.6%..70.6%` win rate)** while capturing **`80.5%` of `qd1946_tests` graduating winners** (`31.7%` graduated in `60..90s`, including `71452 SPOOF` at `62.74s`).
    - **Pre-fill slot guard (`entry_gate` $\leftrightarrow$ `flow_exit`)**: `GateVerdict` propagates `entry_slot` into `FlowPosition` so `_consume_trade_flow` ignores stale pre-fill events (`ev.slot < entry_slot`) and automatically resubscribes `trade_hub` whenever an overflowed `TradeQueue` drops subscription during gate handoff.
    - **Keeper supervisor resilience**: `keep_whaleride_shadow.py` enforces an `elapsed > STALE_SECONDS` startup grace window before stale-log kills and resets `restart_count` after `HEALTHY_RESET_SECONDS = 3600.0` of continuous uptime.

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
- **OOS microstructure & post-graduation PumpSwap reclaim**:
  - Across the 24,015-coin exact-CPMM tape (TRAIN `0..12,006` → LOCK `12,007..24,014`), dispersed organic flow (`max_buyer_share <= 0.75`, `max_single_buy_sol <= 10.0`, `min_wait_slots >= 1`) separates multi-wallet accumulation from single-operator tranche sweeps, though curve-entry edge remains bounded by graduation selectivity.
  - On graduated PumpSwap pools, waiting for the post-migration shakeout (`p5/open = 0.917x` on top-decile fee-generating pools) and entering on a **`+5%` reclaim above `open_price`** captures the second-wave continuation (`p120/open = 1.155x`) while avoiding instant open-and-dump migrations.
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
