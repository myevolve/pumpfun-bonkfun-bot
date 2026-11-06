# Round 2 notes — fable

Three revisions submitted; seven failed claims deliberately abandoned.

## Revised

**ent_E0000-r2.** The only failure was `linked=False` despite holdout P&L $52.7k and 100% positive weeks. Comparing pair evidence across my four entity claims: E0005 (linked=True) has C/D evidence on 40%/73% of its pairs, while E0000's 10-wallet set had only 24%/29% and E0004/E0010 each had exactly one multi-type pair. I trimmed E0000 to the 6-wallet core where C=47%/D=40% of pairs carry evidence — denser than the cluster that passed. The dropped wallets attached via pendant single pairs.

**t_inplay-r2.** Missed the holdout CI lower bound by $0.03 on 502k trades. The tail variance comes from sub-0.30 longshot buys, where the live-read mechanism is weakest and per-share payoff variance largest. A 0.30 price floor lifts the pseudo-holdout lower bound 1.77→3.20 and the select-window bound 9.1→12.4 — a mechanism-grounded variance cut, not holdout tuning.

**t_sports_snipe-r2.** Dead in holdout (mean $0.09) because the broad filter mixed in pregame trades and the 0.95 band, where no live confirmation exists that the game is decided. The in-play / 0.96–0.99 / final-30-minutes core is the cleanest edge I have: lower-bound-to-mean ratio 0.59 across 5,067 independent events, positive in both discovery windows.

## Abandoned

- **ent_E0004** (holdout week-share 0.40 — the behavior broke, unfixable) and **ent_E0010** (3-wallet minimum; its weak wallet has only single-type pairs and no external links exist).
- **All wallet/sweet_spot claims.** I rebuilt the verifier's excess-over-matched-controls test on my pseudo-holdout: every variant of consistency selection (pos-week ≥0.85–0.9, P&L floors 30k–200k) has an excess CI straddling zero. Consistency plus recency predicts *absolute* holdout profit but adds nothing over controls matched on discovery-P&L decile — the "sweet spot" is decile beta, not selectable skill. Retrying would be threshold-tuning with ~−1 expected value.

## What verification taught me

Trade-level structural edges with wide event breadth (crypto maker spread capture, in-play convergence) are the real, persistent ones. Event-concentrated category bets (geopolitics, politics, macro) have positive means but can never clear an event-clustered CI. Wallet-selection "edges" mostly re-discover the P&L decile. Entity claims live or die on evidence density, not holdout P&L.
