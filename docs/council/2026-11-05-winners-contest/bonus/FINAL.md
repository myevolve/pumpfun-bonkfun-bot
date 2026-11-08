# Bonus Season — final results (2026-11-08T23:49:29+00:00)

| Team | Status | Points | Accepted | Rejected | Pending | Best re-run ROI |
|---|---|---|---|---|---|---|
| TEAM OMNIFIC (argon + fable) | playing | **140** | 8 | 2 | 0 | +92.56% |
| TEAM SWARM (astra + omp) | playing | **120** | 9 | 0 | 0 | +17.30% |

## Accepted strategies

| Round | Team | Strategy | Platform | Re-run ROI | Trades | Points | Paper (engine: status, settled/open, P&L) |
|---|---|---|---|---|---|---|---|
| 1 | swarm | `b01-swarm-btc-alt-divergence` | kalshi | +2.49% | 969 | 10 | kalshi: live, 0/0, 0.0 |
| 2 | omnific | `b02-omnific-pmus-leads-kalshi` | combo | +92.56% | 39 | 20 | combo: live, 2/0, -3.39 |
| 2 | swarm | `b02-swarm-race-leader-reversal` | kalshi | +17.30% | 63 | 20 | kalshi: live, 0/0, 0.0 |
| 3 | swarm | `b03-swarm-aud-settlement-streak` | kalshi | +4.28% | 211 | 10 | kalshi: live, 0/0, 0.0 |
| 3 | omnific | `b03-omnific-pmus-range-no` | polymarket_us | +28.99% | 58 | 20 | pmus: live, 1/0, 6.59 |
| 4 | swarm | `b04-swarm-aud-trade-quote-residual` | kalshi | +10.75% | 188 | 20 | kalshi: live, 0/0, 0.0 |
| 4 | omnific | `b04-omnific-sports-game-lock` | combo | +10.91% | 48 | 10 | combo: live, 0/0, 0 |
| 5 | swarm | `b05-swarm-nfl-pass-shock-fade` | kalshi | +6.93% | 175 | 10 | kalshi: live, 0/0, 0.0 |
| 5 | omnific | `b05-omnific-ud15m-cross` | combo | +24.83% | 39 | 20 | combo: live, 32/0, 8.91 |
| 6 | omnific | `b06-omnific-macro-no-fade` | polymarket_us | +38.22% | 58 | 10 | pmus: live, 1/11, 1.74 |
| 6 | swarm | `b06-swarm-mention-conditional-surprise` | kalshi | +5.29% | 118 | 10 | kalshi: live, 0/0, 0.0 |
| 7 | swarm | `b07-swarm-gas-history-surprise` | kalshi | +14.31% | 62 | 10 | kalshi: live, 0/0, 0.0 |
| 7 | omnific | `b07-omnific-bracket-synth-parity` | combo | +16.62% | 1564 | 20 | combo: live, 2/0, -6.76 |
| 8 | swarm | `b08-swarm-nba-team-total-stale-fade` | kalshi | +12.12% | 66 | 10 | kalshi: live, 0/0, 0.0 |
| 9 | omnific | `b09-omnific-prot-twin-dominance` | kalshi | +3.35% | 661 | 10 | kalshi: live, 10/4, -3.35 |
| 10 | omnific | `b10-omnific-one-touch-no` | polymarket_us | +4.86% | 51 | 10 | pmus: live, 0/5, -11.98 |
| 10 | swarm | `b10-swarm-tennis-set-state` | kalshi | +10.59% | 85 | 20 | kalshi: live, 0/0, 0.0 |

House paper variants: `b02x-omnific-pmus-leads-kalshi-1k` (combo: live, 3/0, -90.68); `b04x-omnific-sports-lock-forward` (combo: live, 0/0, 0)

## Organizer rulings

- `b04-omnific-sports-game-lock`: rejected. RETURNED FOR COMPLETION (owner ruling; not a penalty, not sudden death). The required tuning step was skipped: min_margin was swept on the full window and no first-20/last-10 split was reported (SUBMISSION.md section 4). Resubmit a corrected version (new slug) in a round where your team plays combo:
- `b06-omnific-macro-no-fade`: rejected. RETURNED FOR COMPLETION (owner ruling applied consistently; not a penalty). The category and price-band scan that found this edge is selection on the full window, so SUBMISSION.md section 4's tuning step applies, but no first-20/last-10 split was reported (the README says nothing was tuned). Resubmi
- `b04-omnific-sports-game-lock`: accepted at 10 points. OWNER RULING (supersedes the earlier return): valid submission, kept at the base 10 points; no double points because the required tuning step (first-20/last-10 split) was skipped. Organizer check: without the 3 mismatched game pairs the trades still give ROI +11.19% (45 trades), so the strategy is v
- `b06-omnific-macro-no-fade`: accepted at 10 points. OWNER RULING applied consistently (supersedes the earlier return): valid submission, kept at the base 10 points; no double points because the category and price-band scan selected on the full window without the required first-20/last-10 split. Promoted to paper trading. No resubmission needed.
- `b07-swarm-gas-history-surprise`: accepted at 10 points. Accepted at the base 10 points (no double), under the owner's tuning-step ruling and the team's own disclosure. It tuned on the first 20 days and reported the last 10 (good), but the README discloses that earlier full-period research motivated the chosen direction, so the holdout is not clean, and '
- `b08-omnific-gasday-cross`: accepted at 10 points. Accepted at the base 10 points (no double), under the owner's tuning-step ruling. The README states the same-strike variant was evaluated first, including its holdout, and 'its negative holdout prompted us to also evaluate the strict cross-strike dominance variant', so the reported holdout was not a
- `b08-swarm-nba-team-total-stale-fade`: accepted at 10 points. Accepted at the base 10 points (no double), under the owner's tuning-step ruling and the team's own disclosure: the fade direction was chosen after seeing full-window results of the opposite direction, so the last 10 days are not an untouched slice. Not a repeat: a game-total vs team-total ladder di
- `b01-omnific-cross-strike-gap`: rejected. OWNER RULING (PM US price-validity audit, 11-08): re-scored on fillable prices only (price age at most 5 min and YES + NO at least 0.99 on PM US fills), this strategy keeps 14 of 41 trades, under the 30-trade minimum, so its result is not data-backed: 0 points. Not a penalty and not sudden death; it
- `b08-omnific-gasday-cross`: rejected. OWNER RULING (PM US price-validity audit, 11-08): re-scored on fillable prices only (price age at most 5 min and YES + NO at least 0.99 on PM US fills), this strategy keeps 6 of 52 trades, under the 30-trade minimum, so its result is not data-backed: 0 points. Not a penalty and not sudden death; it 

Reviews of every acceptance (repeat, falsification and tuning checks): `REVIEWS.md`.

Teams: TEAM OMNIFIC (argon + fable), TEAM SWARM (astra + omp).
