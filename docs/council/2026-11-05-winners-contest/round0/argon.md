## A. Recoverability of Winner Edges and Analytical Traps

Public ledgers recover **execution mechanisms** (category, timing, price band, maker/taker role, multi-leg structure) far better than **exogenous signal weights**.

**Recoverable classes:**
- **Endogenous structural edges:** Multi-outcome `CONVERSION`/`MERGE`/`SPLIT` completeness arbitrage and strike/calendar monotonicity trades, since mispricings sit inside public prices (prior).
- **Lifecycle and behavioral edges:** Post-outcome carry during UMA oracle liveness (prior), longshot-NO harvesting, new-market sniping, and contrarian fades of retail sweeps.
- **Subsidy capture:** Two-sided quoting funded by `maker rebates` or `liquidity-reward income` is explicit in decomposed P&L, though sub-100ms cancel priority is infrastructural (prior).

**Unrecoverable classes (and why):**
- **Fast feeds and proprietary models:** Sub-second stadium/audio/wire feeds, post-processed NWP weather ensembles, and custom sports/polling models (prior). Trade logs record *fills* and post-trade drift, hiding external features, resting quotes, and cancel ratios (prior); Kalshi trackers also lack fill-level timestamps.
- **Off-exchange books:** Visible trades may hedge private Deribit, Pinnacle, or Betfair positions (prior).

**Seven traps:**
1. **Survivorship:** Across >100k wallets (prior), high-variance tail or concentrated macro bettors reach top-250 boards by luck.
2. **Selection on outcomes:** Ranking discovery winners induces Winner's Curse; conditioning on resolved outcomes confuses luck with calibration.
3. **Off-platform hedges:** Apparent winners may lose more hedging elsewhere, while cross-venue arbitrageurs look unprofitable on one leg (prior).
4. **Rebates/rewards in P&L:** Headline P&L mixes alpha with `maker rebates`, `liquidity-reward income`, `yield income`, and `fee refunds`.
5. **Account splitting:** Quants shard orders or split YES/NO legs across wallets to hide scale and evade copy-bots (prior).
6. **Copy-trading:** Polygon copy-bots (prior) inflate breadth and push prices post-fill, overstating leader markouts while followers bleed spread.
7. **Wash/airdrop farming:** Sybil self-fills and 99c churn inflate volume and win rates for expected token/reward distributions (prior).

## B. Pre-Registered Edge Hypotheses

*Format: Mechanism. Sig (thresholds; D#=Section D). Prev (top-250; ~950 consistent cohort). Mag (ROI/trade, win rate, +weeks share). Rep (~1s Kalshi/PM-US replicability & method). Falsify.*

1. **NegRisk Completeness Arbitrage:** Retail single-leg flow pushes sibling-outcome sums off 100c (prior). *Sig:* `CONVERSION`/`MERGE`/`SPLIT` ≥20% activity, `combo`+`market P&L` ≥85%, D1 sibling hedge. *Prev:* 15–25; 35–60. *Mag:* +0.6–1.8%, 90%, >85%. *Rep:* Yes; 1s ask-sum (<98.5c) bracket sweeper. *Falsify:* Clustered hedged P&L ≤0.
2. **Post-Event Resolution Carry ("Bonding"):** Winners sell at 96.5–99.8c post-event during UMA liveness (prior) to recycle capital. *Sig:* `buy-price` 96.5–99.8c (≥70%), final-5%/post-`end` timing, `hold-to-resolution` ≥85%, `market P&L` ≥85%. *Prev:* 15–30; 60–110. *Mag:* +0.6–2.2%, >98.5%, >85%. *Rep:* Yes; 1s AP/NWS/boxscore confirmation poller pre-settlement. *Falsify:* Clustered P&L ≤0 including reversals.
3. **Longshot-NO Tail Harvesting:** Retail overpays for 3–12c lottery skew (prior); lockup deters institutions. *Sig:* NO buys at 88–96.4c (≥65%), >24h pre-`end`, `hold-to-resolution` ≥75%, ≥30 markets. *Prev:* 10–20; 70–130. *Mag:* +1.5–4%, 93%, >80%. *Rep:* Yes; 1s base-rate NO screener with correlation caps. *Falsify:* 88–96.4c D5 `brier_skill_excess` ≤0.
4. **Deadline Hazard-Decay ("Nothing Happens" NO):** Headline-scared retail overprices date-bounded YES events, ignoring Poisson decay (prior). *Sig:* `geopolitics`+`politics` ≥65%, NO ≥70%, `buy-price` 55–87.9c, >12h pre-`end`, `brier_skill_excess` >+2.5%. *Prev:* 10–20; 40–75. *Mag:* +3–7%, 76%, 78%. *Rep:* Yes; 1s spike-fader buying NO on deadline contracts. *Falsify:* Clustered P&L ≤0 in tail cascades.
5. **New-Market Creation Sniping:** Early liquidity misreads resolution rules or base rates on new listings (prior). *Sig:* ≥45% trades <60m post-`creation`, `buy-price` 15–85c, `both-outcomes` <25%, D2 `markout_3600s` >0. *Prev:* 5–12; 20–40. *Mag:* +5–12%, 65%, 74%. *Rep:* Yes; 1s listing poller + rule/base-rate parser. *Falsify:* <60m entries net ≤0 P&L.
6. **Strike-Ladder & Calendar Monotonicity Arb:** Single-strike retail sweeps invert dominance across adjacent thresholds/dates (prior). *Sig:* Non-NegRisk `crypto threshold`/`macro`/`politics`, ≥40% trades with D1 `has_sibling_hedge_120s`, `round-trip` ≥50%. *Prev:* 8–15; 25–45. *Mag:* +1.5–3.5%, 72%, >80%. *Rep:* Yes; 1s dominance scanner on Kalshi strike ladders (prior). *Falsify:* Paired net P&L ≤0.
7. **Rebate-Subsidized Short-Horizon Crypto MM:** Retail pays taker spread on 5/15m crypto binaries; rebates subsidize makers (brief; prior). *Sig:* `crypto up/down` ≥75%, `both-outcomes` ≥70%, `maker share` ≥75%, `maker rebates` ≥10% net P&L. *Prev:* 25–45; 80–150. *Mag:* +0.05–0.15%, 53%, 85%. *Rep:* No at 1s; requires <50ms CEX cancel latency (prior). *Falsify:* Total net P&L ≤0.
8. **Liquidity-Reward Passive Mid-Book Farming:** Platform treasury pays quadratic spread subsidies to resting quotes (prior). *Sig:* `liquidity-reward`+`LP P&L` ≥35% net P&L, `maker share` ≥80%, `both-outcomes` ≥50%, `crypto up/down` <30%. *Prev:* 15–30; 50–90. *Mag:* +0.2–0.8%, 50%, 80%. *Rep:* Partial; 1s passive quoter on incentive-eligible books only (prior). *Falsify:* Adverse selection drives net P&L ≤0.
9. **Isolated Retail-Sweep Mean-Reversion:** Whales dislocate thin non-news books 6–15c temporarily (prior). *Sig:* Non-crypto/non-in-play ≥70%, D2 `pre_move_300s` ≤−5c, `round-trip` ≥60% (<4h), `both-outcomes` <30%. *Prev:* 10–20; 35–65. *Mag:* +2.5–6%, 62%, 80%. *Rep:* Yes; 1s impact detector fading isolated ≥6c sweeps. *Falsify:* D2 `markout_3600s` ≤0.
10. **Pre-Game Sharp Sportsbook Pinning:** Fan bias skews pre-game prices 2–5% off vig-free Pinnacle/Circa lines (prior). *Sig:* `sports pre-game` ≥70%, >15m pre-`event_start_ts` (D1), `buy-price` 20–80c, `both-outcomes` <25%, `brier_skill_excess` >+2%. *Prev:* 20–35; 50–90. *Mag:* +2–4.5%, 56%, 76%. *Rep:* Yes; 1s sharp-odds API poller taking >1.5% post-fee edges. *Falsify:* Post-fee `brier_skill_excess` ≤0.
11. **In-Play Sports Fast-Feed & State Sniping:** Stream-delayed fans misprice live score/clock Markov states (prior). *Sig:* `sports in-play` ≥70%, D2 `markout_60s` ≥+2.5c, `round-trip` ≥45%. *Prev:* 25–45; 40–75. *Mag:* +1.5–4%, 60%, 80%. *Rep:* Partial; 1s timeout Markov/Pinnacle sync only (not court-siding). *Falsify:* Net holdout P&L ≤0.
12. **Crypto Options-Surface Digital Mispricing:** Retail prices multi-day crypto targets on sentiment, ignoring Deribit vol/skew (prior). *Sig:* `crypto threshold` ≥65%, `both-outcomes` <30%, >2h hold, `buy-price` 10–90c, `brier_skill_excess` >+2%. *Prev:* 10–18; 25–50. *Mag:* +2.5–6%, 62%, 76%. *Rep:* Yes; 1s Deribit digital/barrier pricer (prior) trading >3c gaps. *Falsify:* Expiry-clustered P&L ≤0.
13. **Ensemble Weather & Station-Rounding Alpha:** Retail uses consumer apps, missing ECMWF/HRRR spreads, C-to-F rounding knots, and 5m METAR highs (prior). *Sig:* `weather` ≥60%, `both-outcomes` <25%, `brier_skill_excess` >+3%. *Prev:* 3–8; 25–50. *Mag:* +4–9%, 68%, >82%. *Rep:* Yes; 1s NOAA/ECMWF+METAR bot (prior) on Kalshi temperature ladders. *Falsify:* Clustered `weather` P&L ≤0.
14. **Pre-Event "Mentions" Base-Rate Calibration:** Retail overpays for buzzwords without speaker transcript base rates (prior). *Sig:* `mentions` ≥60%, >1h pre-`event_start_ts`, NO ≥65%, `buy-price` 55–95c, `hold-to-resolution` ≥75%. *Prev:* 4–10; 20–45. *Mag:* +3.5–8%, 78%, >80%. *Rep:* Yes; transcript Poisson scorer trading Kalshi mentions pre-event at 1s. *Falsify:* Pre-event `mentions` `brier_skill_excess` ≤0.
15. **Breaking-Wire, Docket & Live-Audio Sniping:** Stale resting quotes get sniped on gov drops, PACER filings, or live audio (prior). *Sig:* `maker share` <20%, non-sports/non-crypto ≥70%, D2 `markout_60s` ≥+4c. *Prev:* 15–30; 20–40. *Mag:* +4–12%, 72%, 75%. *Rep:* Partial; 1s PACER/election-drop/Whisper STT polling (prior) on slower books. *Falsify:* High-markout takers net ≤0.
16. **Pre-Release Macro Bracket Calibration:** Retail crowds consensus/tail macro buckets vs SOFR swaps and component nowcasts (prior). *Sig:* `macro` ≥60%, >30m pre-`event_start_ts`, `buy-price` 15–85c, `both-outcomes` <30%. *Prev:* 8–15; 20–40. *Mag:* +3–7%, 63%, 70%. *Rep:* Yes; 1s FedWatch/nowcast model on Kalshi CPI/Fed/jobs brackets (prior). *Falsify:* Clustered `macro` P&L ≤0.
17. **Awards & Cultural Precursor Modeling:** Fans bet name recognition over precursor/critic/Rotten-Tomatoes priors (prior). *Sig:* `awards`+`other` ≥55%, `buy-price` 15–85c, `both-outcomes` <20%, `brier_skill_excess` >+3%. *Prev:* 5–12; 20–35. *Mag:* +4–10%, 66%, 74%. *Rep:* Yes; 1s Rotten-Tomatoes/GoldDerby scraper (prior) on Kalshi/PM-US markets. *Falsify:* Clustered `awards`/`other` P&L ≤0.
18. **Smart-Wallet Shadowing & Cross-Venue Lead-Lag:** Polygon exposes sharp fills in ~2s; slower books reprice over 10–120s (prior). *Sig:* `maker share` <25%, D4 `followed_wallet_share` ≥35% (2–60s lag). *Prev:* 5–12; 30–60. *Mag:* +1.5–3.5%, 59%, 76%. *Rep:* Yes; ~1s Kalshi/PM-US execution off Polymarket Polygon fills (prior). *Falsify:* Lagged fills net ≤0.

## C. Detecting the Hidden Multi-Account Quant Entity

**Likely edge class and signature:** Sharding hurts market-maker inventory netting and `MERGE` efficiency, so a hidden fleet most likely runs **capacity-constrained directional calibration**—pre-game sports (#10), weather (#13), longshot-NO/deadline carry (#3, #4), or bonding (#2)—slicing $2k–$10k signals into $50–$500 clips across 10–40 wallets to evade Polygon copy-bots and MM quote-widening (prior). Individual wallets rank #150–#2,500 with `market P&L` >85%, +2–5% calibration excess, and ≥80% positive weeks.

**Detection (on-chain + ≥2 independent behavioral links):**
1. **On-chain graph (prior):** Shared 1–2 hop funders, gas relayers, Safe owners, or **shared CEX withdrawal sinks** (separately funded Sybils often sweep profits to one exchange deposit address).
2. **Symmetric slicing / partitioning:** Co-entry in identical `(market_id, outcome)` within 0.2–15s with symmetric lead order (`lead_symmetry ≈ 0.5`, zero self-fills), or hash-partitioned market subsets with matching sizing distributions.
3. **Shared outage clock & sizing grammar (prior):** Simultaneous inactivity windows during server restarts/RPC drops (`outage_jaccard`), plus identical fractional-Kelly share quantization.

**False positives:**
- **Shared vendor bots:** Uncorrelated outages, disjoint funding/withdrawal sinks, divergent sizing, mixed P&L.
- **Copy-traders:** Asymmetric lag (`lead_symmetry > 0.95`, >2s post-block (prior)), worse fills, taker-only.
- **Market makers:** `both-outcomes` >60%, heavy `MERGE`/rebate share.
- **Airdrop/Sybil wash farmers:** Circular counterparty loops, 99c churn, near-zero net `market P&L` (prior).

## D. Exact Collector Data Requests

1. **Event-Group, Start & Ladder Context (per trade):** `event_id` grouping sibling/threshold markets; `event_start_ts` (kickoff/speech/release timestamp from metadata (prior)); `event_prob_sum_ts = sum_i(last_price_YES_i)` across active siblings at `ts`; `has_sibling_hedge_120s` (opposing sibling trade within ±120s).
2. **Signed Markouts (per trade):** `pre_move_300s = s*(p_t - p_{t-300s})`, `markout_60s = s*(p_{t+60s} - fill_price)`, `markout_300s`, and `markout_3600s`, with `s = +1` (buy) or `−1` (sell).
3. **On-Chain Attribution (per trade, Polygon `OrderFilled` logs (prior)):** `is_maker` (`wallet == maker`), `counterparty_wallet`, `fee_usdc`, and `self_cluster_fill` (counterparty shares 2-hop funder/sink).
4. **Copy-Lag (per trade & wallet):** `min_alpha_lag_s` = seconds in `(0,60]` since a top-100 discovery-market-P&L non-MM wallet (`both_outcomes < 0.35`) traded identical `(market_id, outcome, side)`; `followed_wallet_share` = taker share with `min_alpha_lag_s <= 60`.
5. **Sweet-Spot & Calibration (per wallet, discovery/holdout split):** `positive_iso_week_share = weeks(net_pnl > 0)/active_weeks` (total and market-only); `median_trade_usdc`; `brier_skill_excess = mean(resolved_outcome - effective_buy_price - fee_per_share)`.
6. **Sybil Forensics (wallet & pair):** `funder_2hop_root`, `withdrawal_sink_addresses` (USDC outflow EOAs/CEX deposit sinks (prior)), `outage_jaccard` (overlap of ≥30m inactive gaps during active UTC hours), and `co_entry_15s_count` with `lead_symmetry = count(A_before_B)/co_entry_15s_count` across ≥10 shared markets.
