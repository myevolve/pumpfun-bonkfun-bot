# Verification results: argon

Points: **42** (9/18 PASS). Verified holdout P&L: trades $966,509, wallets $38,252.

U_disc: 1332 wallets. Holdout: from 2026-10-06T00:00Z. Bootstrap: 10000 resamples, seed 20261006.

| id | kind | status | points | key numbers |
|---|---|---|---|---|
| trd_05_pre_end_settled_carry | trade | PASS | 7 | trades=32997, wallets=130, independent_wallets=116, total_pnl=104,207, pnl_per_trade=3.158, ci95_per_trade=[2.564, 3.723], capped-feed gap: 15 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| trd_08_multi_outcome_slate_no | trade | PASS | 7 | trades=19973, wallets=91, independent_wallets=79, total_pnl=364,310, pnl_per_trade=18.24, ci95_per_trade=[11.26, 23.62], capped-feed gap: 12 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| trd_09_smart_wallet_shadow | trade | PASS | 7 | trades=92258, wallets=31, independent_wallets=27, total_pnl=363,700, pnl_per_trade=3.942, ci95_per_trade=[1.127, 6.906], capped-feed gap: 15 wallets (coverage from as late as 2026-11-04T19:09:06Z) |
| trd_10_crypto15m_passive_maker | trade | PASS | 5 | trades=71115, wallets=32, independent_wallets=28, total_pnl=40,492, pnl_per_trade=0.5694, ci95_per_trade=[0.2841, 0.8529], capped-feed gap: 17 wallets (coverage from as late as 2026-11-04T19:09:06Z) |
| trd_11_crypto5m_hft_maker | trade | PASS | 5 | trades=406054, wallets=51, independent_wallets=46, total_pnl=52,201, pnl_per_trade=0.1286, ci95_per_trade=[0.02984, 0.227], capped-feed gap: 28 wallets (coverage from as late as 2026-11-04T22:35:10Z) |
| ent_03_e0005_pregame_scalper | entity | PASS | 3 | wallets=6, total_pnl=1,617, pos_week_share=1, linked=True |
| trd_01_inplay_dir_fav_taker | trade | FAIL | -1 | trades=17404, wallets=109, independent_wallets=105, total_pnl=-644,734, pnl_per_trade=-37.05, ci95_per_trade=[-97.21, 19.74], capped-feed gap: 5 wallets (coverage from as late as 2026-11-01T14:28:54Z) |
| trd_02_inplay_passive_fav_maker | trade | FAIL | -1 | trades=45137, wallets=111, independent_wallets=106, total_pnl=-65,056, pnl_per_trade=-1.441, ci95_per_trade=[-11.63, 8.663], capped-feed gap: 16 wallets (coverage from as late as 2026-11-04T23:09:37Z) |
| trd_03_pregame_sharp_pinning | trade | FAIL | -1 | trades=6801, wallets=20, independent_wallets=19, total_pnl=-29,814, pnl_per_trade=-4.384, ci95_per_trade=[-20.32, 11.33], capped-feed gap: 3 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| trd_04_after_end_resolution_carry-r2 | trade | PASS | 7 | trades=32986, wallets=101, independent_wallets=93, total_pnl=25,510, pnl_per_trade=0.7734, ci95_per_trade=[0.02443, 1.391], capped-feed gap: 21 wallets (coverage from as late as 2026-11-04T23:09:37Z) |
| trd_06_sibling_ladder_arb-r2 | trade | FAIL | -1 | trades=24312, wallets=32, independent_wallets=32, total_pnl=429,296, pnl_per_trade=17.66, ci95_per_trade=[-3.132, 45.84], capped-feed gap: 7 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| trd_07_crypto_moonshot_no | trade | FAIL | -1 | trades=5503, wallets=30, independent_wallets=29, total_pnl=40,899, pnl_per_trade=7.432, ci95_per_trade=[-6.004, 18.82], capped-feed gap: 8 wallets (coverage from as late as 2026-11-04T19:09:06Z) |
| trd_12_awards_culture_precursor | trade | FAIL | -1 | trades=1094, wallets=30, independent_wallets=28, total_pnl=-11,972, pnl_per_trade=-10.94, ci95_per_trade=[-22.21, -0.8301], capped-feed gap: 7 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| trd_13_weather_ensemble-r2 | trade | PASS | 7 | trades=11165, wallets=56, independent_wallets=54, total_pnl=16,106, pnl_per_trade=1.443, ci95_per_trade=[0.456, 2.789], capped-feed gap: 7 wallets (coverage from as late as 2026-11-02T20:52:10Z) |
| ss_01_near_certainty_harvesters | sweet_spot | FAIL | -1 | wallets=21, independent_wallets=18, total_pnl=-8,186, mean=-389.8, ci95_mean=[-2,754, 1,031], excess_mean=-1,630, ci95_excess=[-3,629, -34.8], median_pos_week_share=0 |
| ent_01_e0000_postmatch_fleet-r2 | entity | PASS | 3 | wallets=6, total_pnl=36,635, pos_week_share=1, linked=True |
| ent_02_e0004_inplay_slicer | entity | FAIL | -1 | wallets=6, total_pnl=36,528, pos_week_share=0.4, linked=False |
| ent_04_e0010_afterend_soccer | entity | FAIL | -1 | wallets=3, total_pnl=7,101, pos_week_share=1, linked=False |

Full per-claim numbers, including the all-collected-wallets secondary result, are in results.json.
