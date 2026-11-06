# Plan: deploying the winners on paper

The owner's instruction (2026-11-06): add the usable winners to the plan, implement them and make them
live, where **live means paper trading**. No real orders are placed anywhere.

## Council steps (Wave C3), carried forward
1. Desk markout audit: done (Wave C4).
2. 10-day option-implied gap logger: running until 2026-11-16.
3. One small live maker test: replaced by the paper maker specs below (items 8 and 9).

## Winners to paper (Kalshi)
Engine: `paper-lab`, built in the `polymarket-alpha-bot` worktree on branch `feat/paper-lab`
(`experiments/paper_lab/`), reading public Kalshi data only. Each spec has a $1,000 paper bankroll.

| # | Spec | Verified on the Polymarket holdout | Kalshi version |
|---|---|---|---|
| 4 | r01-argon-slate-no | NO at 85–96c in 3+ market events, +$18.24/trade, 20k trades | mutually exclusive events, non-sports/crypto categories |
| 5 | r01-argon-decided-carry | 96–99.9c before end, +$3.16/trade, 33k trades | favorite 96–99.5c within 72 h of close |
| 6 | r01-astra-mentions-bond | near-certain mentions | Mentions favorites 95–99.5c within 48 h |
| 7 | r01-astra-tech-bond | near-certain tech/companies | favorites 95–99.5c within a week |
| 8 | r01-astra-crypto-strike-bond | resting bids on near-decided crypto strikes | maker bids on hourly BTC/ETH strike favorites |
| 9 | r01-council-crypto15m-maker | 15-minute crypto maker, found by all four | maker quotes from a spot fair value (no rebate on Kalshi) |
| 10 | r01-argon-weather-nws | weather traders, +$1.44/trade, 11k trades | daily-high brackets priced from a calibrated NWS model |
| 11 | smart_shadow (coded) | shadowing proven wallets, +$3.94/trade, 92k trades | mirror fresh leader trades onto confidently matched Kalshi markets |

Not deployable now: after-end carry (Kalshi halts trading at close; revisit for Polymarket US), the
linked fleets (observations, not strategies), and every mechanism that failed the holdout (pre-game and
in-play sports, broad politics, wallet persistence).

## Kill rules, per spec
- Stop when at least 30 positions have settled and the 95% CI of mean P&L per position is below zero.
- Stop when the daily loss cap trips twice.
- Stop on any paper fill that cannot be traced to a stored book snapshot (fill-model breach).
- Models must beat the Kalshi mid on calibration before their specs trade (weather, crypto).

## What would justify real money (owner's decision, not part of this plan)
At least 100 settled positions with the CI above zero after fees, consistent across two independent
windows, and a fill audit showing paper fills were achievable.

## Tournament loop (rounds 2–20, hourly)
Seal at :55 → deploy every valid contestant spec → forward-verify all sealed claims → claims that newly
verify become `council` specs → deploy. Rules: `contest/tournament/TOURNAMENT.md` (research tree).
