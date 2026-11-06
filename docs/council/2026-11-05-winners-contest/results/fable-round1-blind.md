# Verification results: fable

Points: **0** (2/12 PASS). Verified holdout P&L: trades $41,190, wallets $1,617.

U_disc: 1332 wallets. Holdout: from 2026-10-06T00:00Z. Bootstrap: 10000 resamples, seed 20261006.

| id | kind | status | points | key numbers |
|---|---|---|---|---|
| ent_E0000 | entity | FAIL | -1 | wallets=10, total_pnl=52,666, pos_week_share=1, linked=False |
| ent_E0004 | entity | FAIL | -1 | wallets=6, total_pnl=36,528, pos_week_share=0.4, linked=False |
| ent_E0005 | entity | PASS | 3 | wallets=6, total_pnl=1,617, pos_week_share=1, linked=True |
| ent_E0010 | entity | FAIL | -1 | wallets=3, total_pnl=7,101, pos_week_share=1, linked=False |
| ss_consistent_band | sweet_spot | FAIL | -1 | wallets=76, independent_wallets=52, total_pnl=179,351, mean=2,360, ci95_mean=[1,099, 3,511], excess_mean=-779.2, ci95_excess=[-3,771, 1,687], median_pos_week_share=1 |
| w_consistent_large | wallet | FAIL | -1 | wallets=9, independent_wallets=9, total_pnl=127,506, mean=14,167, ci95_mean=[8,358, 20,585], excess_mean=31,821, ci95_excess=[-2,465, 78,741], capped-feed gap: 2 wallets (coverage from as late as 2026-10-08T13:06:27Z) |
| w_rebate_mm | wallet | FAIL | -1 | wallets=17, independent_wallets=14, total_pnl=82,215, mean=4,836, ci95_mean=[2,514, 7,775], excess_mean=-2,314, ci95_excess=[-14,780, 4,977], capped-feed gap: 13 wallets (coverage from as late as 2026-11-04T06:06:43Z) |
| t_sports_snipe | trade | FAIL | -1 | trades=21890, wallets=89, independent_wallets=84, total_pnl=1,929, pnl_per_trade=0.08814, ci95_per_trade=[-4.605, 3.336], capped-feed gap: 24 wallets (coverage from as late as 2026-11-04T23:09:37Z) |
| t_geopolitics | trade | FAIL | -1 | trades=39959, wallets=135, independent_wallets=122, total_pnl=683,183, pnl_per_trade=17.1, ci95_per_trade=[-46.72, 69.6], capped-feed gap: 7 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| t_politics | trade | FAIL | -1 | trades=110609, wallets=159, independent_wallets=143, total_pnl=1,107,412, pnl_per_trade=10.01, ci95_per_trade=[-14.42, 33.06], capped-feed gap: 15 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| t_inplay | trade | FAIL | -1 | trades=502677, wallets=273, independent_wallets=245, total_pnl=3,639,464, pnl_per_trade=7.24, ci95_per_trade=[-0.03164, 14.7], capped-feed gap: 31 wallets (coverage from as late as 2026-11-04T23:09:37Z) |
| t_crypto15m_maker | trade | PASS | 7 | trades=71895, wallets=33, independent_wallets=29, total_pnl=41,190, pnl_per_trade=0.5729, ci95_per_trade=[0.3003, 0.8433], capped-feed gap: 17 wallets (coverage from as late as 2026-11-04T19:09:06Z) |

Full per-claim numbers, including the all-collected-wallets secondary result, are in results.json.
