## 1. Verdict

There is money being made, but it goes to three places. Fast makers collect the fees and rebates that retail takers pay. Option-aware sellers profit from speculative demand for longshot YES. Carry traders collect funding from levered longs. At about 1 s latency and ≤$10k, only slow, wide-spread making on Polymarket US and a rich-longshot NO book are realistically within reach. At $10k, the best case is tens to a few hundred dollars a month. The next two weeks should measure markouts and option-implied gaps. They should not add strategies.

## 2. Ranked candidates

|Candidate|Who pays us|Edge/trade; per day at our size|Capacity|How it dies|Cheapest decisive test (kill)|P(+net EV, 3 mo)|
|---|---|---|---|---|---|---|
|A. Fair-value maker on Polymarket US hourly/daily crypto brackets, quotes priced from options or spot, with surplus hedged on Kalshi|Retail takers crossing ~4c spreads; 25% rebate|1–2c/contract; $5–30/day|Low thousands of $ per day of volume|Spreads tighten to Kalshi's 1–2c; 1.3 s latency gets picked off when spot moves|100+ fills with markouts at 10 s/60 s/settlement. **Kill:** mean 60 s markout + rebate ≤ 0, or < 15 fills/day|25%|
|B. Existing cross-venue maker–taker desk (Polymarket US maker, Kalshi taker)|Same retail, plus stale Polymarket US quotes|~$0.28/hedged lot; $1–3/day|Bounded by ~1% hedged-fill rate|Legging losses (unhedged lots) swamp the edge|Count **all** lots including unhedged/unwound over ≥150 attempts-with-fill. **Kill:** total PnL CI upper bound < $0.10/lot|30% (positive but trivial)|
|C. Sell longshot YES (buy NO) on weekly/monthly BTC/ETH thresholds|Speculative longshot demand|2–5c per NO bought at 90–97c, if the 2023 gap survives; ~$3–8/day on $5k|~$5–20k at current depth (prior)|Gap has decayed; a BTC tail move hits every strike at once|Free 10-day snapshot comparing Kalshi and Polymarket US asks with Deribit-implied digitals. **Kill:** executable NO edge < 1.5c after fees|20%|
|D. Funding/basis carry (CME or US perp-style futures vs spot)|Levered longs|Prior: 5–10% annualized in 2025–26, sometimes negative; ~$1/day on $5k|Large|Below the T-bill hurdle (prior ~4%); venue/ADL tail|Pull 90-day funding/basis history for accessible venues. **Kill:** net spread over T-bills < 3%|15% (to beat cash)|
|E. Passive deep-favorite bids (96–98c), Kalshi 15-minute|Late takers paying for certainty|+2–4c/fill against a −96c tail|Small|One loss per ~30 fills erases everything|Full KXBTC15M history: favorite win rate at 96–98c. **Kill:** win rate < price + 0.5c|5%|

## 3. Answers

**Q1.** In order: A, B, C. Who pays us and why: retail takers on Polymarket US pay for immediacy, and nobody with options-grade fair value quotes there yet. That window closes when professional market makers arrive on Polymarket US. C pays because longshot demand is behavioral and persistent (prior), but the size of the gap is unverified on current data. Tests and kill criteria are in the table.

**Q2.** Single-venue pair-locking does not reproduce on our venues:
- There is no merge, and a 3% maker fill rate makes completing both legs rare.
- When one leg fills, it is usually because the price moved against the other leg. That is adverse selection, and it shows up as exactly the legging risk you measured.
- At 1.3 s you cannot manage your queue position.

A cross-venue version (rest on both venues, lock when the combined cost is under $1) works only where one side is very stale and wide (Polymarket US) and the other is liquid enough to hit as taker (Kalshi). That is B. Resting on both sides doubles the adverse selection. Kalshi 15-minute pair-locking at our latency: no. Lancellotti's maker loses more as cancels get slower, and ours are ~5× slower than his 250 ms case.

**Q3.** Yes, this is the right trade shape. Buying NO at 92c that is "worth" 97c returns about 5% per month on locked capital. Prior: Kalshi pays interest on balances and positions, which softens the lockup cost.

The tails work against it:
- Every strike is short the same BTC jump, so losses are correlated.
- The variance risk premium makes the trade pay on average, but it does not shorten the tail.
- Hedge: the only realistic US-accessible option is CME micro BTC options or futures through a futures broker (prior), and basis and settlement-index mismatch cost several points.
- At $10k, run it unhedged with per-expiry exposure caps: a ≤10% bankroll loss if BTC moves 3σ.
- The 2023 evidence is stale. Re-measure before committing.

**Q4.** The two sources agree. The bot census shows maker buys about breakeven before rebates and taker buys losing the fee. So the group's profit is the transfer of taker fees through rebates, and that is exactly "efficient for takers and alpha-less makers." The residual edge is rebates plus spread capture, and it goes to whoever has the fastest cancels. On the specific candidates:
- Volatility-conditioned lead pricing is just better fair value. It is reachable only on slow brackets (A), not 15-minute contracts.
- Cross-coin lead-lag gives t = 0.56, which is dead.
- Kalshi-leads-Binance is not causal and not useful at our latency.
- The 53% streak reversal comes from a vendor, and Lancellotti's 3,988 trials did not reproduce it. Check it once on Kalshi data for free, then drop it.

**Q5.** Ranking, from my priors:
1. **(a) Funding/basis carry:** real, longs pay it, Sharpe 1–2 before tail. At $10k it is roughly T-bills plus a little, with exchange and ADL tails.
2. **(g) Cross-instrument relative value:** this is C/A.
3. **(f) Market-making with rebates:** on CEXs, retail fee tiers pay maker fees, so it is dead. On prediction markets it becomes A.
4. **(b) Cross-venue spot/perp basis:** taken by co-located firms. Net Sharpe < 0.5 for us.
5. **(c) Intraday momentum or reversal:** published edges are small, and costs eat them. Net Sharpe ~0.
6. **(d) Cointegrated pairs:** cointegration breaks in regime shifts. Net Sharpe < 0.5.
7. **(e) Liquidation-cascade and event trading:** a speed game we lose.

Candlestick patterns as such carry no edge after costs and data-snooping correction.

**Q6.** The best single combination is option- and spot-implied fair value used to quote Polymarket US crypto brackets (A). Inventory is netted against Kalshi brackets and, later, CME micros. This combines cheap fair value from one arena with a slow, wide-spread venue in the other. Using binary prices as a spot signal: no.

**Q7.** What we are likely mis-measuring:
- **The +$1.98 is survivorship.** The strategy's real PnL is all 18 lots, about +$3.14, and +$11.07 of that is unhedged coin flips. With n = 18 there is no confidence interval worth stating.
- **Failure to hedge is not random.** It is correlated with adverse moves, so unhedged lots must count as strategy outcomes.
- **Fill-rate denominators** may count the same opportunity several times.
- **46.9% per capital-day hides near-zero capital utilization.**
- **Kalshi per-order fee rounding at 1–10 lot sizes** can push the effective fee from 1.75c to 2c+.
- **The terms of the Polymarket US rebate** (cash, timing, caps) are unverified.
- **Rule-text matching for games** carries void and overtime divergence risk.
- **The vendor's bot census** has strong survivorship and weak persistence.
- **Engineering hours** are not in any PnL.

Measure before capital moves:
- per-fill markouts;
- PnL including all lots;
- daily fill counts;
- realized rebate statements.

**Q8. Two-week plan**

1. **Markout harness on the existing desk, extended to A** (shadow quotes, then $300 live).
   - Success: ≥ 100 fills, mean 60 s markout + rebate > +0.5c/contract, and all-lots PnL > 0.
   - Kill: markout ≤ 0 after 100 fills, or < 15 fills/day.
   - Cost: ~4 engineering days plus ≤ $100 of losses.
   - P(positive net EV within 3 months) = **25%**.
2. **Longshot gap re-measurement** (free data; Kalshi, Polymarket US and Deribit snapshots hourly for 10 days; settlement-time and index adjusted).
   - Success: executable NO edge ≥ 2c on strikes under 20c, t > 3, across ≥ 3 expiries.
   - Kill: edge < 1.5c, or concentrated in one expiry.
   - Cost: ~3 days, $0.
   - P = **20%**.
3. **Kalshi KXBTC15M history check** of streak reversal and the 96–98c favorite win rate (one script).
   - Success: reversal ≥ 52.5% out of sample with p < 0.01 as maker, or favorite win rate ≥ price + 1c.
   - Kill: otherwise, and close the 15-minute arena permanently.
   - Cost: 1 day.
   - P = **5%**.

Biggest program risk: spending weeks of engineering on edges worth under $100 a month, while adverse-selected maker fills look like edge in small samples.

## 4. Top 3 risks

1. **Adverse selection disguised as edge.** Maker fills happen when we are stale. Without markouts and all-lots accounting, the numbers will look positive until a regime shift.
2. **Correlated tail on short-longshot books.** One BTC jump hits every strike, and the 100% win records in the evidence are ≤ 91 fills.
3. **Decay and competition.** The 2023 option gap and today's Polymarket US spreads are the kind of inefficiency that professional market makers close within months. Capacity at our size may disappear before validation finishes.
