# Orchestrator entry: working notes (discovery data only)

Pre-registration: `../../round0/orchestrator-preregistration.md` (sha256 f5f7a1a4…, sealed before any contestant answered).

## Finding 1: weekly consistency persists (pseudo-holdout inside discovery)

Selected on ISO weeks ≤ 2026-W36 (wallets with ≥ 8 nonzero weeks); tested on W37–W40, 4 weeks:

| Pre-period positive-week share | n (total P&L) | Post mean | Post > 0 |
|---|---|---|---|
| < 50% | 5,068 | −$1,656 | 29% |
| 50–60% | 2,478 | +$677 | 41% |
| 60–70% | 1,900 | +$1,647 | 48% |
| 70–80% | 1,187 | +$6,543 | 55% |
| 80–90% | 664 | +$9,805 | 63% |
| ≥ 90% | 290 | +$9,475 | 60% |

The trend is monotone. On net-ex-incentives the ≥ 90% band has a median post positive-week share of 1.0.

Sweet spot ($1k–$100k pre P&L, ≥ 80% positive weeks), n=390: post mean +$3,579 [2,111, 5,329], 63% positive. The same P&L band below 80%, n=2,403: +$1,309 [388, 2,276], 46% positive.

**Caveat:** the universe comes from today's boards, so both groups are survivorship-inflated. The comparison between them is the informative part.

## Finding 2: the consistent cohort is not mostly rebate-funded

| Cohort (discovery) | n | Median P&L | Median positive days | Median drawdown / P&L | ROI on volume | Incentive-heavy |
|---|---|---|---|---|---|---|
| A ≥ 90% positive weeks | 321 | $26,982 | 0.82 | 0.06 | 1.59% | 17% |
| B 80–90% | 704 | $6,913 | 0.71 | 0.17 | 1.55% | 12% |
| C 60–80% | 3,278 | $3,121 | 0.61 | 0.42 | 1.88% | 12% |
| D < 60% | 7,892 | −$564 | 0.48 | 1.14 | −0.72% | 9% |

"Incentive-heavy" means more than half of P&L comes from rebates, rewards or fee refunds.

Sweet spot ($1k–$100k, ≥ 80% positive weeks), n=429: 18% incentive-heavy, median positive days 0.74, median ROI on volume 2.44%, median 21 weeks.

**Next:** use trade-level features to see what the A/B cohort trades (price buckets, timing relative to resolution, categories, both-sides share). Prior: near-certainty harvesting and resolution-window trading. The smooth equity curves and the ~1.6% ROI on volume fit that.

## Finding 3: day-level consistency persists inside U_disc (random tiers only)

The sample is U_disc's daily economic P&L (first difference of cumulative `economic_pnl`, in decimal USDC). Selection used data before 2026-09-08; the test ran 09-08 to 10-05. Only the random tiers D3 and D4 are used, because D1 and D2 were partly selected on September.

| Pre-period selection | n | Post mean (CI) | Median post positive weeks | Median post positive days |
|---|---|---|---|---|
| All with ≥ 6 pre weeks | 679 | +$749 [229, 1,332] | 0.55 | 0.56 |
| ≥ 80% positive weeks, $1k–$100k | 74 | +$673 [15, 1,457] | 0.75 | 0.71 |
| ≥ 90% positive weeks and ≥ 70% positive days | 19 | +$921 [−462, 2,939] | 0.80 | 0.83 |
| ≥ 75% positive days, ≥ 40 active days | 43 | +$1,166 [−38, 2,677] | 0.75 | 0.82 |
| Control: < 60% positive weeks | 337 | +$602 [−132, 1,435] | 0.50 | 0.47 |

Consistency persistence is strong. Dollar excess is noisy at these n.

Sweet-spot claim draft: pct_pos_days ≥ 0.75, ≥ 40 active days, P&L $1k–$100k, pct_pos_weeks ≥ 0.85. The 80% positive-week gate is the binding risk with only about 5 holdout weeks.

## Status: non-competing benchmark

I built the dataset and had about 45 minutes of discovery-data access before the contestants. My claims are verified and scored under the same rules, but I am **not eligible for the prize**.

## Finding 4: trade-level economics (masked discovery trades, final U_disc = 1,332)

Sample: BUYs in markets resolved before the cutoff; payoff = size×(payout − price) − fee. Train is before 2026-09-08; test is 09-08 to 10-05. Both halves are partly selection-inflated: D1–D3 were chosen on full-discovery P&L, so treat the numbers as ranking signals only.

- **All resolved BUYs (546 wallets with trades):** ROI +4.7% train, +2.7% test.
- **Consistent wallets** (pct_pos_days_disc ≥ 0.75, ≥ 40 active days): ROI +2.4% / +1.0%, about 125 wallets.
- **Consistent wallets × sports_pregame:** ROI +4.5% / +5.5%, about 80 wallets and 2,100–2,700 events. This is the best broad cell, replicable on Kalshi. Pre-registered #9 (fable #6, astra #10, argon #10).
- **Taker buys at 10–30¢ (all U_disc):** ROI +24% / +30%, while maker fills at 10–30¢ are −7% / −13%. Informed longshot taking.
- **ge99 / 95–99¢ near-certainty buys:** about 0 to +1%. Thin; the fee eats it.
- **All U_disc by market type (test half):** crypto_updown_15m +10.5% (only 23 wallets), politics +10.2%, macro +9.3%, geopolitics +7.3%, sports_pregame +1.7%, sports_inplay +2.1%, crypto_updown_1h −3.1%.
