# Filter whitelist (round 1)

Run `python contest/validate.py <claims.json>` before submitting. The verifier uses the same code, so a claim the validator accepts passes the verifier's schema stage. Columns are checked against the as-of-cutoff view (`contest/data_disc/`).

## trade_filter: trade-time-observable columns only

A trade_filter column must be in this list and exist in `data_disc/features/trades.parquet`:

| column | meaning |
|---|---|
| `side` | BUY / SELL (the test scores BUYs only) |
| `price` | fill price of the outcome token |
| `size`, `usdc_size`, `notional` | shares; USDC value of the fill |
| `outcome_index` | which outcome was bought |
| `maker_flag` | the fill was a resting (maker) order |
| `market_type`, `trade_market_type`, `category` | market labels; `trade_market_type` splits sports into pre-game / in-play by the trade time |
| `neg_risk`, `n_markets_event` | event structure |
| `fees_enabled`, `fee_rate`, `fee_exponent`, `fee_taker_only`, `fee_obs`, `fee_est`, `fee_used` | fee schedule; fee on the fill |
| `secs_to_end` | seconds from the trade to the scheduled end |
| `secs_since_created` | seconds since the market was created |
| `after_end` | the trade came after the scheduled end, while the market was still open |
| `px_bucket`, `px_bucket_2c`, `t2end_bucket` | price and time-to-end buckets |
| `hour_utc` | hour of the trade |
| `pre_move_300s` | price move in the 300 s before the trade (sampled fills only; null otherwise) |

Columns not present in the trades table are simply unavailable.

**Never allowed in a trade_filter (binding):**
- Outcome, payoff and P&L: `won`, `payout_*`, `h2r_pnl`, any `*pnl*`, ROI or calibration value.
- Markouts and any later price: `mo_1m`, `mo_1h`, `mo_24h`, `mo_close`, `markout_*`, closing price.
- Resolution facts: `resolution_ts`, `secs_to_resolution`, `resolution_status`, UMA/dispute fields, `trading_close_ts`.
- Holdout or lifetime aggregates, such as market lifetime volume.
- Identifiers, text and absolute times: `condition_id`, `market_id`, `token_id`, `slug`, `event_id`, `event_slug`, `neg_risk_market_id`, `series_slug`, `outcome` (label text), question or title text, `tags`, `transaction_hash`, raw timestamps (`timestamp`, `created_ts`, `end_ts`, `event_start_ts`, ...).
- Columns that look past the trade: `htr_buy` (was the position held to resolution), `is_combo` (split/merge within 10 minutes), `has_sibling_trade_120s` (includes the next 2 minutes), `period`, and the linkage/flow columns `min_alpha_lag_s`, `is_alpha_leader`, `counterparty_wallet`, `onchain_role`, `n_onchain_counterparties`, `self_cluster_fill`, `capped_wallet`.
- Wallet-specific columns: the trader's address, name or any per-wallet value. Wallets are selected with `wallet_filter`.

## wallet_filter

Allowed:
- columns ending in `_disc` in `data_disc/wallets.csv` (`tier_disc`), `data_disc/features/wallet_features.parquet` or `wallet_pnl_metrics.parquet`;
- the static column `first_ts`, the start of the wallet's P&L series;
- columns of `data_disc/clusters/wallet_linkage.parquet` (list-valued ones support only `is_null` / `not_null`).

Not allowed: `_hold` / `_all` columns, leaderboard ranks or values (today's boards include holdout results), `proxy_wallet` / `wallet` / `user_name`. An explicit wallet list (`wallets`) is allowed only in `entity` claims.

## Condition semantics

- Ops: `==, !=, <, <=, >, >=, in, not_in, between, is_null, not_null`. All conditions are ANDed.
- `between` takes `[lo, hi]` and is inclusive. `in` / `not_in` take a non-empty list.
- Ordering ops need a numeric column. Values must match the column type.
- A null value fails every op except `is_null`; this includes `!=` and `not_in`.

## How claims are scored (summary; RULES.md governs)

- Holdout runs from 2026-10-06T00:00Z to the last 00:00Z boundary that every P&L series reaches (2026-11-05T00:00Z in the current snapshot). Wallets are selected from the discovery-defined universe U_disc (`tier_disc`, tiers D1-D4), using the as-of-cutoff values you see.
- `trade`: holdout BUYs in markets resolved by the dataset snapshot; payoff = size × payout − size × price − fee. Trades in markets still unresolved at the snapshot are dropped and counted. The 95% CI is a percentile bootstrap clustered by event, with 10,000 resamples and a fixed seed.
- Breadth counts independent wallets. Wallets that the verifier links into one entity count once.
- Weeks: a partial first week (Tue 2026-10-06 to Sun 10-11), then ISO weeks. No week mixes discovery and holdout days. Positive-week and positive-day shares count only periods with nonzero P&L.
- Every result is reported beside its U_disc baseline: the same wallet selection without the trade filter, or all U_disc wallets.
- Replicable bonus (mechanical): every selected holdout trade's market type is listed on Kalshi or Polymarket US. Listed (`trade_market_type`): crypto up/down 15m/1h/1d, crypto_threshold, sports_pregame, sports_inplay, sports_unk_start, combo, politics_elections, geopolitics, macro_data, weather, mentions_speech, awards_culture, other_tech_ai, other_finance_equities. Not listed: crypto_updown_5m, crypto_updown_4h, other, missing. No condition may need sub-minute reaction: an upper bound below 60 s on `secs_since_created` voids the bonus. A replication field of "not replicable" also voids it.
- The strategy-level bonus check is unavailable: the dataset has no market-wide tapes.

## Binding clarifications (orchestrator, 2026-11-05)

1. No identifier or text filters. Trade filters use only the generic, trade-time-observable columns above. Explicit wallet lists are allowed only in `entity` claims.
2. Straddling ISO week. Holdout weekly sums are built from daily P&L points at or after the cutoff: a partial first week, then ISO weeks. No weekly row mixes the periods. Positive-week and positive-day shares use the same rule.
3. Resolved markets only. Only payoffs of markets resolved by the dataset snapshot are counted. Each result reports how many selected holdout trades were unresolved and dropped.
4. Baseline numbers for the full holdout are not published to contestants before scoring.
5. U_disc is the union of the discovery-defined tiers D1-D4 (`tier_disc`). It does not depend on today's leaderboards.
6. Selection inputs are the contestant view. wallet_filter (and any discovery feature) is evaluated on the `data_disc/` tables only, the same values you see. Trade-derived `_disc` features there are as-of-cutoff recomputations. U_disc excludes wallets that were collected only through short-window boards (eligibility_source "volume").
