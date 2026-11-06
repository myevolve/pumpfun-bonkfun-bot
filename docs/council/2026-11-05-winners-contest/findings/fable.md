# Round-1 findings — contestant fable

All numbers below are from my pseudo-holdout: select on data before 2026-09-08, test on 2026-09-08 → 2026-10-05 (ISO weeks W37–W40 for wallet claims), bootstrap CIs clustered by event.

## Edges found

**1. Resolution convergence in sports favorites (pre-registered #2).** BUYs of sports outcomes at 0.95–0.99 inside the final hour (including after scheduled end) earn +$6.4/trade net of fees (CI [3.9, 9.4], n=25.6k, 96 wallets, 11.7k markets), positive in both sub-windows. Sellers pay a capital-velocity discount to exit decided positions early. Trivially automatable at 1 s.

**2. In-play sports (pre-registered #5).** All in-play BUYs below 0.95: +$7.0/trade (CI [1.9, 12.4], n=585k, 304 wallets). In-play books lag the game state by minutes; slow retail and stale resting orders pay.

**3. Maker fills in 15-minute crypto up/down (pre-registered #1).** Maker BUYs: +$0.44/trade (CI [0.23, 0.66], n=100k). Pure spread capture against continuous recreational taker flow; maker fee is zero. The same wallets' edge also shows up in P&L decomposition: 17 wallets with ≥30% maker-rebate income and ≥90% positive weeks made +$7.0k each in the 4-week test (88% positive).

**4. Judgment-market underreaction.** Geopolitics BUYs: +$28.6/trade (CI [5.4, 56.7]); politics/elections BUYs: +$18.9/trade (CI [3.3, 29.1]). Slow, news-driven markets with anchored retail on the other side; bots stay out because resolution is judgment-heavy.

## The sweet spot

It exists and is sharply identifiable, but only with a **recency condition**. Wallets with ≥90% positive lifetime weeks, ≥8 nonzero weeks, discovery P&L $1k–$100k, **and ≥40 active trade days since Aug 1** (n=76): 99% positive over W37–W40, median weekly win share 1.0, mean +$10.0k. The leak-free version (selection strictly from weeks ≤ W36) still gives 91–93% positive, mean +$5.4k (CI low +$2.7k). Without the recency condition the same consistency filter selects 304 wallets of which only 51% stay positive — the lifetime positive-week share includes long-dormant accounts whose streak is stale. These wallets are mostly near-certainty harvesters and small market makers: median maker share 0.75, median daily Sharpe 5.7, incentives a minor share of P&L. A parallel band of 9 continuously-active consistent wallets **above** $100k discovery P&L was 100% positive (mean +$33k).

## The hidden entity

The strongest candidate is **cluster E0000**: 10 wallets, each small (~$17k discovery P&L, inside the sweet-spot band), linked by three independent behavioral evidence types (matched trade fingerprints, activity partition/staggering, correlated daily P&L), zero on-chain links — exactly the profile of one operator deliberately splitting flow. Combined: $167k discovery P&L in 8 weeks, 100% positive weeks, 96% positive days, and +$156k with 4/4 positive weeks in my pseudo-holdout. Three more behavioral clusters (E0004: 6 wallets, cofill+fingerprint, +$172k test; E0005: 6 wallets, fingerprint+P&L-corr, +$114k; E0010: 3 wallets, +$14k) show the same pattern. The large on-chain funder clusters (E0001–E0003, $8–24M) look like declared/industrial market-making operations, not hidden entities — they are big, not small, and their week-share is mediocre (0.66–0.73).

## What failed

- **macro_data**: huge mean (+$140/trade in both windows) but the CI never excluded zero under event clustering — P&L concentrates in a handful of CPI/Fed events. Capping trade size or restricting price/time did not fix it. Not submitted.
- **crypto_updown_5m maker**: sign flipped between sub-windows (−0.19 → +0.16). Not submitted (also not replicable per the listed-market rule).
- **crypto_threshold**: decayed from +3.3% ROI to +0.7%; CI includes zero.
- **weather, mentions**: directionally positive but underpowered (n in the hundreds).
- **5–10¢ longshots**: +34% ROI in the first window, −29% in the second. Classic noise.
- **sports_pregame overall**: −1.9% then +1.5%; unstable without further conditioning.
- **Naive consistency selection**: `pct_pos_weeks_disc ≥ 0.9` alone forward-predicts barely better than a coin flip (51%); the recency interaction is the whole game.

## Why can't we just copy the winners?

Four reasons. (1) **The data shows mechanism, not signal.** We can see that in-play sports buyers win, not what feed or model told them to buy; copying the category without the signal buys the average, not the edge. (2) **Following is too late.** The profitable fills happen at prices that exist for seconds-to-minutes; a copier trades after the move that made the trade good (the 2.96% of fills that follow a "leader" within 60 s are following stale prices). (3) **Capacity.** The cleanest edges — 95–99¢ convergence, 15-minute maker spread — pay dollars per trade and only absorb small size; that is precisely why E0000 splits itself into ten small wallets instead of running one big one. Copying at size destroys the price you are copying. (4) **Infrastructure rents.** Maker-rebate P&L requires queue priority and uptime; in-play P&L requires licensed low-latency feeds. What *is* copyable is the strategy class — and that is what my trade claims encode: category + price + timing + maker-side filters that any 1 s-latency operator can run on Kalshi or Polymarket US without seeing anyone's order flow.
