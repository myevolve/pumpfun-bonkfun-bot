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
- **Status**: MEASURED-LIVE — blocked on capital + explicit approval.
  The bot's create plumbing exists.

## 2. Rollover condition-driven exit (graduated pool)

- **Mechanism**: hold the pool position; exit at the first poll below
  the running peak (no threshold — the 2%/5% trailing variants measured
  dead; poll noise produces sub-2% dips).
- **Measured**: beats the fixed 900s timeline on 12/13 resolved coins,
  +1,865 points aggregate; the WICK rebound class is the quantified cost
  (fire at +13%, pool ran to +152%).
- **Regime detector**: none needed — it is per-position and
  self-arming. Requires an ENTRY to exit from (any of the below, or an
  operator-directed position).
- **Worst case**: the WICK giveback (~139 points on one coin of 13) and
  the instant-collapse class where no exit rule helps (dump between
  2-second polls).
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
- **Status**: MEASURED-CONDITIONAL — gate support shipped; arming is the
  operator's call.

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

## Untested hypotheses — the widening net (queued censuses)

Each enters the inventory only with a measured conditional verdict:

- **Leaderboard winner census**: profile the top-100 consistent
  winners' behavior (entry timing vs coin age, hold time, coin
  selection, position size) from the same on-chain tape the whale
  census used. Their pattern, if mechanizable, becomes candidate
  entries.
- **Creation-window re-test by regime**: the Sep 4–5 tape measured
  first-10-slot entries negative OOS — in one mayhem burst. The
  verdict is regime-conditional; re-test in the current regime.
- **Buyback fee stream**: pump's BuybackFeeRecipient — an unexplored
  supply-side stream.
- **Serial-sniper clone**: the 15-coin sniper who never sells (off-chain
  settlement — likely a paid service). If the entry pattern is visible
  on-chain, the entry half may be cloneable.

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
