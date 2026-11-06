# Verification results: astra

Points: **24** (5/12 PASS). Verified holdout P&L: trades $35,001, wallets $0.

U_disc: 1332 wallets. Holdout: from 2026-10-06T00:00Z. Bootstrap: 10000 resamples, seed 20261006.

| id | kind | status | points | key numbers |
|---|---|---|---|---|
| T02 | trade | PASS | 5 | trades=43427, wallets=23, independent_wallets=20, total_pnl=27,529, pnl_per_trade=0.6339, ci95_per_trade=[0.2434, 1.027], capped-feed gap: 15 wallets (coverage from as late as 2026-11-04T19:09:06Z) |
| T03 | trade | PASS | 5 | trades=487, wallets=10, independent_wallets=10, total_pnl=48.45, pnl_per_trade=0.09949, ci95_per_trade=[0.07069, 0.1308], capped-feed gap: 3 wallets (coverage from as late as 2026-11-04T19:09:06Z) |
| T04 | trade | PASS | 7 | trades=202, wallets=14, independent_wallets=14, total_pnl=1,379, pnl_per_trade=6.828, ci95_per_trade=[3.604, 10.56], capped-feed gap: 4 wallets (coverage from as late as 2026-10-30T03:37:43Z) |
| T07 | trade | PASS | 7 | trades=270, wallets=18, independent_wallets=15, total_pnl=598.3, pnl_per_trade=2.216, ci95_per_trade=[0.7575, 3.577] |
| T09 | trade | PASS | 7 | trades=831, wallets=24, independent_wallets=24, total_pnl=5,446, pnl_per_trade=6.553, ci95_per_trade=[1.086, 10.94], capped-feed gap: 6 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| E01 | entity | FAIL | -1 | wallets=3, total_pnl=7,101, pos_week_share=1, linked=False |
| E02 | entity | FAIL | -1 | wallets=3, total_pnl=2,458, pos_week_share=0.6667, linked=False |
| T01 | trade | FAIL | -1 | trades=10839, wallets=32, independent_wallets=30, total_pnl=-2,984, pnl_per_trade=-0.2753, ci95_per_trade=[-1.069, 0.3802], capped-feed gap: 23 wallets (coverage from as late as 2026-11-04T22:35:10Z) |
| T05 | trade | FAIL | -1 | trades=4084, wallets=76, independent_wallets=67, total_pnl=-66,954, pnl_per_trade=-16.39, ci95_per_trade=[-97.72, 32.93], capped-feed gap: 7 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| T06 | trade | FAIL | -1 | trades=828, wallets=30, independent_wallets=28, total_pnl=2,399, pnl_per_trade=2.898, ci95_per_trade=[-5.86, 11.55], capped-feed gap: 5 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| T08-r2 | trade | FAIL | -1 | trades=717, wallets=28, independent_wallets=26, total_pnl=38,373, pnl_per_trade=53.52, ci95_per_trade=[-10.12, 68.83], capped-feed gap: 6 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| W01 | wallet | FAIL | -1 | wallets=324, independent_wallets=284, total_pnl=9,588,265, mean=29,593, ci95_mean=[288.1, 69,986], excess_mean=21,839, ci95_excess=[-10,206, 63,813], capped-feed gap: 49 wallets (coverage from as late as 2026-11-04T06:06:43Z) |

Full per-claim numbers, including the all-collected-wallets secondary result, are in results.json.
