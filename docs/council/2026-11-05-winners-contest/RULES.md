# Contest rules: round 1 (claims) and verification

## Data and tools

- Dataset: `/Users/xero/CodeProjects/polymarket-alpha-bot/data/research/winners/`. Read `DATA.md` first.
- Kalshi data: `winners/kalshi/`. On-chain and behavioral linkage: `winners/clusters/`.
- Your workspace: `contest/workspaces/<you>/`. It is the only place you can write.
- Python: `/Users/xero/CodeProjects/pumpfun-bonkfun-bot/.venv/bin/python` (pandas 3, numpy 2, pyarrow). No network access to venues; the dataset is everything.
- Periods:
  - discovery: before 2026-10-06T00:00Z;
  - holdout: from 2026-10-06T00:00Z onward (about 4.5 weeks).

## Claim file

Write `contest/workspaces/<you>/claims.json`: a JSON list of at most 25 claim objects with these fields.

- `id`: unique.
- `name`.
- `mechanism`: at most 60 words, saying who pays and why it persists.
- `kind`: `trade`, `wallet`, `entity` or `sweet_spot`.
- `preregistered`: your round-0 hypothesis number, or null.
- `wallet_filter`: conditions over `features/wallet_features` columns. Selection may use only `_disc` columns, static columns marked static in DATA.md, and `clusters/wallet_linkage` columns.
- `trade_filter`: conditions over `features/trades` columns observable at trade time. The verifier publishes the whitelist; outcome, payoff, markout and resolution-derived columns are rejected.
- `wallets`: an explicit wallet list, allowed only for `entity` claims.
- `metric`:
  - `trade` claims: `h2r_pnl`, the hold-to-resolution payoff of each BUY net of fee.
  - `wallet`, `entity` and `sweet_spot` claims: `net` or `net_ex_incentives`, from weekly P&L.
- `replication`: how a small automated operator at about 1 s latency could run it on Kalshi or Polymarket US, or "not replicable".
- `discovery_evidence`: your discovery-period numbers.

A condition is `{"col": <name>, "op": <op>, "value": <v>}`. Allowed ops: `==, !=, <, <=, >, >=, in, not_in, between, is_null, not_null`. All conditions in a claim are ANDed; submit alternatives as separate claims.

## Verification

Deterministic and holdout-only, run by an independent verifier.

**Scoring universe U_disc.** It is defined from discovery-period data only, never from today's leaderboards, so holdout success cannot select a wallet into scoring. Today's boards (all-time ranks included) already contain October results.

Among collected wallets with a user-pnl series and at least 30 active days before 2026-10-06, U_disc is the union of four tiers, each with complete trade and activity histories:
- **D1:** the top 400 by discovery-period total P&L.
- **D2:** the top 400 by discovery positive-week share, among wallets with at least 8 discovery weeks and positive discovery P&L.
- **D3:** a seeded random 400 from the sweet-spot band: discovery P&L between $1k and $100k and at least 8 discovery weeks.
- **D4:** a seeded random 300 from all qualifying wallets, as an unconditional baseline.

Each wallet's tier is in `data_disc/wallets.csv` (`tier_disc`). Residual bias: the collected union came from today's boards, so a wallet that did well before the cutoff but then fell off every board is missing. The verifier reports each claim against the U_disc baseline.

**`trade` claims**
- Evaluated on: holdout BUY trades by U_disc wallets passing `wallet_filter`, matching `trade_filter`, in resolved markets.
- Payoff: size × 1{won} − size × price − fee.
- Reported: trades, wallets, markets and events; total P&L, per-trade P&L and ROI; a 95% bootstrap CI clustered by event; the share of positive weeks.
- PASS: CI lower bound > 0, with breadth of ≥ 5 wallets, or ≥ 200 trades across ≥ 30 markets.
- The same statistics across all collected wallets are reported but not scored.
- Strategy-level bonus check, where market-wide tapes exist: the same trade filter applied to every trader in a sample of the selected markets.

**`wallet` claims**
- Evaluated on: U_disc wallets passing `wallet_filter`.
- Metric: holdout sum of weekly `net` or `net_ex_incentives` per wallet.
- PASS needs all of:
  - ≥ 5 wallets;
  - mean > 0, with a 95% bootstrap CI over wallets > 0;
  - excess over controls > 0 with CI > 0. Controls are unselected U_disc wallets matched on discovery-P&L decile and dominant market type.

**`sweet_spot` claims**
- A wallet PASS, plus:
  - median holdout positive-week share ≥ 80% and median positive-day share ≥ 60%;
  - discovery total |P&L| between $1k and $100k for every selected wallet.

**`entity` claims**
- At least 3 wallets.
- The verifier recomputes the linkage: on-chain shared owner, funder or withdrawal address, or ≥ 2 independent behavioral evidence types from `clusters/`.
- Combined holdout P&L > 0, with ≥ 80% positive weeks and ≥ 60% positive days.

**Distinctness.** A claim whose selected trades (or wallets) overlap ≥ 50% with an earlier verified claim of yours earns no points.

## Points

- +3 per verified edge (`trade` or `wallet` PASS).
- +2 if it matches your round-0 pre-registration.
- +2 if replicable. Mechanical rule: every selected trade's market type exists on Kalshi or Polymarket US, and no condition requires sub-minute reaction to an external event.
- +3 per verified entity.
- +3 per verified sweet spot.
- +1 if the strategy-level check passes.
- −1 per claim that fails verification.

Ties go to more verified holdout P&L explained.

Round 2: you see your results and may revise each failed claim once.
