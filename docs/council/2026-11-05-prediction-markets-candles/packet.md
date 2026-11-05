# Council review packet, round 2: prediction markets and crypto candles/pairs. Where is the hidden strategy?

You are one of three independent reviewers (different AI labs). The other two receive this same packet and do not see your answer. Critique honestly and directly; do not defer to the framing. Answer only from this packet plus your own general knowledge; when you use general knowledge, label it "prior". Do not read files or run tools.

## 0. Why there is a round 2

Round 1 (earlier today) reviewed a "safe passage" plan for pump.fun memecoins. All three reviewers agreed: do not race the open, entries have no measured edge, the only validated asset is an exit signal, and the chance of positive net EV within 3 months is about 15-20%.

The owner now challenges the council: **"Somebody is making money. Show me where, and whether we can take a piece of it."** Two arenas:

1. **Prediction markets** (our live venues: Kalshi and Polymarket US).
2. **Crypto pairs and candles** (spot, perpetuals, short-horizon price-direction contracts).

"Markets are efficient" is not an acceptable answer on its own. If you believe there is no edge for us, name the strongest remaining candidate anyway, size it, and give the cheapest test that would kill it.

## 1. Who we are

- Small automated operator. Every claim is evidence-gated: offline replay, then several independent samples, then paper, then small live. No invented prices. Missing exits count as losses. We report medians and means with confidence intervals, with and without the top outcomes.
- **Live venues:** Kalshi and Polymarket US (both US-regulated). Small Novig and ProphetX accounts exist. The *international* Polymarket, home of the 5- and 15-minute crypto Up/Down markets described in section 3, is a different venue; treat our access to it as unavailable unless you argue otherwise. Access to offshore crypto perpetual venues is also unconfirmed.
- **Fees we face:** Polymarket US charges 0 bps maker fee plus a 25% maker rebate. On Kalshi, 175 of 221 series we map charge 0 maker fee; taker fees are about 0.07 x p x (1 - p) per contract, rounded per order. A two-taker cross-venue basket pays about 3.5 cents in fees.
- **Latency:** Polymarket US answers an order POST in about 1.3-1.4 s (fills arrive on the private stream about 1.2 s earlier). We stream Kalshi books. Services run on a cloud VM; a London cloud runner is available. We also run a dedicated Solana node with a Geyser stream for the memecoin work.
- **Capital:** small. Current lots are single-digit to tens of dollars. The bankroll is the owner's call; assume at most $10k unless you argue for more.

## 2. Our own measured evidence, prediction markets

### 2.1 Cross-venue desk, Kalshi x Polymarket US (live, real money, since late October 2026)

- Matched on rule text and priced with both venues' fees, every overlapping class sits within about 0.3% of $1: games $1.009, races $0.997. Crypto (BTC/ETH/SOL/XRP hourly and daily, both venues settle on the CF Benchmarks 60-second average): 548 matched propositions, 209 pairs priced, **0 under $1 even before fees** (2026-10-27). Later, the cheapest of 1,570 priced crypto pairs cost $1.03 per 10 baskets.
- Two-taker baskets: 0 of 50 sampled games under $1.00.
- **Maker on Polymarket at bid + 1 cent, hedged as Kalshi taker:** 5 of 22 pre-game wide-spread games (23%) offered a sub-$1 basket (+1.1% to +14.1% ROI), and 11 of 22 (50%) when quoting inside Polymarket's 3-17 cent spread. Crypto hourly/daily brackets: Polymarket spreads about 4 cents against Kalshi's 1-2 cents produce sub-$1 maker-taker baskets (e.g. $0.9957 net on a BTC 78,250 bracket), settling in 1-24 hours.
- **Realized, 2026-11-02 to 11-05, 18 lots:** 10 fully hedged earned **+$1.98 in total** (makers about $0.28 per lot, takers about $0.15): that is the real edge. 4 unwound: -$0.94. 4 left unhedged: makers +$11.07, one taker -$8.97. Those are coin flips that dwarf the edge.
- **Fill rates are the binding constraint.** Taker fill-or-kill fill rate decayed from 100% (2026-10-30) to about 5% (11-01 onward). When a Kalshi move creates the cross, the Polymarket leg fills 5% of the time (5/108); when Polymarket moved last, 17% (2/12). The stale Polymarket quote's market maker pulls it before our order lands. Maker rests fill about 3% of the time (4 of ~131), but each fill paid +$2 to +$2.9. Hedged fills are about 1% of attempts on every Kalshi shard. Crypto maker rests contributed +$7.95 of the makers' realized result.
- Settled short-horizon lots earned 46.9% per capital-day while held (median hold about 1 hour). Large rate, tiny absolute dollars.

### 2.2 Novig public data (10 trade days)

The favorite-longshot pattern is visible in prices: by a same-day close-mark proxy, longshots gained 7-14 cents on average and favorites lost 15-22 cents. Resolution truth killed it. On 68,475 finalized sub-30-cent openings, real win rates were 0.9-6.7%, against the 29-44% the close-hit proxy implied. Holding a longshot to resolution is a 93-97% loss lottery. No deployable strategy.

## 3. External evidence, short-horizon crypto binaries

Sources vary in quality. Two come from a firm that sells a trading bot. Weigh accordingly.

- **Kalshi 15-minute crypto, negative-result study** (Lancellotti, 2026-07; KXBTC15M plus ETH/SOL/DOGE/XRP; 38 pre-registered tests; round-trip cost about 2.5 cents):
  - The market-implied probability is the best-calibrated forecaster (expected calibration error 0.020, against 0.031 or worse for every trained model). A walk-forward combination of the full feature library re-derives the market price.
  - Stale-quote sniping: profit factor 0.99 at best. The Kalshi quote follows a BTC move in the expected direction 58% of the time, with a median lag of 0.13 cents, about one-eighth of the cost.
  - Market-making simulation: an alpha-less maker loses more with faster cancels (-717 cents per window at 250 ms cancel latency). Every maker fill is 0.6-0.9 cents adversely selected.
  - One survivor, which the author also rejects: a passive bid on deep favorites (about 96-98 cents). It earned +2 to +4 cents per fill and won 100% of 67 search fills and 24 holdout fills, against an unrealized -96 cent single-loss tail.
  - Cross-coin lead-lag: BTC's implied probability predicts the residual on alt contracts (information coefficient up to +0.335). After costs ETH nets +1.23 cents per trade with t = 0.56, statistically zero.
  - Regime slicing over 3,988 trials found nothing that survives deflation.
- **International Polymarket BTC 15-minute replay** (bot vendor; 11,519 windows; Binance one-second data as a proxy for the Chainlink TWAP settlement):
  - Up won 49.3% of windows. Taker fee is 0.07 x p x (1 - p) per share (1.75 cents at 50 cents). Makers pay nothing and share 20% of taker fees as rebates. Marketable orders wait 150 ms before matching.
  - The lead decides: a 5-10 bp lead with 5 minutes left held 86.1% of the time. Volatility moves that by about 10 points: 90.6% in calm hours, 80.1% in volatile ones.
  - A random-walk model was conservative in the 60-90% zone (the favorite won 3-6 points more often than predicted) and overconfident above 97%.
  - After two same-direction windows, the next one reversed 53.1% of the time, stable across both halves of the sample.
- **Census of 2,500 always-on bots** (same vendor; 28 days; 14.6M trades; 5- and 15-minute BTC markets):
  - Bots are 4.7% of wallets but do 70% of volume and supply 85% of maker volume. 32% finished in profit after fees and rebates; the median bot lost $59.
  - As a group the bots kept +$190k on $328M of volume: +$1.13M before fees, -$2.48M taker fees, +$1.55M rebates. Everyone else lost $3.0M.
  - At every price, taker buys lost about the fee. Maker buys finished within ±0.5% of zero in 4 of 6 price buckets, before rebates.
  - Winners share three traits:
    - Most buy both sides: of bots that bought both Up and Down in more than 80% of their windows, 59% were in profit.
    - They fill mostly as makers: 49% of bots with under 10% taker volume were in profit.
    - They trade larger size, which earns more rebates.
  - For 4 of the top 10, rebates were more than half the result.
  - Persistence is weak (rank correlation 0.20 between halves) and survivorship is strong.
- **"gabagool22" reverse-engineering** (third party, 2025-12, 41k trades; internally inconsistent timing claims):
  - Passive limit bids on both Up and Down, about 1:1 balance. Combined average cost about $0.97-0.99 per pair, so +2.5-3.2% gross on the best markets.
  - About $7.6k maximum concurrent capital and about $83 an hour net in one 3.2-hour sample (under an older fee regime).
  - A pair-locking structure, not prediction.
- **Derivatives lens** (Lee, Lee and Lee, International Review of Financial Analysis, 2026; 113,338 Kalshi and Polymarket crypto contracts): the binaries are digital options. Fixed-strike contracts show a symmetric smile and a robust variance risk premium, and the two venues price matched BTC 15-minute contracts near-identically.
- **Option-implied benchmark** (Portnaya, arXiv 2606.19517; September 2023 data):
  - Polymarket BTC threshold YES priced above the discounted digital value implied by Binance options by 5.6 percentage points on average (t = 6.46, 214 hourly observations). 6.3 pp pooled over three markets; 11 pp against Deribit.
  - The gap is largest at low probabilities and long maturities, consistent with speculative demand, and has a half-life of about 4 hours.
  - A delta-hedged arbitrage proxy stayed profitable after conservative costs, with marginal precision. ETH evidence was mixed.
- **Kalshi weekly BTC digitals vs Deribit** (student club, 2025-02, one snapshot at open, thin book): Kalshi prices sat near simple GBM models and above Deribit-implied probabilities, by up to about 12 points at one strike. Settlement-time and index differences complicate any hedge.
- **Lead-lag claim** (Synth, 2026-08): 2-second moves in Kalshi 15-minute YES prices correlate with the next 10 seconds of Binance BTC returns. The correlation rose from 0.036 (January) to 0.173 (August). Causality is not established.
- **Growth:** Kalshi crypto 15-minute volume grew from $53.8M (January 2026) to $4.8B (July 2026).

## 4. External evidence, crypto candles and pairs

- Seasonality: a 2024 review finds no robust return seasonality in crypto; Bitcoin's Monday effect does not persist after 2015.
- Technical analysis: a 2022 study finds technical rules can beat buy-and-hold in some coins, especially during bubbles, with transaction-cost effects varying by coin. Data-snooping (reality-check) studies are mixed. A 2022 paper reports intraday time-series momentum in Bitcoin.
- Order flow: a 2026 study reports that order flow explains the cross-section of crypto returns, robust to short-selling constraints.
- Funding and basis carry: a 2025-2026 literature exists on funding-rate arbitrage (CEX and DEX risk/return profiles, a "two-tiered" funding structure, "funding drag", "conditional arbitrage capacity"). We could not retrieve its numbers; use your priors and label them.
- Fake volume: Bitquery estimates 58% of Solana DEX volume over a recent 30-day window was not real trading.
- Our memecoin tapes: at retail latency, every entry window after the creation slot is negative out of sample. The validated exit signal: after a wallet with at least 2 SOL of strictly-prior realized profit sells, the median price change over the next 10/25/60 slots was -13.4/-21.6/-36.8% (tape 1), -10.1/-14.1/-23.0% (tape 2) and -7.3/-11.0/-19.4% (tape 3), against -0.7 to -2.9% after ordinary sells.

## 5. The challenge

Q1. **Prediction markets.** Rank the top 3 places a small, automated, ~1 s latency operator with Kalshi and Polymarket US accounts can still earn positive expected value after fees. For each give: who pays us and why they keep paying, edge per trade and per day at our size, capacity, how it dies, and the cheapest decisive test with a kill criterion.

Q2. **Pair-locking and rebates.** Is the gabagool-style structure (passive bids on both outcomes, lock the pair under $1, harvest rebates) reproducible on our venues? Consider: Polymarket US 0 bps maker fee plus 25% rebate, Kalshi 0 maker fee on most series, Kalshi 15-minute crypto contracts, hourly/daily CF Benchmarks brackets on both venues, no on-chain merge, and our ~3% maker fill rate. Would a cross-venue version (rest on both venues, lock when combined cost < $1) work, and what kills it (legging risk, adverse selection, queue position)?

Q3. **Longshot premium.** The option-implied gap (prediction-market YES rich by 5.6-11 pp at low probabilities and long maturities) and our Novig result (longshots lose 93-97% at resolution) look like the same phenomenon: speculative demand overpays for longshot YES. Is the trade "sell longshot YES (buy NO) on long-dated crypto thresholds, hedged with options or perps"? Size it honestly. How do the variance risk premium, fat tails (the Kalshi survivor's -96 cent tail) and capital lockup change it? Which hedge, if any, is available to us?

Q4. **Short-horizon binaries.** The Kalshi study says 15-minute crypto is efficient for takers and for alpha-less makers. The bot census says profitable bots are two-sided makers living on rebates. Reconcile these. Where exactly is any residual edge: rebate harvesting, volatility-conditioned lead pricing as a maker, cross-coin lead-lag, the Kalshi-leads-Binance claim, the 53% streak reversal? Is any of it reachable at our latency?

Q5. **Candles and pairs.** Rank honestly: (a) funding/basis carry, (b) cross-venue spot/perp basis, (c) intraday momentum or reversal, (d) cointegrated pairs, (e) liquidation-cascade and event trading, (f) market making with rebates, (g) cross-instrument relative value (prediction-market price vs options vs perps). For each: who is on the other side, why it persists, capacity at our size, realistic net Sharpe, and the tail that kills it. Say plainly whether candlestick patterns as such carry any edge.

Q6. **The connection.** Where do the two arenas combine into something better than either alone (for example, hedging binaries with perps or options, using binary prices as a spot signal, buying cheap convexity in binaries)? Name the single combination with the best odds for us.

Q7. **Our blind spots.** What are we likely mis-measuring or mis-reading? Candidates: the +$1.98 hedged edge over 3 days, the 1-5% fill rates, survivorship in public bot data, vendor sources. What must be measured before any capital moves?

Q8. **Two-week plan.** At most 3 experiments. For each: success and kill criteria, expected cost, and your probability (0-100%) that it yields a strategy with positive net EV after costs within 3 months. Then state the single biggest risk to the program.

## 6. Response format

At most 1,200 words, in these sections:

1. **Verdict** (2-3 sentences).
2. **Ranked candidates**: a table with at most 5 rows. Columns: candidate, who pays us, edge per trade and per day at our size, capacity, how it dies, cheapest decisive test (kill criterion), P(positive net EV within 3 months).
3. **Answers Q1-Q8** (numbered, brief; you may refer back to the table).
4. **Top 3 risks**.
