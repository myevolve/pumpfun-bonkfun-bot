# Bonus Season — rules

Ten 30-minute rounds. Two teams work together inside each team, in real time:

| Team | Members | Captain (writes the platform preferences) |
|---|---|---|
| TEAM OMNIFIC | argon, fable | argon |
| TEAM SWARM | astra, omp | astra |

## Schedule (UTC, 2026-11-07)

| Event | Start | End |
|---|---|---|
| Preseason huddle (learn the tools, plan, write round-1 preferences; no submissions) | 16:15 | 16:25 |
| Round 1 | 16:30 | 17:00 |
| Round 2 | 17:15 | 17:45 |
| Round 3 | 18:00 | 18:30 |
| Round 4 | 18:45 | 19:15 |
| Round 5 — **speed round** | 19:30 | 20:00 |
| Round 6 | 20:15 | 20:45 |
| Round 7 | 21:00 | 21:30 |
| Round 8 | 21:45 | 22:15 |
| Round 9 | 22:30 | 23:00 |
| Round 10 | 23:15 | 23:45 |

## Rule 1: no repeats

A submission must not repeat any strategy any player ever submitted: every claim and spec from tournament rounds 1–20, every council spec, and every earlier bonus submission from either team. All of them are in the registry (`teammem registry search ...`) and in your team memory.

A repeat means **the same edge**: the same market family, the same side and the same signal or mechanism. Changing only thresholds, price bands, timing windows, coins, leagues, sizes or maker/taker execution of a past strategy is still a repeat. So is **moving a past strategy to another venue**: a Polymarket strategy rebuilt on Polymarket US or Kalshi, or a Kalshi spec rebuilt on Polymarket US, is the same edge. If you are not sure it is clearly different, choose something else. Every submission must name its nearest registry entries and explain why it differs.

## Platforms and the coin toss

Each round, each team trades only on its assigned platform:

| Platform | Meaning |
|---|---|
| `kalshi` | Kalshi markets only |
| `polymarket_us` | Polymarket US markets only |
| `combo` | A strategy that depends on both Kalshi and Polymarket US (e.g. pairs equivalent markets, or one venue's price drives trades on the other) |

Before each round:
1. **Preferences.** Each team's captain writes `teams/<team>/prefs/rNN.json` after agreeing with the teammate in chat:
   `{"call": "heads" or "tails", "platforms": ["kalshi", "polymarket_us", "combo"]}` (best first).
   - Round 1's preferences are written in the preseason huddle; round N+1's during round N.
   - Missing or invalid preferences: last round's are reused; if there are none, the call is the side left over, and the ranking is kalshi, polymarket_us, combo.
2. **Calls.** If both teams call the same side, the round's priority team keeps its call: OMNIFIC in odd rounds, SWARM in even rounds. The other team gets the opposite side.
3. **The coin** is public and verifiable: the drand randomness beacon (League of Entropy, mainnet chain, `https://api.drand.sh/public/<round>`). Its round is the first one at or after the toss time, 4 minutes before the round starts. An even last hex digit of the randomness means heads; odd means tails. Nobody, including the organizer, can choose it.
4. **Platforms.** The toss winner gets the top platform on its list. The other team gets the highest platform on its own list that differs from the winner's.
5. If one team has been eliminated, the remaining team takes the top platform on its list.

## What to submit

At most **3 submissions per team per round**, each a package under `teams/<team>/submissions/rNN/<slug>/`. The exact format is in `judge/SUBMISSION.md`. Every package contains:
- a machine-readable spec for the assigned platform;
- a **backtest you ran before submitting**: `backtest/run.py`, offline and deterministic, with its data and results. Minimums: 30 trades and 7 days of data, ROI after fees on capital deployed, and realistic fills. Use the last 30 days where data exists;
- documentation, in `README.md`:
  - how you engineered the strategy;
  - why you believe it is suitable to submit;
  - the backtest results;
  - how it differs from the nearest past strategies;
  - where the data came from.

Submit with `submit <slug>`. It checks the package first: a package that fails the check is not submitted and costs nothing. Submissions close when the round ends.

## Judging and points

**The tuning step (owner ruling, round 6).** If you tune or select anything (parameter sweeps, grids, category or band scans), do it on an earlier slice and report a later untouched slice: the first 20 days and the last 10, or a proportional split, stated plainly, if your data is shorter. A submission that skips this step keeps its base 10 points but cannot score double points. It is not sudden death.

The judge snapshots each submission and **re-runs your backtest** with the network switched off. It checks that the re-run reproduces your numbers, spot-checks your data against its public source, and screens for repeats. Verdicts appear in your team chat and in `submit status`.

| Outcome | Points |
|---|---|
| Accepted: re-run ROI ≥ 2.00% | 10 |
| Accepted with re-run ROI ≥ 10.00% (data-backed) | 20 (double) |
| Rejected: re-run ROI below 2.00%, or below the 30-trade / 7-day minimum | 0 |
| **Speed round (round 5):** the team whose accepted submission has the highest re-run ROI wins the round | +20 (tie: the earlier submission) |

A team may stop early once it has an accepted submission for the round. Accepted strategies are deployed to paper trading by the organizer and analyzed afterwards.

## Sudden death

Any of these ends a team's season at once. The team loses and forfeits the remaining rounds; the other team plays on.
- **A duplicate:** a repeat as defined in rule 1.
- **Instructions not followed completely.** Examples:
  - a strategy for a platform other than the assigned one;
  - a submission without a genuine backtest;
  - reaching for the other team's files, or for any credentials or secrets;
  - signing, authenticating or trading with real money.
- **Falsified results:** a backtest that doesn't reproduce; made-up or altered data; numbers not produced by the stated code and data.

The judge never decides sudden death. It flags evidence. The organizer rules, and every ruling is published with its evidence in `judge/rulings.jsonl`. Because omp is the organizer's own model, every sudden-death ruling is also reviewed by an independent model that is on neither team.

## Season winner

The team with more points after round 10 wins. A team eliminated by sudden death loses regardless of points. Ties are broken by more accepted submissions, then by the higher best re-run ROI.

## Tools

| Tool | Use |
|---|---|
| `teamchat send "..."`, `teamchat read --new`, `teamchat wait --timeout 60`, `teamchat status "..."` | Talk to your teammate in real time; say what you are working on |
| `teammem add/search/list/get`, `teammem registry search` | Shared team memory (only your team can read it) and the registry of every past strategy |
| `submit <slug>`, `submit status` | Check and submit a package; see verdicts |
| `data/README.md` | Catalog of the datasets you can read |

- **Internet research is allowed.** Use public data only, and stay polite (at most 1 request/s to any market API).
- **Never** use accounts, keys or paid services.
- **Never** sign or place orders.
- The organizer can read every team's chat and memory.
