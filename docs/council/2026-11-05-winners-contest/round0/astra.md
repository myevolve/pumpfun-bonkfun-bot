**A. Can we reverse-engineer the winners?**

Only partly. On Polymarket we can see what a wallet did. We usually cannot see why it did it.

*Recoverable from public per-wallet data (prior):* edges whose profit shows up in fill prices, timing and settlement. These are:
- rebate and two-sided market making;
- complete-set and neg-risk arbitrage;
- near-certain "bond" harvesting;
- buying after the outcome is known but before resolution;
- fading longshots and deadline decay;
- reward and yield farming;
- calibration specialists in narrow categories such as weather, mentions and crypto thresholds.

The test for each is whether a computable selector shows positive markout or calibration in the holdout.

*Not recoverable:*
- **Information edges.** Private sources, scouts, proprietary weather or options models. We see the outputs, not the inputs.
- **Off-platform hedges.** Kalshi legs, sportsbooks, perps. The Polymarket leg looks like noise and only the combined book is profitable.
- **Latency edges.** We can see their signature but cannot copy them at 1 s.
- **Kalshi.** Only settled aggregates; no fills, no timestamps.
- **Polymarket US.** Invisible.

*Traps:*
1. **Survivorship and selection on outcomes.** Thousands of concentrated directional bettors produce a P&L top 250 dominated by variance, such as large election bets. We must re-rank on discovery data only, test in the holdout, and compare against random wallets matched on size and category.
2. **Rebates, rewards, yield and referral income inside P&L.** Strip them out; a "winner" can lose money on trades.
3. **Off-platform hedges.** Wallets that are flat or losing can be the profitable half of an arbitrage.
4. **Win rate.** A 99% win rate with negative expected value is normal for bond buyers. Use ROI with clustered CIs, not win rate.
5. **Correlated markets.** One event can drive dozens of markets. Cluster bootstraps by event, not by market.
6. **Account splitting.** It hides concentration and makes the top 250 a sample of fragments.
7. **Copy-traders.** Followers who lag the leader by seconds look like an entity and inherit a degraded edge.
8. **Wash trading and airdrop farming.** Volume with roughly zero P&L, often self-crossing.
9. **Accounting artifacts.** Merges, splits and conversions recorded as trades distort win rates and price buckets. Unrealized marks inflate 30-day boards.
10. **Regime change.** Sports seasons, crypto volatility and fee or rebate schedule changes between discovery and holdout.

**B. Pre-registered hypotheses**

1. **Two-sided crypto rebate market maker.**
   - *Mechanism:* takers pay fees and spread; rebates subsidise quoting.
   - *Signature:* maker share ≥70%; both-outcomes share ≥40% of markets; combined YES+NO cost per market ≤0.995; rebates ≥15% of net; crypto up/down ≥70% of notional.
   - *Prevalence:* 15–30 of top 250; ~25% of the consistent cohort.
   - *Magnitude:* 0.03–0.3% of volume; ≥85% positive weeks.
   - *Replication:* Kalshi or Polymarket US maker programs; 1 s is marginal.
   - *Falsification:* net after fees ≤0 in the holdout.

2. **Complete-set merge arbitrage.**
   - *Mechanism:* fragmented books briefly price YES+NO below $1.
   - *Signature:* MERGE notional ≥20% of buys; matched cost ≤0.99; held <1 h.
   - *Prevalence:* 5–15; *magnitude:* 0.5–2% per set, ~100% win rate.
   - *Replication:* Kalshi, by holding both sides to settlement.
   - *Falsification:* average matched cost ≥1.00 after fees.

3. **Neg-risk multi-outcome arbitrage.**
   - *Signature:* CONVERSION events; buys covering ≥80% of an event's outcomes within 60 s; total cost <1.
   - *Prevalence:* 3–10; *magnitude:* 0.5–3%.
   - *Replication:* Kalshi multi-strike events.
   - *Falsification:* summed entry cost ≥1.

4. **Near-certain "bond" harvesting.**
   - *Mechanism:* capital lock-up and dispute risk deter others.
   - *Signature:* ≥70% of notional at ≥0.95; held to resolution; entered <72 h before resolution.
   - *Prevalence:* 20–40; ~30% of the consistent cohort.
   - *Magnitude:* 1–4% per trade; win rate 97–99.5%; ~80% positive weeks; fat left tail.
   - *Replication:* yes, on both venues.
   - *Falsification:* realized frequency ≤ entry price plus fees, per bucket.

5. **Post-event settlement-lag sniping.**
   - *Mechanism:* markets stay open after the outcome is knowable (final whistle, candle close, speech end).
   - *Signature:* fills 0–600 s after the event-end time (`event_end_ts`, D2), at 0.80–0.99; win rate ≥97%.
   - *Prevalence:* 10–25; *magnitude:* 2–10% per trade.
   - *Replication:* Polymarket US (Kalshi usually halts trading at the event).
   - *Falsification:* ROI ≤0 for these fills.

6. **Longshot fading.**
   - *Mechanism:* retail overpays for lottery tickets.
   - *Signature:* ≥60% of notional is NO at ≥0.90 against YES priced 0.02–0.10; realized YES frequency ≤0.7× price.
   - *Prevalence:* 15–30; *magnitude:* 1–3%; ~75% positive weeks.
   - *Replication:* yes.
   - *Falsification:* calibration gap ≥0 with a CI.

7. **Deadline decay.**
   - *Mechanism:* "by date X" YES prices decay too slowly.
   - *Signature:* NO buys with ≤14 days to deadline at 0.80–0.97, in politics or geopolitics.
   - *Prevalence:* 5–15; *magnitude:* 2–5%.
   - *Replication:* yes.
   - *Falsification:* ROI ≤0.

8. **Weather model specialists.**
   - *Mechanism:* ensemble forecasts beat retail.
   - *Signature:* ≥50% weather; entries clustered within 2 h after the 00z/06z/12z/18z model releases; Brier score ≥0.02 better than price at entry.
   - *Prevalence:* 3–10 at the top, more in the deeper cohort.
   - *Magnitude:* 5–15%; ~80% positive weeks.
   - *Replication:* high (Kalshi daily-high-temperature markets); 1 s is fine.
   - *Falsification:* Brier ≥ market.

9. **Mentions specialists.**
   - *Mechanism:* transcript base rates and live listening.
   - *Signature:* ≥50% mentions; Brier ≥0.02 better than price; a share of fills during the speech window.
   - *Prevalence:* 5–15; *magnitude:* 5–20%.
   - *Replication:* high on Kalshi.
   - *Falsification:* no Brier gain.

10. **Sports pre-game closing-line value.**
    - *Mechanism:* prediction-market prices lag sharp books.
    - *Signature:* mean (closing price − entry price) ≥ +1.5 c over ≥200 trades; sports pre-game ≥60%.
    - *Prevalence:* 15–30; *magnitude:* 2–4%; ~70% positive weeks.
    - *Replication:* high; quote against devigged sharp lines.
    - *Falsification:* closing-line value ≤0.

11. **Sports in-play feed latency.**
    - *Signature:* taker fills ≤10 s after score events; 1-minute markout ≥ +3 c.
    - *Prevalence:* 5–15; *magnitude:* 3–8%.
    - *Replication:* no at 1 s.
    - *Falsification:* markout ≤0.

12. **Crypto threshold markets priced off the options surface.**
    - *Signature:* ≥50% crypto threshold; positions on several strikes; entry price deviates ≥3 c from a lognormal model using Deribit implied volatility.
    - *Prevalence:* 5–15; *magnitude:* 2–6%.
    - *Replication:* high (Kalshi BTC and ETH range markets).
    - *Falsification:* model-sign trades ROI ≤0.

13. **Crypto up/down taker latency.**
    - *Signature:* taker fills in the last 120 s; direction matches the 5 s Binance return ≥85% of the time.
    - *Prevalence:* fading, 3–10.
    - *Replication:* no.
    - *Falsification:* ROI after fees ≤0.

14. **Liquidity-reward farming.**
    - *Signature:* reward income ≥50% of net; |market P&L| ≤ reward income; tight two-sided resting quotes.
    - *Prevalence:* 10–20; ≥90% positive weeks; low ROI.
    - *Replication:* Kalshi liquidity incentives.
    - *Falsification:* net ex-rewards so negative that net ≤0.

15. **Cross-venue arbitrage (one visible leg).**
    - *Signature:* entries where Polymarket diverges ≥3 c from Kalshi's mid on the same event; weekly P&L flat or slightly negative while volume is high.
    - *Prevalence:* 5–15.
    - *Replication:* the Kalshi leg itself.
    - *Falsification:* entries uncorrelated with the dislocation.

16. **News first-mover.**
    - *Signature:* fills 0–10 min before a ≥10 c move; 1-hour markout ≥ +4 c.
    - *Prevalence:* 5–15; lumpy.
    - *Replication:* low.
    - *Falsification:* markout ≤0.

17. **Overreaction fade.**
    - *Signature:* buys against a ≥15 c move in ≤1 h in politics or geopolitics; 24-hour markout ≥ +2 c.
    - *Prevalence:* 3–10.
    - *Replication:* yes.
    - *Falsification:* markout ≤0.

18. **Resolution-rules lawyering.**
    - *Signature:* positions in markets with clarifications, disputes or UMA escalation; side matches the literal resolution.
    - *Prevalence:* 2–6; rare, high ROI.
    - *Replication:* partial.
    - *Falsification:* ROI ≤0.

19. **Stale-order sweeping in thin, long-dated markets.**
    - *Signature:* taker fills ≥5 c from the prior mid within 5 min after a related market reprices; positive markout.
    - *Prevalence:* 5–10.
    - *Replication:* yes.
    - *Falsification:* markout ≤0.

20. **Holding-yield carry.**
    - *Signature:* yield income ≥30% of net; long-dated high-probability positions.
    - *Prevalence:* 2–8; small, very consistent.
    - *Replication:* uncertain.
    - *Falsification:* net ex-yield <0 with yield insufficient.

**C. Detecting a hidden multi-account entity**

*On-chain links:*
- a common USDC deposit source or CEX deposit address;
- a common MATIC gas funder;
- shared withdrawal destinations;
- Safe owner or proxy deployer;
- creation blocks close together.

*Behavioral links (need ≥2 independent types):*
- co-trading the same markets within ≤5 s;
- identical size grids or price ladders;
- matching active-hour and sleep profiles;
- category-mix cosine ≥0.9;
- daily P&L correlation ≥0.5;
- **near-zero mutual counterparty fills**, i.e. siblings avoid crossing each other;
- **complementary market partitioning**, i.e. siblings split markets instead of all trading the same ones.

*Likely signature (prior):*
- 5–40 wallets, each with a $2–30k bankroll, each below leaderboard visibility;
- individually ~65–75% positive weeks, but the aggregated cluster ≥85%.

*Likely edge class:* capacity-limited and calibration-based, such as weather, mentions, bond harvesting, settlement-lag sniping or reward farming. The motive for splitting is to avoid copy-traders and per-wallet reward caps; on Kalshi, also per-member position limits.

*False positives:*
- **Shared vendor bots:** same code, but different funders and uncorrelated bankroll flows.
- **Copy-traders:** consistent lag, and they trade the same markets as the leader rather than splitting them.
- **Market makers:** many wallets per firm, but driven by rebates.
- **Sybil and airdrop farmers:** high volume, ~0 P&L, self-crossing.
- **Exchange omnibus funding:** a common funder that is a CEX hot wallet.

**D. Data requests**

1. **Fill table.** `tx_hash, log_index, block_ts, wallet, counterparty_wallet, role{maker,taker}, token_id, condition_id, outcome_idx, side, price, size, fee_paid, rebate_attrib`.
2. **Market timing.** Per market: `event_end_ts` (the real-world moment the outcome becomes knowable, from the source schedule), `trading_close_ts`, `uma_proposal_ts`, `resolution_ts`, `dispute_flag`, `clarification_count`, `neg_risk_event_id`. Per fill: `secs_after_event_end = block_ts − event_end_ts`.
3. **Markouts.** For each fill, the volume-weighted midpoint at +10 s, +1 m, +1 h, +24 h and at trading close, signed by side. Close-line value: `clv = close_mid − price` for a buy, and `price − close_mid` for a sell.
4. **Book at fill.** Best bid, best ask and depth to 5 c at the pre-fill block; `dist_from_mid = price − mid`.
5. **Pair cost.** Per wallet × market: VWAP(YES buys) + VWAP(NO buys) over matched size; merged size; held time.
6. **External references.**
   - Binance BTC/ETH 1 s mid.
   - Deribit-implied probability for each crypto threshold strike at the fill time.
   - Devigged Pinnacle closing line for sports.
   - NWS and NBM forecast for the station and date, plus model-run release timestamps.
   - Kalshi mid for the matched same-event market (`kalshi_match_id`).
7. **Calibration.** Per wallet × category: Brier score of entry price against outcome; the market's Brier score at the same timestamps; difference; count.
8. **Weekly series.**
   - Net P&L by component, plus `net_ex_incentives` = realized market P&L + LP P&L + combo P&L − fees.
   - `pct_pos_weeks`, both overall and ex-incentives.
9. **Linkage.**
   - `first_usdc_source`, `gas_funder`, `withdraw_dests[]`, `safe_owners[]`, `created_block`.
   - `sibling_cofill_rate`: share of fills with another candidate wallet trading the same market and side within 5 s.
   - `mutual_counterparty_share`.
   - Hour-of-day histogram as a 24-bin vector.
10. **Kalshi trackers.** For every profile we can obtain: per-market settled results with category and entry and exit price.
