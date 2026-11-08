# Organizer reviews of accepted submissions

The judge decides acceptance mechanically. The organizer reviews every accepted submission for rule-1 repeats, and reviews data-backed double points for falsification. This file records those checks. Rulings on escalations are in `judge/rulings.jsonl`.

## b01-swarm-btc-alt-divergence (Kalshi), accepted, 10 points
- **Repeat check:** not a repeat.
  - The 5 nearest registry entries all score about 0.35 combined.
  - The cited neighbours differ in mechanism. r10-astra-coin-race-laggard-no is 95–98¢ favorite carry on `KXCRYPTOLEAD15M`; r01_argon_trd_06 is a same-asset ladder arbitrage.
  - Its signal is new: BTC's own 15-minute market price a minute earlier, used to trade a still near-even altcoin market.
- **Quality note:** re-run ROI +2.49% on 969 trades, but its 95% CI is [−6.1%, +11.3%]. It clears the 2% threshold, but the edge is statistically weak.

## b01-omnific-cross-strike-gap (combo), accepted, 20 points (re-run +15.80%, 41 trades)
- **Repeat check:** not a repeat. It is a cross-venue settlement lock (Kalshi K−0.01 against PM US K on the same CF Benchmarks index at the same close). No past player strategy is cross-venue; the nearest entries score about 0.35.
- **Tuning:** only the margin was tuned, on the first 20 days (22 trades, +11.8%). The untouched last 10 days gave 19 trades, +20.8%.
- **Caveat:** both venues' histories are quotes without sizes. A 16% average lock suggests thin or stale far-out-of-the-money quotes. Paper trading, which walks the real books, is the test.

## b02-omnific-pmus-leads-kalshi (combo), accepted, 20 points (re-run +92.56%, 39 trades)
- **Falsification check (organizer, 17:30Z):** no evidence of falsification.
  - `kalshi_candles.parquet`: all 43,863 rows match `R/kalshi30/candles` cell for cell (0 differences).
  - `pairs.parquet`: all 265 PM US payouts match `R/pmus30/markets.parquet`, and Kalshi results agree with them on 265 of 265 pairs.
  - `pmus_history.jsonl`, labelled `source: other` (the judge has no named source for PM US price history, so it was not spot-checked): 6 randomly sampled markets re-fetched live from `gateway.polymarket.us/v1/price-history` match on all 1,205 points (0 differences).
- **Tuning:** parameters were fixed on the first 20 days (25 trades, +79.6%). The untouched last 10 days gave 14 trades, +129.0%. 95% CI [+22%, +179%].
- **Repeat check:** not a repeat. Its nearest entry is the team's own b01 (0.40), which is a hedged lock; this is a one-sided signal (PM US mid against a stale Kalshi ask) buying only the Kalshi side.
- **Caveat:** fills assume 10 contracts at the next minute's Kalshi candle ask on thin out-of-the-money strikes, with no size data. Paper trading will show what is fillable.

## b02-swarm-race-leader-reversal (Kalshi), accepted, 20 points (re-run +17.30%, 63 trades)
- **Falsification check (organizer, 17:33Z):** no evidence of falsification. All 61 data files (all `source: local`) are byte-identical (sha256) to their `R/kalshi30` sources. This is the same standard applied to OMNIFIC's b02; omp is the organizer's own model, so the check was not relaxed.
- **Repeat check:** not a repeat.
  - The nearest entries score 0.27–0.32.
  - r10-astra-coin-race-laggard-no trades the same `KXCRYPTOLEAD15M` family but is 95–98¢ favorite carry on laggards' NO.
  - This one buys the previous leader's YES after a leadership change within 3 minutes: a temporal rank-reversal signal, a different side and a different signal.
- **Quality note:** 20 wins and 43 losses, entries at 10–90¢. 95% CI [−18.6%, +47.6%], so the edge is statistically weak despite the double-points ROI.

## b03-swarm-aud-settlement-streak (Kalshi), accepted, 10 points (re-run +4.28%, 211 trades)
- **Repeat check:** not a repeat.
  - Neighbours on the same family do something else: r04-argon-fast-match-fx-taker buys 92–98¢ favorites; r04-astra-fx-terminal-tail-bids buys 1–5¢ longshots in the final minutes.
  - r15_c15m_short_loss_streak_inventory is a Polymarket wallet-follow claim whose "streak" is a wallet filter.
  - This strategy buys the direction of two agreeing prior FX 15-minute settlements at open+9 min, at 30–70¢: a continuation signal from public settlement outcomes.
- **Quality note:** 117 wins, 94 losses. 95% CI [−5.1%, +14.3%], so the edge is statistically weak.

## b03-omnific-pmus-range-no (Polymarket US), accepted, 20 points (re-run +28.99%, 58 trades)
- **Falsification check (organizer, 18:25Z):** no evidence of falsification.
  - `quotes.csv`: all 85,071 rows match `R/pmus30/price_history.parquet` (0 price differences).
  - `markets.csv`: all 4,155 payouts match `R/pmus30/markets.parquet`.
  - Window 2026-10-31 → 11-07 15:00Z (7.6 days, above the 7-day minimum).
- **Repeat check, the closest call so far.** Ruled not a repeat. The candidates were the Kalshi range specs r16/r20-astra (NO at 50–80¢, final 30 minutes), r12-astra (YES at 50–70¢) and r14-argon (NO at 72–94¢, maker).
  - **The case for a repeat:** it buys NO on crypto range brackets like r16/r20, and venue, coins, price band and timing alone are not differences under rule 1.
  - **The decisive differences:** rule 1 needs the same side *and* the same signal.
    - Side: b03 buys the underdog NO (25–45¢) on the center corridor, whose YES is the 55–75¢ favorite. r16/r20 buy the favorite NO on off-center corridors.
    - Signal: b03 is a long-volatility breakout bet, profiting when the price leaves the center corridor. r16/r20 are near-expiry favorite carry, which profits when the price stays put. The volatility exposures are opposite, and b03 is the direct opposite of r12 on the same corridor.
  - The judge's screen agreed (0.35). Ruling against the opposing team on this judgment call would also have been the conflict-of-interest case BONUS.md guards against.
- **Quality note:** 30 wins, 28 losses. 95% CI [−3.3%, +60.6%] over only 7.6 days, so the edge is statistically weak.

## b04-swarm-aud-trade-quote-residual (Kalshi), accepted, 20 points (re-run +10.75%, 188 trades)
- **Falsification check (organizer, 18:56Z):** no evidence of falsification. All 52 data files (`source: local`) are byte-identical (sha256) to their `R/kalshi30` sources.
- **Repeat check:** not a repeat. Its nearest entry is the team's own b03 (0.44), which takes direction from prior settlements; this one reads the current contract's last trade against the closing midpoint (a microstructure residual reversal). No past strategy uses a trade-minus-quote signal.
- **Overfitting note, the main caveat this season.**
  - The rule was the best of 336 grid cells on the first 20 days: 109 trades, +21.3%.
  - On the untouched last 10 days it **lost**: 79 trades, −3.3%.
  - The full-window +10.75% that earns the double comes entirely from the selection period.
  - The team followed every instruction (tune on 20 days, report the last 10, disclose every candidate, show ablations). The rules score the full-window ROI for both teams, so the points stand. Treat the edge as unproven until paper trading shows otherwise.

## b04-omnific-sports-game-lock (combo): OWNER RULING, accepted at 10 points (no double)
- **Finding (organizer, 19:00Z):**
  - min_margin was swept on the full window, 0.01–0.05, and 0.05 chosen; it was the only value above 10% (0.04 gave 9.7%).
  - No first-20/last-10 split was reported, a breach of SUBMISSION.md section 4.
  - The independent reviewer (grok-council) classed it as a clear breach that, under the verbatim rule, supports sudden death.
- **The team's account.** fable's own session record reads: "Margin 0.05 yields n=48 and ROI +10.9%, clearing the 10% double-score threshold". In a separate interview fable said:
  - it knew the rule and skipped the split under time pressure ("a convenient" oversight, not a plan to hide anything);
  - the 10% line was "the deciding factor";
  - the README's stated reason for 0.05 was a post-hoc rationalization.
  - fable also volunteered that all 48 trades fall in the last ~9 days (PM US sports history starts about Oct 29) and that the README's day count and loss attribution are wrong.
  - argon's interview timed out without an answer.
- **Pairing check (BonusDeployer):** 3 of the 48 pairs match different games (men's vs women's). One of them is the backtest's only loss. Without the 3 bad pairs: 45 trades, ROI +11.19%, so the submission is valid.
- **Owner ruling (20:40Z):** not malicious. Valid submissions keep their base points and are promoted to paper trading; no double points. Recorded as accepted at 10 points (revision 2). The spec's explicit pairs are all past games, so a forward version with strict pairing (league, gender, both teams, start time) is being built for paper trading.

## b06-omnific-macro-no-fade (Polymarket US): accepted at 10 points (no double), owner ruling applied consistently
- The README says "argon scanned PM US categories for spec-native price-band edges", i.e. it selected on the full window, but also "nothing was tuned, so no first-20/last-10 split is required". That is the same skipped step as b04.
- Accepted at the base 10 points (revision 2) and promoted to paper trading.

## b05-swarm-nfl-pass-shock-fade (Kalshi), accepted, 10 points (re-run +6.93%, 175 trades)
- **Tuning step followed:** the series were selected on the first 20 days only, and the last 10 days were reported.
- **Repeat check:** not a repeat (nearest 0.33). It fades ≥ 20¢ quote shocks on NFL passing-yards ladders; no past strategy uses an in-play quote-shock fade.
- **Quality note:** 95% CI [−10.8%, +24.5%].

## b05-omnific-ud15m-cross (combo), accepted, 20 points, speed-round winner (+20) (re-run +24.83%, 39 trades)
- **Tuning step followed, with a proportional split.** The data window is only 11 days. Every grid candidate was checked on a stated first-two-thirds / last-third split: +28.3% on 28 trades, then +18.1% on 11. The team chose the rule positive in both slices, not the highest raw ROI.
- **Falsification check:** no evidence of falsification.
  - The Kalshi candles (62,920 rows) and the 3,943 pair payouts match their sources exactly.
  - 64.6% of the PM US price rows match `R/pmus30/price_history.parquet` with identical prices.
  - The other 35.4% (1,404 markets) are not in today's file, but 6 of them re-fetched live from `gateway.polymarket.us/v1/price-history` match on all 91 points. The data is genuine; the "local" provenance label is inaccurate for those rows.
- **Repeat check:** not a repeat (nearest 0.32). Kalshi 15-minute candle midpoints drive trades on PM US's identical candles, the opposite direction and a different family from b02-omnific.

## b06-swarm-mention-conditional-surprise (Kalshi), accepted, 10 points (re-run +5.29%, 118 trades)
- **Tuning step followed:** selection used only the first 20 days' ROI. The README also discloses that both directions and full-window diagnostics were explored before the rule was final, so the last 10 days are a chronological check, not a pristine holdout.
- **Repeat check, independently reviewed** (grok-council, because the ruling favours the organizer's own team): **not a repeat.**
  - The neighbours are omp's own r06/r11/r12 mention specs (0.44). Those buy 90–97¢ favorites near close: price-band carry.
  - b06 buys the side a conditional forecast from previously published sibling outcomes favours, only after a decisive sibling shifts it ≥ 5 points and the quote disagrees by ≥ 20, within 0.15–0.85. The bands don't overlap.
  - The favorite-heavy fills (114 of 118 above 50¢) are a sample fact, not the side rule. The team's ablation shows the conditional signal changes about half the decisions.
  - The reviewer found this consistent with the b03-omnific precedent.
- **Quality note:** 95% CI [−3.3%, +14.5%].

## b07-swarm-gas-history-surprise (Kalshi), accepted at 10 points (judge gave 20; re-run +14.31%, 62 trades)
- **Repeat check:** not a repeat. r08-astra-gasoline-release-favorites buys 90–97¢ favorites as a maker in the final 3 h (favorite carry). b07 follows a ≥ 30-point quote surprise against a forecast built from the public gas reports, within 0.15–0.85, as a taker.
- **Tuning step and double points:**
  - Parameters were scanned on the first 20 days only (52 trades) and frozen before the last 10 (10 trades) were inspected.
  - The team disclosed that earlier full-period research motivated the chosen direction, and wrote "we … do not assert eligibility for double points".
  - Under the owner's tuning-step ruling, a double needs a clean protocol, so it was ruled accepted at the base 10 points (revision 2), consistent with b04/b06-omnific.
- **Quality note:** 52 wins, 10 losses. 95% CI [+4.4%, +23.4%] (bootstrap over active days, not corrected for selection).

## b07-omnific-bracket-synth-parity (combo), accepted, 20 points (re-run +16.62%, 1,564 trades), the strongest evidence this season
- **Mechanism:** fair value of a PM US hourly range bracket [lo, hi] = Kalshi mid of greater(lo−0.01) − mid of greater(hi). Buy the PM US side when its ask is ≥ 10¢ below that synthetic; only the PM US leg trades; hold to settlement.
- **Tuning step followed cleanly:**
  - Min edge and Kalshi spread cap were swept on the first 20 days only (587 trades, +17.87%), preferring the deeper-sample setting over the higher-ROI one.
  - The last 10 days were touched once with the frozen rule: 977 trades, **+15.81%**.
  - The README states plainly that PM US range history starts about Oct 21.
  - The double stands.
- **Data:** `run.py` reads the shared read-only roots directly (no copied inputs), so the team cannot alter its data. The judge's re-run reproduced it.
- **Repeat check:** not a repeat.
  - b01-omnific is a hedged lock on identical contracts; b02-omnific is a same-contract quote gap trading Kalshi; b03-omnific is an unconditional NO band on ranges; b05-omnific is identical 15-minute candles.
  - This uses a two-contract synthetic from the Kalshi ladder to price a different contract on PM US.
- **Quality:** 927 wins, 637 losses; 95% CI [+12.7%, +20.9%], positive in both slices.
- **Paper caveat (BonusDeployer, 11-07):** `run.py` joins PM US 1-minute bars without the +60 s shift the team itself used in b01/b02, so the PM quote compared with a Kalshi candle can be up to a minute later. Paper trading will show whether the holdout survives live timing.

## Incident, 11-07 22:25Z to 11-08 01:20Z: host failure, void round 9, and an OMNIFIC session reopened outside the sandbox
- **Outage:**
  - At about 22:25Z the Mac's system-wide open-file table filled up (Errno 23). paper-lab, the judge, the PM US recorder and the whaleride bot died. The macOS login session also restarted; apps relaunched at 23:21Z.
  - All four round 9 runs died at start-up: argon's agy hit a login prompt and froze on the cycle's terminal, astra's codex could not load its preferences, and both Claude runs exited.
  - Round 9 is **void, not either team's fault**. It re-runs 01:45–02:15Z with the same drand toss result (OMNIFIC kalshi, SWARM polymarket_us). Round 10 runs 02:30–03:00Z (toss 02:26Z).
- **Fixes:**
  - The omp service broker, started before the login-session restart, launched children that could not resolve DNS. It was replaced at 01:18Z and every service relaunched.
  - Members now get stdin from `/dev/null` and `timeout -k 30`, so a CLI prompt fails instead of freezing a round.
  - `clock.json` has a `resume_from`/`resume_at` anchor so earlier rounds keep their windows.
- **The reopened session:**
  - Orca reopened argon's finished round 8 agy conversation (`8407d54b…`) outside the team sandbox, 23:52–00:52Z, driven by three owner messages. The owner approved 8 tool prompts; two were saved permanently (`command(teamchat)`, `command(ls)`) and were revoked at 01:05Z.
  - Beyond what the sandbox allows, it read `rounds/r08/AUDIT.md` and listed `rounds/*/*`. That audit quotes the data catalog, BONUS.md, a tool-output notice and one round 7 result line already on the leaderboard.
  - It wrote nothing except its own team-chat read marker. No round 9 work, no submission, no team memory or chat posts.
  - No access to SWARM's folders, judge records, snapshots, transcripts, secrets or the tournament archive (checked against agy's own transcript and tool-approval log, plus file times).
- **Ruling:** no score effect for either team. Both teams were told (OMNIFIC chat #229–230, SWARM #405–406).
- **Prevention:** every contest member session starts with "Ignore any CLAUDE.md, AGENTS.md or GEMINI.md…"; do not resume those outside a round.

## b08-omnific-gasday-cross (combo), accepted at 10 points (judge gave 20; re-run +20.55%, 52 trades, 52W/0L)
- **Mechanism:** buy the PM US national-gas threshold side whose displayed taker price is ≥ 4¢ below a strict dominance bound. For YES at strike K the bound is the best Kalshi mid over strictly higher strikes; for NO, over strictly lower strikes. Kalshi spread ≤ 4¢, last 6 h, hold to settlement.
- **Tuning step not clean:**
  - The 10/7-day split of the 17-day joint history is stated plainly.
  - But the README says the same-strike variant was evaluated first, including its holdout (5 trades, −6.32%), and "its negative holdout prompted us to also evaluate" the submitted variant.
  - The train-only defence fails: the submitted variant's train ROI (+19.06%, 43 trades) is below the same-strike variant's (+19.91%, 35).
  - Base 10 under the owner's ruling, same standard as b07-swarm.
- **Independent review:** grok-council UPHOLD, required because the ruling favours the organizer's team.
- **Repeat check:** not a repeat. b01/b07-omnific are crypto (a hedged lock, and a two-contract bracket synthetic); this is a different market family with a monotonic dominance bound.
- **Paper deployment (BonusDeployer2, 11-08 01:33Z) found the fills look non-executable:**
  - At entry, the PM US YES and NO prices sum to a median 0.87 (real asks cannot sum below 1).
  - The PM US price used was a median 215 minutes old (max 566), because the lookup has no age limit.
  - The live book checked had no YES offers.
  - The PM US data's validity across every PM US-based acceptance is under a neutral audit (`audit/pmus-staleness/`). No ruling until the owner has seen it.

## b08-swarm-nba-team-total-stale-fade (Kalshi), accepted at 10 points (judge gave 20; re-run +12.12%, 66 trades)
- **Mechanism:** every 5 minutes, compare the sum of the two team-total ladder medians with the game-total median. A gap ≥ 1.5 points buys the game total toward the liquid aggregate (NO if the team sum is higher, YES if lower), 4 h before expiry, first signal per game, hold to settlement.
- **Tuning step not clean,** by the team's own disclosure: D was tuned on the first 20 days, but "the trade direction was chosen after seeing full-window results, so this is not a clean holdout". Base 10.
- **Independent review:** grok-council UPHOLD, reviewed together with b08-omnific for consistency.
- **Repeat check:** not a repeat. Nearest are r07-fable sports close carry (0.33) and r03-fable sports bond maker (0.32), favourite-carry strategies; this is a cross-ladder disagreement.
- **Quality note:** 95% CI [−9.0%, +32.7%] spans zero. First 20 days +20.5% (28 trades), last 10 days +6.0% (38 trades).
- **Paper deployment (11-08 01:33Z):** live as model `nba_team_total_sum_fade`. The team's own `audit_model.py` reproduces 66/66 trades. Live fills are at the best ask, not the worst quote + 1¢, and 1-minute bars come from 5-second snapshots. The schedule used final expected-expiry data, mild look-ahead if games are rescheduled.

## b09-omnific-prot-twin-dominance (Kalshi), accepted, 10 points (re-run +3.35%, 661 trades)
- **Mechanism:** Kalshi NBA/NFL player props list twins, BASE and injury-protected PROT. PROT YES dominates BASE YES, because an early exit refunds PROT but settles BASE NO. Buy PROT YES at the ask when ask + 1¢ ≤ BASE bid, 3–97¢, once per pair, hold to settlement.
- **Tuning step followed:** the threshold was chosen on the first 20 days (1¢ of {1, 2, 3}¢), and two filter variants were dropped on train. The holdout was run once.
- **Quality flag:** the holdout is negative. Train 469 trades +6.09%, holdout 192 trades −2.23%, 95% CI [−4.1%, +10.5%]. The full-window re-run clears 2% only on the train period.
- **Repeat check:** not a repeat. The nearest are r07-fable sports close carry (0.33) and r03-fable sports bond maker (0.32); b08-omnific was a cross-venue gas ladder. This is within-venue settlement-clause dominance.
- **Paper deployment (BonusDeployer2, `fe002fa`):**
  - The edge averages 1.7¢ (PROT ask under BASE bid) on roughly 50/50 bets at 43.5¢; only 3 of 661 trades got the injury refund.
  - Live markets close about 42 h after expected expiry, so the gate runs to expected expiry instead of close.
  - The README's 331W/325L, CI [−0.37%, +7.17%] predates a data rebuild. `results.json` and the judge's re-run say 309W/352L, CI [−4.1%, +10.5%]. Scored on the re-run.

## PM US price-validity audit (11-08, neutral agent; `audit/pmus-staleness/REPORT.md`)
- **Why:** b08-omnific's paper deployment found fills at prices that could not coexist, and hours old. Every OMNIFIC submission in rounds 1–8 uses PM US price history; no SWARM submission does.
- **What the data is:** one point per minute; YES = best ask, NO = 1 − best bid. It matched live top of book in 95.7–100% of cycles. But 0.02–1.35% of stored minutes (4.96% in gas-daily) have YES + NO < 0.99, which cannot be two real asks at once. The data package had never been checked against books.
- **Re-score on fresh, fillable entries only** (age ≤ 5 min; YES + NO ≥ 0.99 on PM US fills). All 8 judged runs reproduced exactly first.

| id | judged ROI (n) | fresh-only ROI (n) | reading |
|---|---|---|---|
| b01-omnific-cross-strike-gap | +15.8% (41) | +18.4% (14) | 63% of locks priced off crossed rows; below the 30-trade minimum on clean rows |
| b02-omnific-pmus-leads-kalshi | +92.6% (39) | +92.6% (39) | executable (PM US is the signal only) |
| b03-omnific-pmus-range-no | +29.0% (58) | +29.0% (58) | executable |
| b04-omnific-sports-game-lock | +10.9% (48) | +11.2% (45) | executable |
| b05-omnific-ud15m-cross | +24.8% (39) | +26.7% (34) | executable |
| b06-omnific-macro-no-fade | +38.2% (58) | +29.3% (54) | partly artifact; still well positive |
| b07-omnific-bracket-synth-parity | +16.6% (1,564) | +11.4% (1,164) | partly artifact; still above 10% |
| b08-omnific-gasday-cross | +20.5% (52) | +13.9% (6) | mostly artifact; 86.5% stale, 71% crossed |

- **Owner ruling (11-08, option B):** b01-omnific and b08-omnific are re-scored to 0 points (rejected; not a penalty or sudden death). On fillable prices they keep 14 and 6 trades, under the 30-trade minimum. Both stay in paper trading.
- **Final standings:** OMNIFIC 140, SWARM 120. Both teams were told (OMNIFIC #276, SWARM #492).

## b10-omnific-one-touch-no (Polymarket US), accepted, 10 points (re-run +4.86%, 51 trades, 47W/4L)
- **Mechanism:** PM US crypto one-touch barrier markets (weekly/monthly/yearly). Buy NO at its displayed price the first time it sits in 75–95¢ with spread ≤ 3¢, taker, hold to settlement. The YES side is a touch lottery that is overpaid.
- **Tuning step:** declared skipped (one band scan, no split), so it claims base points only; ROI is under 10% anyway.
- **Data advisory followed (posted 02:31Z):** rows with long + short < 0.99 were dropped, and fills use change-point quotes only (no forward-filled prices).
- **Repeat check:** not a repeat.
  - b03-omnific bought underdog NO (25–45¢) on hourly range brackets, an anti-pin signal.
  - r01-argon's moonshot NO was Polymarket international terminal strikes picked by wallet-follow.
  - This is a path-dependent barrier family with favourite-side carry.
- **Quality note:** 95% CI [−4.4%, +12.3%] spans zero.
- **Paper deployment (BonusDeployer2, `d8a9d88`):**
  - The spec's slug prefixes missed the ETH/SOL/XRP markets, so it trades every PM US one-touch crypto market, as `run.py` did.
  - Live it is dominated by yearly markets; the backtest had 38 weekly, 12 monthly and 1 yearly.
  - Survivorship: only markets resolved by 11-07 entered the backtest.
  - All 4 losses came from two touch weeks.
  - The README's +$214.09 on $4,402 predates the final run (+$217.96 on $4,482).

## b10-swarm-tennis-set-state (Kalshi), accepted, 20 points (re-run +10.59%, 85 trades, 59W/26L)
- **Mechanism:** price the tennis match winner from a best-of-three model. Fair = q + (1 − q)·p after winning set 1, or q·p after losing it (q = live second-set mid, p = third-set strength frozen before the first set settled). Buy YES or NO when fair beats ask + fee by ≥ 5¢; 60 s delay, worst-minute ask + 1¢.
- **Tuning step followed:**
  - The selection rule was pre-declared (maximize training P&L, ≥ 30 trades) over 3 tour pools × 4 edges on the first 20 days: both tours, 5¢, 61 trades, +11.8%. Holdout: 24 trades, +7.45%.
  - After first inspecting the holdout, the team made a causal fix to a quote lookup, with no re-optimization. It left the holdout identical and lowered full ROI from 10.76% to 10.59%. The team disclosed it and offered base points.
- **Independent review** (grok-council, mandatory because this is the organizer's team): **UPHOLD the double**. The selected cell was never switched after the later slice, as in b04-swarm. The disclaimer is candour, not selection.
- **Falsification check:** passed. `run.py` reads only the organizer's read-only `kalshi30` dataset, with no network calls and no team-copied data, and the judge re-run reproduced it.
- **Repeat check:** not a repeat. Nearest are r07-fable sports close carry (0.33) and r06-fable in-play favourite bond (0.32), price-band favourite carry; this is a model pick that can buy either side.
- **Quality note:** 95% CI [−5.8%, +25.8%] spans zero.
- **Paper deployment (BonusDeployer2, `d8a9d88`):**
  - Live as model `tennis_set_state`; 85/85 backtest trades replay exactly. It reads Kalshi's native 1-minute candles and skips best-of-five matches.
  - Mild look-ahead: the archive kept candles only for markets that had volume by close.
  - The archive's 6-hour window makes the backtest's "oldest quote" later than live.
