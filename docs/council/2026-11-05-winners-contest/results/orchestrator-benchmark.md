# Verification results: orchestrator

Points: **1** (1/7 PASS). Verified holdout P&L: trades $114,405, wallets $0.

U_disc: 1332 wallets. Holdout: from 2026-10-06T00:00Z. Bootstrap: 10000 resamples, seed 20261006.

| id | kind | status | points | key numbers |
|---|---|---|---|---|
| orch-S1 | sweet_spot | FAIL | -1 | wallets=250, independent_wallets=197, total_pnl=263,781, mean=1,055, ci95_mean=[580.6, 1,482], excess_mean=-55.43, ci95_excess=[-994.9, 764.8], median_pos_week_share=0, capped-feed gap: 16 wallets (coverage from as late as 2026-11-03T19:51:55Z) |
| orch-W1 | wallet | FAIL | -1 | wallets=67, independent_wallets=53, total_pnl=1,789,276, mean=26,706, ci95_mean=[8,837, 57,212], excess_mean=8,761, ci95_excess=[-21,154, 33,771], capped-feed gap: 18 wallets (coverage from as late as 2026-11-04T22:35:10Z) |
| orch-C1 | trade | FAIL | -1 | trades=6124, wallets=77, independent_wallets=53, total_pnl=-174,063, pnl_per_trade=-28.42, ci95_per_trade=[-55.13, -3.784], capped-feed gap: 7 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| orch-C2 | trade | FAIL | -1 | trades=74969, wallets=308, independent_wallets=282, total_pnl=-114,465, pnl_per_trade=-1.527, ci95_per_trade=[-26.46, 27.2], capped-feed gap: 50 wallets (coverage from as late as 2026-11-04T23:09:37Z) |
| orch-C4 | trade | FAIL | -1 | trades=157239, wallets=199, independent_wallets=181, total_pnl=1,980,723, pnl_per_trade=12.6, ci95_per_trade=[-10.31, 34.89], capped-feed gap: 15 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| orch-C5 | trade | PASS | 7 | trades=152001, wallets=43, independent_wallets=37, total_pnl=114,405, pnl_per_trade=0.7527, ci95_per_trade=[0.3994, 1.088], capped-feed gap: 24 wallets (coverage from as late as 2026-11-04T19:09:06Z) |
| orch-C7 | trade | FAIL | -1 | trades=16466, wallets=148, independent_wallets=142, total_pnl=-608,363, pnl_per_trade=-36.95, ci95_per_trade=[-207.2, 135.9], capped-feed gap: 21 wallets (coverage from as late as 2026-11-04T23:09:37Z) |

Full per-claim numbers, including the all-collected-wallets secondary result, are in results.json.
