# PM US price-history staleness audit — Bonus Season

Read-only data-validity audit, delegated for independence (the organizer is on TEAM SWARM). Both
teams' submissions were screened the same way. Nothing in `judge/`, `teams/`, `rounds/`, or the
data roots was modified. No ruling or points recommendation is made here; that is the owner's call.

**Scope.** All frozen snapshots in `judge/snapshots/` were screened: platform in `strategy.json`
plus every data path read by `backtest/run.py`. All 8 SWARM submissions (rounds 1–8) are
Kalshi-only and read no PM US data. All 8 OMNIFIC submissions from rounds 1–8 use PM US price history
and are audited below. Round 9 added `b09-omnific-prot-twin-dominance` (accepted 01:57Z). It is
Kalshi-only (`run.py` reads only `kalshi30`), so it is out of scope. SWARM drew `polymarket_us` for
round 9 but had no snapshot when the round closed (`cycle.log` "round 9 done" 02:15:42Z). Round 10
had not started when this audit finished.

## 1. What a PM US price-history point is

Source: `GET gateway.polymarket.us/v1/price-history?symbol=<slug>&fidelity=1`
(`pmus30/code/collect.py` L288–299). Evidence is in `semantics.json`, produced by `semantics.py`.

1. **One point per minute, and the API itself repeats values.** The API returns a dense 1-minute grid
   (`collect.py` L5–6, L265; data README L121). In the 265 raw grids that b01 saved, every spacing is
   exactly 60 s, and 28.0% of minutes repeat the previous minute's (YES, NO) pair exactly (C). So a
   point is emitted every minute whether or not the quote moved. The research copy keeps only the first
   row, each change, and the last row (`collect.py` L253–261). Readers forward-fill it (data README
   L121–122). Across the stored history, the median market's value changes once every 48–235 minutes,
   depending on the market family (B).
2. **The timestamp marks the minute's opening.** b01, b02 and b05 shift labels +60 s to wall-clock
   (b01 `run.py` L10–14, L102). Our only overlap check against exact-time recorder books (104 obs,
   2026-11-07 15:13–15:17Z) matched best at label = book minute − 1: YES = best ask 96.2%,
   NO = 1 − best bid 95.2% (D). b04, b07 and b08 apply no shift (see method note 4).
3. **YES is the best ask. NO is 1 − the best bid, which is also the NO ask, because PM US has one
   book.** The data README says so (L118–119: "YES display price, normally the best ask … NO display
   price, normally 1 − best bid … not trades … no size"). The recorder confirms it for the same
   display field in the market listing: on 395,918 two-sided rows, `long_px` = best ask and
   `short_px` = 1 − best bid in 100% of cases. Across all 497,368 listing rows, YES + NO is never below
   1.00 (A; `recorder.py` L93–96). Points carry no size.
4. **The data package never checked the history against real books.** `PROXY_QUALITY.md` L36–37
   ("B. PM US price-history vs book snapshots — No overlap yet"). This audit ran a small check: 5
   public GETs on 5 currently open markets (3 gas, 2 game winners), compared with the 10-minute
   recorder listing (recorder 2026-11-07 15:07Z–2026-11-08 01:18Z; history to 01:45Z; E, raw
   responses in `live_check/`). In 95.7–100% of cycles the history matched the listing's top of
   book. It showed 96–100% of book changes, and no minute had YES + NO < 0.99. **On active books in
   that window, the history tracked the top of book.**
5. **Some historical points are not a single book state.** In a real book, ask ≥ bid, so
   YES + NO = ask + (1 − bid) ≥ 1. Yet the stored history has YES + NO < 0.99 for this share of
   minutes: gas daily 4.96%, macro 1.35%, crypto hourly range 0.06%, crypto 15m up/down 0.29%,
   crypto above 0.27%, game winners 0.02% (B). In 79–94% of those low-sum rows, one side is ≤ 0.12
   and the other sits at an older level (B). [INFERENCE] This fits one side of the history not
   updating, for example after the bid side empties. We did not see such an event live (E), so the
   mechanism is unconfirmed. A YES + NO < 0.99 point cannot be two simultaneous executable asks.

**What "price age" means here:** the time since the API's (YES, NO) pair last changed. A long age
can also be a quiet but real resting quote. YES + NO < 0.99 is the stronger sign that a price
cannot be filled.

## 2. Results

Per entry: price age, YES + NO of the row used, win/loss and P&L. Full lists are in
`entries_<id>.csv`; all numbers are in `summary.json` (`audit.py`). "Fresh" means age ≤ 5 min and,
when PM US is a fill leg, YES + NO ≥ 0.99. The 60 s column applies the same rule with age ≤ 60 s.
Every reproduced run matched the judged re-run's n_trades and ROI exactly.

| id | team | PM US use | n | judged ROI | stale > 5 min | median age (min) | YES+NO < 0.99 | ROI fresh ≤ 5 min (n) | ROI fresh ≤ 60 s (n) |
|---|---|---|---|---|---|---|---|---|---|
| `b01-omnific-cross-strike-gap` | omnific | fill (lock leg) | 41 | +15.8% | 2.4% | 1.0 | 63.4% | +18.4% (14) | +18.4% (14) |
| `b02-omnific-pmus-leads-kalshi` | omnific | signal | 39 | +92.6% | 0.0% | 0.0 | 0.0% (signal) | +92.6% (39) | +94.3% (38) |
| `b03-omnific-pmus-range-no` | omnific | fill | 58 | +29.0% | 0.0% | 0.0 | 0.0% | +29.0% (58) | +29.0% (58) |
| `b04-omnific-sports-game-lock` | omnific | fill (lock leg) | 48 | +10.9% | 6.2% | 0.0 | 0.0% | +11.2% (45) | +11.2% (45) |
| `b05-omnific-ud15m-cross` | omnific | fill (Kalshi signal) | 39 | +24.8% | 2.6% | 0.0 | 10.3% | +26.7% (34) | +27.9% (29) |
| `b06-omnific-macro-no-fade` | omnific | fill | 58 | +38.2% | 0.0% | 0.0 | 6.9% | +29.3% (54) | +29.3% (54) |
| `b07-omnific-bracket-synth-parity` | omnific | fill (Kalshi signal) | 1564 | +16.6% | 6.1% | 0.0 | 19.6% | +11.4% (1164) | +10.8% (1086) |
| `b08-omnific-gasday-cross` | omnific | fill (Kalshi signal) | 52 | +20.5% | 86.5% | 215.5 | 71.2% | +13.9% (6) | +14.8% (5) |

(Generated as `table.md` by `audit.py`.) Below-0.99 entries with one side ≤ 0.12, the crossed pattern
from item 1.5: b01 22/26, b05 4/4, b06 2/4, b07 233/306, b08 37/37.

Columns: *stale > 5 min* = share of entries with age > 300 s. *YES+NO < 0.99* = share of entries
whose PM US row sums below 0.99 (b02 uses it only as a signal, so the sum filter does not apply).
ROI = Σ P&L / Σ cost, unchanged from each `run.py`.

Per-submission notes (`summary.json`):

- **b01 cross-strike-gap** (lock: PM US leg + Kalshi leg). 26 of 41 entries (63.4%) price the PM US
  leg from a row with YES + NO < 0.99. Staleness is minor: median age 1 min, 1 entry over 5 min.
  Every lock pays $1, so 41W/0L says nothing about whether the PM US leg could be filled. 14 entries
  are fresh and plausible.
- **b02 pmus-leads-kalshi** (PM US is the signal; fills are on Kalshi). No signal is older than
  2 min, and the signal requires PM US YES + NO ≥ 1 by construction. Unaffected.
- **b03 pmus-range-no.** Entries fire only on stored change rows, so age is 0 by construction.
  On the traded NO side alone, 3.4% of entries are older than 5 min. No entry sums below 0.99.
  Unaffected.
- **b04 sports-game-lock.** 3 of 48 entries are stale (max 1,537 min). None sums below 0.99.
  ROI moves from +10.9% to +11.2%.
- **b05 ud15m-cross.** 1 entry is stale. 4 entries sum below 0.99 (0.78–0.88; YES ≤ 0.06 with
  NO bought at 0.76–0.85). 34 entries remain; ROI moves from +24.8% to +26.7%.
- **b06 macro-no-fade.** No entry is stale (they fire on change rows, but 17.2% have the NO side
  itself older than 5 min). 4 entries sum below 0.99 (0.40, 0.58, 0.87, 0.98); those 4 return
  +119% ROI. Without them: 54 entries, +29.3% (judged +38.2%).
- **b07 bracket-synth-parity.** 6.1% of entries are stale and 19.6% sum below 0.99. The 400
  dropped entries return +27.7%. The 1,164 fresh entries return +11.4% (judged +16.6%).
- **b08 gasday-cross.** 86.5% of entries are stale (median 215.5 min, max 566 min) and 71.2% sum
  below 0.99; the most common sum is 0.81 (0.01 + 0.80). These reproduce the engineer's figures
  (median sum 0.87, median age 215 min, max 566). Of 52 entries, 6 are fresh and plausible. They
  return +13.9%, all wins.

## 3. Conclusion (neutral)

- **Look executable on this test:** b02, b03, b04 and b05. Few or no entries are stale or summed
  below 0.99. Fresh-only ROI is within about 2 pp of the judged ROI, and n barely changes.
- **Partly a stale-price artifact:** b06 and b07. Removing the non-executable entries lowers ROI by
  9 and 5 pp. The removed entries earn far more than the rest. The remaining entries are still
  positive and numerous.
- **Mostly a stale or crossed-price artifact:** b01 and b08. Most entries price the PM US leg from a
  crossed display (b01 63%, b08 71%). In b08 most prices are also hours old; b01's are not. b01's 14 surviving locks still
  earn +18.4%. b08 keeps 6 trades, too few to show an edge either way. b08's 52W/0L record comes
  mainly from buying at about 0.80 on crossed rows such as 0.01 / 0.80, at least 4¢ below the
  Kalshi-ladder bound by the strategy's own entry rule.
- **TEAM SWARM:** no submission uses PM US data, so this issue does not affect it.

## 4. Method notes and limitations

1. **Reproduction.** `audit.py` copies each snapshot to a temp directory and edits only the copy's
   `run.py`, so it saves the trades it already computes plus the PM US row it used. It runs the copy
   under `sandbox-exec` with the network denied, then checks n_trades and ROI against
   `judge/verdicts.jsonl`. All 8 matched exactly.
2. **b04 and b07 read the live, growing `pmus30/price_history.parquet`.** It is rebuilt every
   ~6,000 fetched markets. Re-run on today's file, b04 returns 92 trades and b07 returns 1,897. Both
   were reproduced exactly by restricting the copy to the slugs of an earlier build: b04 to the
   build after 36,046 fetched markets (the 18:43Z build; judged 18:59Z), b07 to the build after
   60,037 (20:59Z; judged 21:13Z). The build was found by searching back from today's file. For
   b04, the 48 reproduced trades also equal the frozen `trades.csv` (same slugs and P&L).
3. **Age.** Age = entry time minus the label of the last row whose (YES, NO) pair differed from the
   row before, measured on the submission's own clock. For the `side` age only the traded side
   counts. Where the last change is the market's first stored row, the true age is at least the
   reported value (left-censored; `n_left_censored`: b02 2, b03 1, b06 11, b07 20, b08 3).
4. **Clock convention.** b04, b07 and b08 match PM US label L to the Kalshi minute ending at L.
   Under the minute-open timestamp convention (item 1.2), each value was emitted up to 60 s later,
   so ages would be up to 60 s shorter. `fresh60_barclose_*` in `summary.json` gives the 60 s cut
   under that reading: b04 +11.2% (45), b07 +10.9% (1,119), b08 +14.8% (5). These are close to the
   table's 60 s column.
5. **Re-scoring only drops entries.** It does not re-run strategies with a freshness entry rule, so
   a market that lost a stale entry could have entered later on a fresh quote. That would be a rule
   change, so it was not done.
6. **Not assessed:** displayed size and depth (the history has none), and the Kalshi legs.

## Files

- `audit.py`: reproduction, per-entry measures, re-scoring → `entries_<id>.csv`, `summary.json`,
  `table.md` (`audit_run.log` = last run's stdout).
- `semantics.py`: semantics evidence → `semantics.json`. `--fetch` made the 5 public GETs once;
  responses are cached in `live_check/`.
- Run: `/Users/xero/CodeProjects/pumpfun-bonkfun-bot/.venv/bin/python audit.py` then `… semantics.py`.
