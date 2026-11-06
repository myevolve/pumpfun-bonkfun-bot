# Paper-Trading Lessons — living document

Source artifacts: `state/paper-trading/*.json`,
`.state/learning/lessons.sqlite3`, `learning-examples/token-lifecycles/README.md`.
The correction below supersedes the legacy journal-derived claims later in this
document. Those passages are retained as research history, not promotion evidence.

## Superseding audit: measurement integrity

The old paper journal cannot establish either profitability or the absence of an
edge. Both entry `TradeFlowHub.latest_price` and exit `_paper_fill_outcome` divided
**real** quote inventory by real token inventory. Pump's marginal price uses
**virtual** reserves. These are different quantities, not interchangeable price
feeds. Multiplying their ratio change by a hardcoded 0.01 SOL also omitted fees,
price impact, executable size and fills. A large return alone does not prove
migration; the earlier profit-threshold quarantine was not a valid classifier.

At a read-only audit snapshot the journal contained 43,231 lessons: 1,374 gate
passes, 274 `horizon_60s`, 142 `horizon_300s`, and **no `horizon_900s`** rows.
The corrected report excluded 1,330 legacy paper rows and found **zero eligible
live outcomes**. Counts describe that snapshot, not a frozen or completed cohort.
Historical raw entry/exit reserves were not retained, so do not reconstruct prices
or reverse the earlier quarantine by guessing.

The replacement is a diagnostic, not a new trading strategy:

- Every accepted dry-run gate plans 60/300/900-second marks in `paper_marks`,
  keyed by the exact parent lesson ID, retaining pre-decision features and scores.
  Its baseline is the accepted SOL `TradeEvent.price`; exits use the existing
  production curve decoder's `price_per_token`. Raw exit reserves and completion
  state are retained. No inferred or cross-mint entry baseline.
- Missing entry state, non-SOL quotes, completed curves, read errors, cancellation
  and observations over five seconds late are censored, never zero-profit fills.
  The read is bounded by that observation window with no sampler-level retry.
  An abrupt process death leaves unresolved pending rows; pending does not prove
  a worker is alive. RPC context slots are not exposed by this read API, so these
  marks do not attest bank freshness or execution.
- Normal dry-run session expiry stops accepting entries and drains the existing
  finite mark tasks before closing resources. This fixes the 600-second session
  cancelling the 900-second arm. External termination still censors or leaves
  pending work; no session, spend, fee or retry configuration was raised.
- CLI and dashboard use one read-only report. All planned, marked, censored and
  pending denominators remain visible. Horizon means use the **same completed
  three-arm entry IDs**, not independently successful samples or mint-only joins.
  Complete-case selection can still bias results; no optimal horizon is asserted.
- Raw Jev quality is a rubric score, not `P(win)`. The clipped-quality Brier
  calculation and automatic predictive verdicts were removed. No normalization,
  ordinal association or text-classification benchmark supplies trading calibration.
- The old threshold miner was removed: its 126 one-variable threshold/direction
  candidates were not “all hypotheses”; no training-positive candidate meant no
  held-out candidate test. Its labels used the invalid proxy above.
- Event authenticity is enforced before decoding: a matching discriminator alone
  is not provenance. `utils.program_logs.attribute_program_logs` validates the
  complete runtime invocation stack, binds each payload to its emitting program,
  and invalidates failed CPI descendants. A caught CPI failure preserves unrelated
  successful siblings; a top-level failure invalidates the entire transaction.
  Trade, migration, creation and lifecycle recording now share this boundary.
  Missing, mismatched or truncated frames cannot supply event-derived gate/exit
  state or a trusted creation fast-path baseline. The Geyser listener rejects
  failed transaction metadata before creation parsing or trade fan-out, even when
  individual invocation logs say `success`. Provider transaction success checks
  remain required at every ingress; processed logs still do not establish finality.
- Live trade flow normalizes current `quote_amount`, `virtual_quote_reserves`
  and `real_quote_reserves` into its SOL-denominated internal fields only after
  verifying native SOL or WSOL identity. Non-SOL events are rejected even if
  their legacy `sol_*` fields are positive. Canonical zero amounts and real
  reserves stay zero; non-positive virtual reserves remain invalid.
  A modern quote field or v2 trade instruction name requires the complete quote
  suffix; missing quantities cannot fall back to stale legacy values. Older
  SOL-only events retain their legacy interpretation. This fixes gate liquidity,
  flow-exit quantities and paper entry prices at their shared decoder, without
  extra RPC or changing strategy settings. Retained captures and stored marks
  are not recomputed.
- Repeated transaction delivery cannot inflate flow while its signature remains
  in the bounded cache. The hub retains the last 4,096 distinct signatures with
  relevant decoded trades or migrations across listener reconnects; standalone
  Geyser streams retain the same bound for one stream invocation. Every event
  in the first admitted batch remains eligible for delivery, including identical payloads at
  different log positions. Replays do not refresh retention or repeat migration
  callbacks. Missing signatures are rejected; wholly invalid batches do not
  reserve a signature. Overflow does not trigger replay retries.
  An offline reproduction previously counted one 3-SOL sell twice and fired a
  false net-outflow exit; the repeated notification now delivers no second trade.
  First admission wins: this is not fork reconciliation or durable exactly-once
  delivery. Eviction and process/stream restarts can admit an old signature again.
- Local trade-queue overflow permanently invalidates only that subscription.
  The buffered trades plus the overflowing trade are counted as discarded;
  all blocked readers wake with `TradeFlowLossError(reason="overflow")`, and the hub removes
  the queue from fan-out. Healthy subscribers keep receiving events.
  Entry gates reject known loss as `trade_stream_overflow`, including no-wait
  gates. Held positions stop consuming that flow and use existing price polling;
  a cached signal cannot latch or price an exit after its queue loses history.
  Already latched exits retain their retry cap, and pending transactions still
  reconcile. Cycle discovery rejects affected pending candidates immediately,
  before its consumer task untracks the mint. No automatic resubscription or
  reset is added.
  Offline reproduction previously accepted a gate after losing a creator sell
  and fired a net-outflow exit after losing a buy; both now stop at the loss
  boundary.
  Geyser EOF, transport failure, failed handshakes and cancellation invalidate
  every trade subscription on that stream before channel cleanup or backoff.
  Subscriptions requested during the interruption fail too. A fresh subscription
  acknowledgement permits new queues; old queues stay failed, and the bounded
  signature cache survives reconnects. Gates report `trade_stream_interrupted`;
  discovery discards affected candidates and held positions use existing polling.
  Standalone Geyser exit streams clear pending signals before asynchronous
  cleanup, without clearing already-latched exits.
  Offline reproduction previously accepted a buffered trade after EOF and left
  a standalone exit signal available during and after cleanup; neither survives
  the interruption now. Reconnect, exit, fee and request limits are unchanged.
  These checks detect explicit local loss and stream termination, not silent
  upstream omissions, rejected individual frames, fork changes, or finality.
- Backwards trade slots invalidate the affected subscription with
  `TradeFlowLossError(reason="out_of_order")`. The checkpoint survives queue
  draining; discarded buffered events cannot still trigger a gate or exit.
  Other mints remain independent, known signature replays are ignored before
  ordering checks, and distinct same-slot trades remain eligible.
  Entry gates reject pre-creation trades as `trade_before_creation` and reject
  backwards inputs as `trade_stream_out_of_order`, without replacing the last
  trusted event or counting another buyer. Direct cycle-discovery ingestion
  also quarantines the mint and its pending candidates before consumer cleanup.
  Standalone Geyser exits clear pending signals on regression before channel
  cleanup; already-latched exits retain their existing behavior.
  Configured replay marks non-monotonic trade-slot tapes `unpriced` with
  `trade_slot_order_ambiguous`, even when receive times increase.
  Offline reproduction previously accepted a negative-slot-wait gate, accepted
  another gate on a backwards slot, fired a trailing stop on older reserves,
  and emitted an older discovery candidate. All four now reject that state.
  This checks relative ordering within a subscription, not absolute freshness
  of its first event, intra-slot transaction order, forks, or finality. No
  timing, amount, fee, retry, request, provider, or commitment limits changed;
  no live capture or funded run was performed.

Offline reproductions previously delivered a foreign-program TradeEvent, a
rolled-back trade, an unclosed invocation, and a metadata-failed transaction to
subscribed queues. The corrected paths deliver none, while regression coverage
retains successful sibling trades and valid creation/migration events. No extra
RPC is added.
The lifecycle replay source closure now includes `src/utils/program_logs.py`;
old captures and source locks remain unchanged and require their original sealed
code, not re-labelling under the new decoder.

Offline reproduction now checks unchanged virtual price gives **0%** return,
same-mint entries cannot cross-link, all three horizons complete, and six different
censored cohorts stay in the denominator. Historical values are excluded, not
deleted. Commands:

```bash
uv run --offline --no-sync learning-examples/verify_paper_horizons.py
uv run --offline --no-sync pytest -q tests/test_trade_flow.py
uv run --offline --no-sync python -m learning.report --limit 0
```

For economic hypotheses, reuse `evaluate_online_paper.py` / `run_paper_trader.py`
and `verify_online_paper.py`: they already model integer curve quotes, fees,
price impact, bounded cash and an explicit no-trade choice. Their costs/fills
remain assumptions, not receipts. Keep immutable source locks and entry-time
chronological train/test splits. New acquisition remains provider-only on the
approved London runner, with existing venue, freshness and request boundaries;
the historical runner's public-provider default is not the current authorization.
No new funded run, service access or capture was launched for this audit.

The unresolved research question is whether a predeclared policy beats no trade
after costs on fresh held-out data. The legacy journal cannot answer it.

## Operator workflow and customer-value hypothesis

The Learning tab and `learning.report` expose the same evidence: planned and
complete paired entry counts, distinct paired mints, overlapping pending/censored
entry counts, per-horizon missing reasons with next checks, and within-entry
300s/900s return differences relative to 60s. Repeated mints are not independent
trials. Improvements/worsenings are descriptive counts, not strategy recommendations.

Live trade outcomes are marked at write time (`outcome_source='live_close'`) and
only marked rows are eligible; an outcome that is merely "not obviously paper"
no longer passes a name filter, and pre-marker rows are counted as
`excluded_unclassified` instead of being silently folded in. Each outcome keeps
its quote asset: the amount is stored in the position's quote units with its
mint, and the SOL column is filled only for SOL closes, so the reported
averages are SOL-denominated and a USDC close can never be read as SOL. The
amount is swap spread, not fee-inclusive net profit. Report schema version is 2;
a journal created before the marker reports that it needs one bot start to
migrate rather than failing to load.

An outcome is attached to the exact lesson row that opened the position (the
position journals that row's id, so it survives a restart); only a position
with no recorded id — a legacy journal, or one rebuilt by the recovery path —
falls back to the newest open lesson for that mint. The emergency and
pending-sell recovery closes now link outcomes too, so a close is no longer
missing from the cohort simply because it happened on those paths.

Download the JSON report from the Learning tab, or save a new snapshot from the CLI:

```bash
uv run --offline --no-sync python -m learning.report --limit 0 --output evidence.json
```

CLI export refuses to overwrite an existing file and returns nonzero for a missing
journal or failed export. Export includes a report schema version, generation time,
lesson watermark, coverage and exclusions. It is a summary of a consistent database
read, **not** a frozen dataset, replay package, proof of market freshness, execution
authorization or certification of profit. No wallet, signer or network is needed.

The narrow customer-value hypothesis is helping Solana strategy researchers and
bot operators avoid false-positive results and diagnose missing evidence without
hand-written SQL. Across the existing workflow:

| Area | Existing capability / current boundary |
|---|---|
| Discovery | Geyser events; current marks do not attest RPC context-slot freshness. |
| Pricing | Canonical virtual-reserve marks; separate fee/impact paper engine for economics. |
| Evaluation | Exact paired cohorts and failure-inclusive counts; no automatic promotion. |
| Execution | Explicit authorization, expected wallet, durable state and bounded budgets remain intact. |
| Operations | Shared CLI/UI evidence, missing-cause diagnosis and portable report snapshots. |

Defensibility, if earned, comes from a permissioned, point-in-time corpus that keeps
failed opportunities, reproducible cost/latency assumptions, and customers' use of
that evidence in their review workflow. An AI score, another dashboard, public chain
data alone, or more unvalidated indicators is not a demonstrated moat.

Demand is **unvalidated**. Before building billing, multi-tenant hosting or promising
alpha, ask prospective operators to bring a recent misleading result: measure the
time and RPC spend required to diagnose it using this workflow, and seek an explicit
paid-pilot commitment. No customer interviews, revenue or willingness-to-pay claim
is implied by the implementation.

### Evidence-quality improvements and next opportunities

The bounded operational audit retained 7,746 queued-token log records,
6,776 stale-token skips and 971 gate skips between 09:39:58 and 16:42:26 UTC
on October 31. These are log-event counts, not matched unique-discovery
denominators. The configured age threshold was 0.001 seconds; no threshold,
concurrency or gate was changed. Four trade-history overflows discarded
2,052 events. A separately reproduced queue-full subscription leak was fixed;
no queue-full warning was observed in that window, so it is not established
as the cause of those overflows.

New paper `observation_skip` rows retain queue-full/stale events separately
from gate outcomes and preserve queue ages. A failed exclusion write stops
the operation rather than reporting complete evidence. Existing historical
captures are not rewritten. Four October 6 unpriced 60-second observations
remain unresolved; absence of an outcome is not an invented loss or a live task.

| Priority | Next move | Decision value / required proof | Placement |
|---|---|---|---|
| 1 | Run a paid strategy-audit pilot using an operator's own misleading result | Measure reproducible corrections, decision changed, review time and provider requests; require an explicit paid commitment, not positive feedback alone. | Before billing/hosting expansion |
| 2 | Use the frozen planned-entry corpus and matching source for reproducible audits | Implemented freeze/analyze workflow preserves missing observations and repeated mints. It remains retrospective and excludes explicitly listed pre-plan history; it does not establish trading edge. | Before another entry-rule claim |
| 3 | Reconcile creator-wide transfers and actual collection receipts | Separate balance changes, deposits and claims; pool attribution and actual capital recovery remain unproved. New acquisition requires its own approved bounds. | Before any creator-pilot economics gate |
| 4 | Schedule a controlled collector/logging maintenance window | Bounded future console capture and UTC/progress fixes are verified offline. Existing stdout is preserved; activation/archive still needs a quiet maintenance point. | Before unattended/customer deployment |

Customer case-review worksheet: original claim; frozen evidence paths/hashes;
missingness and assumptions; reproducible correction; review time/provider
requests; operator decision changed; explicit paid-pilot response; retention
consent. Public data, a score or a dashboard alone is not defensibility.

### Reproducible acceptance evidence and keeper repairs

The first real frozen package is
`.state/research/accept-clues-20261101T043757Z.{cohort.json,report.json,source.py}`.
It retains **122 planned entries / 120 distinct mints**, rather than selecting
only the 100 fully priced entries. TRAIN has 24 fixed entry IDs and the
historical holdout has 98. The 2,151 accepted rows without paper plans remain
explicitly outside this study; this is not complete market-discovery coverage.

| Horizon | Observed | Terminal censored | Pending |
|---|---:|---:|---:|
| 60 seconds | 106 | 12 | 4 |
| 300 seconds | 103 | 19 | 0 |
| 900 seconds | 102 | 20 | 0 |

The saved matching source reproduced the saved report from the immutable
artifact. Nine comparisons expose available pair counts and null insufficient
statistics. Invalid completion/timing metadata and oversized imports are
rejected. Neither fixed historical membership nor a checksum establishes
preregistration, source authenticity, native execution or a profitable strategy.

The future keeper console sink now has bounded files/records, safe whole-stream
continuation censoring, private no-follow output files and retryable cleanup.
Review caught and repaired multiline disclosure, overlapping regex CPU, symlink
following and a slow-writer cleanup race before integration. All security
diagnostics used synthetic data; this is not a claim of production compromise.

Two additional shadow health regressions failed before and passed after the
fix: ISO UTC parsing had shifted epochs in non-UTC timezones, and a short quiet
poll after long uptime could trigger the 900-second frozen-slot rule. Progress
now resets on actual slot change and restart; the configured threshold is
unchanged. No running collector or historical log was altered.

#### Physical lines are not credential boundaries

The first bounded-console implementation masked a synthetic credential label
but retained its newline-separated value. The root cause was treating a
physical line boundary as proof that a sensitive value had ended.
Next time I will keep bounded per-stream privacy state across chunks, newlines
and blank lines, and test the actual stream consumer rather than a regex alone.
Signal to recognize: a masked assignment followed by retained unlabeled
continuation text. The conservative choice here censors that stream through
EOF; useful diagnostics still exist in the independent stream and structured
logs, but no exhaustive secret-detection claim follows.
See the [keeper console guide](../learning-examples/token-lifecycles/README.md#keeper-console-safety-for-future-runs).

#### Intra-slot bundle lookahead and pre-quote paper marks

Joining the 43 post-Oct-14 band-gated accepts in `lessons.sqlite3` 1-to-1 against
`keeper.stdout.log` showed that 35 of 43 (`81.4%`, every entry with
`grad_elapsed_s < 4.0s`) failed `PlatformAwareBuyer.execute()` 1 ms later with
`virtual_token_reserves must be a positive raw u64 integer`. Creators swept their
curves in 4 same-slot tranches (`~21 -> 42 -> 63 -> 85 SOL`); `EntryGate.observe()`
returned `accept=True` on Tranche 3 while Tranche 4 (`85 SOL`, curve complete in
slot 0) remained in `TradeQueue`, and `UniversalTrader._handle_token()` scheduled
`paper_marks` before `buyer.execute()`. The 8 genuinely executable entries had
3 graduations and averaged `-17.40%` at 60s (`-8.86%` composite policy).

Next time an event-driven gate evaluates multi-event transactions or slots, drain
all queued events from the triggering slot before finalizing an accept, require
`min_wait_slots >= 1` when excluding creation-slot bundles, and schedule paper
marks only after the buyer's curve refresh confirms an executable quote (censoring
already-completed curves as `unexecutable_completed_curve`). Signal to recognize:
`slots_waited == 0` accepts that "graduate" in `~2s` (one RPC settle sleep) while
`buyer.execute()` logs zeroed reserves. Verified offline by
`learning-examples/token-lifecycles/verify_executable_crossing_gate.py`.

#### Sequential gate head-of-line blocking and concurrent gate evaluation

Running `UniversalTrader._process_token_queue()` as a single sequential loop
with `max_wait_ms = 30000` throttled throughput from `16.68 tokens/min` (at
`max_wait_ms = 3000`) to `2.36 tokens/min`: each slow non-Mayhem coin blocked
up to 30s at the head of `token_queue`, causing subsequent coins to sit in
`TradeQueue` for 30–240s and fail immediately on their first observed trade
with `window_expired` at `slots_waited = 76..600` (`68.4%` of skips).
`_process_token_queue()` now dispatches up to `_MAX_CONCURRENT_GATES = 64`
concurrent token tasks, anchors `_await_entry_gate()` deadlines to the discovery
timestamp (`token_timestamps[mint] + max_wait_ms / 1000`), and skips
`TradeFlowHub` subscription in `_queue_token()` when static gate rules
(`mayhem_only` / `exclude_mayhem`) already exclude the token.

#### Exact CreateEvent log pre-filtering and quote-mint correlation

Checking `any("Instruction: Create" in log for log in logs)` matched
substring-collide instructions (`Instruction: CreateIdempotent`,
`Instruction: CreateTokenAccount`, `Instruction: CreatePool`), triggering full
invocation-stack attribution and Borsh decode attempts on ordinary trades.
`PumpFunEventParser` now matches exact `_CREATE_INSTRUCTION_LOGS`
(`"Program log: Instruction: Create"`, `"Program log: Instruction: CreateV2"`)
and correlates `CreateEvent` to the matching `create`/`create_v2` instruction by
mint rather than assuming `parsed_Creates[0]`, preserving exotic `quote_mint`
accounts in multi-create transactions.

#### Creator-vault withdrawal reconciliation

Endpoint balance diffs `(last - first)` on `creator_vault` undercount accrued
creator fees whenever a creator withdraws mid-window via
`collect_coin_creator_fee`. Summing positive step increases (`max(0, b[t] - b[t-1])`)
separately from negative step decreases across the 100 tracked graduated pools
reconciles **`152.95 WSOL` accrued (`50,983 SOL` implied PumpSwap volume at 30 bps)**
and **`113.07 WSOL` withdrawn across `27/100` claiming creators**.

#### 24,015-coin OOS microstructure and post-graduation PumpSwap reclaim

Out-of-sample evaluation on the 24,015-coin exact-CPMM tape coupled with the
100-pool PumpSwap post-graduation tracker shows that single-wallet sweep
concentration (`max_buyer_share > 0.75` or `max_single_buy_sol > 10.0`) drives
manufactured completions, while top-decile fee-generating organic graduations
(`grad_elapsed_s = 18.26s` median) dip immediately after migration (`p5/open = 0.917x`)
before reclaiming `open_price` (`+5%` reclaim confirmation) and rallying to
`p120/open = 1.155x`.





## What the market taught us (negative results, held-out)

1. **The tested post-creation signals lost held-out in the recorded study.**
   31,104 policies tested (gates: mayhem/min-buyers/liquidity/creator-holding;
   exits: trail/stop/TP/hold) across slots 1-10 — best in-sample policies
   lose -0.5% to -3.3% per trade out-of-sample. Entering at curve milestone
   X SOL loses -10% to -13% at every X. (token-lifecycles README, Sep 2026)

2. **Post-graduation dumps are manufactured.** Curve bought out in <=10
   trades, 50-100 SOL sniped at pool creation, pool drained within 25 min
   (Sep 2026 live tape). The later Jev paper-journal losses are invalid price
   proxies and cannot independently corroborate that on-chain observation.

3. **Follower/copier models lose.** A locked cohort of 17 wallets lost
   -3.12% per follower trade at +2 slots held-out. Copying actor receipts
   does not reproduce their execution. (tx 2CQgjcdN audit: 261 failures
   among 403 txs, 1.50 SOL fees including 0.79 on failures.)

4. **Mayhem coin flow is time-clustered.** Gate accepts: 23 in a 10-min
   window (15:40 UTC Sep 29), then 0 accepts for 2+ hour stretches. Any
   sampling or reporting that assumes steady-state will mislead. A 600-second
   discovery budget is not sufficient for completing a 900-second observation;
   drain existing marks rather than silently dropping that arm.

## What the engineering taught us (every one was a live failure first)

1. **One-shot exits hide behind two different doors.** The token-wait loop
   has two post-handling exits: the paper-fill `continue` (fill branch) and
   `if not skipped: break` (skip path). BOTH compared against baselines:
   the fill branch needed a per-token `attempts_before_token`, and the skip
   path still held the hoisted `attempts_before` — so the run died 2 seconds
   after the first fill (MSHR, 2026-09-30 08:09). Red-green proven; the
   strengthened test fails with 2 of 3 tokens handled on old code.

2. **Log windows without noise filtering show you the past 40 seconds.**
   event_parser emits ~10 lines per token, httpx one per RPC call. Filter
   by logger BEFORE truncating (dashboard fix eb0dd77).

3. **In-sidebar `st.rerun()` aborts the script before the tabs render.**
   The auto-refresh sleep+rerun must be the last statement of the file.

4. **Streamlit `cache_data` ttl is not real-time.** Loaders fed by changing
   logs need ttl <= 5s and a refresh loop; 30s made everything look frozen.

5. **PID files without identity verification can kill unrelated processes.**
   `os.kill(pid, 0)` proves existence, not identity. Verify argv via `ps`
   before signaling (dashboard hardening 975950c).

6. **pending_token_count is a live-trading gate, not bookkeeping.** A
   journal with pending tokens passes every other "clean status" check,
   and startup queues and BUYS them (bot_runner preflight hard-codes
   durable_state ok). The dashboard gate now includes it.

7. **A quality rubric is not a win probability.** Keep the raw returned score;
   do not clip it into a probability or assert calibration from its numeric range.
   Earlier 0–1 normalization claims did not establish a valid `P(win)` mapping.

8. **Jev latency is ~0.9-6s per call.** Fine for per-decision annotation;
   disqualifying for a <1.5s snipe gate. Never put it in the hot path.

9. **Journaling is necessary but not sufficient evidence discipline.** Durable
   rows can preserve incorrect price models. Retain quote provenance and planned
   denominators, and reproduce the arithmetic before interpreting outcomes.

## Jev-vs-gate correlation (2026-09-30, n=5,864 scored lessons)

Accepted coins (buyers_present) average jev_quality **0.897**; skipped
cohorts average 0.33-0.48. This correlation is **expected and partially
circular**: the scorer's prompt includes the gate state (buyers, real_sol)
that also drives the accept decision. It is NOT independent evidence of
predictive power. The only admissible test remains jev_quality vs realized
outcome PnL on resolved fills.

## How the ecosystem uses Jev in trading (survey 2026-10-01)

Sources: drillan's finance-project survey (updated 9/30), the reference
jev-trader (jarrodwatts, 2.7k stars), buberlo/jev-trader (the rigorous
Python redesign), jev-harness (confidence gates + shadow mode), and the
independent jevbench calibration study.

1. **"Jev judges, code executes" is universal.** Every serious project
   keeps thresholds, sizing, risk vetoes and order placement in
   deterministic code; Jev supplies only typed judgments over a compact
   (<400-token) state snapshot. buberlo: "Jev should not own the trading
   system. It should own selected judgments inside it."
2. **The battery pattern.** One call, many ATOMIC questions (buberlo uses
   six: regime, direction, toxic_flow, liquidity_stressed,
   quote_environment, inventory_pressure), composed in a policy engine.
   Our scorer now asks FIVE (quality, copycat, early_dump_risk,
   momentum_organic, liquidity_trap) in one call - the battery pattern
   applied (0bbe8b9).
   Small-sample caution PROVEN: the first battery correlations that looked
   directionally-correct at n=25 (dump_risk -0.188, organic +0.133) flipped
   to noise by n=41 (dump_risk -0.054, organic -0.063). Correlations under
   ~±0.2 at n<100 are indistinguishable from zero - never read a trend into
   them. The report's correlation table auto-updates; trust only stable
   signs at n>=100.
   n=154 battery-resolved reading (2026-10-01): all signals ~zero EXCEPT
   copycat +0.178 (borderline, CI +-0.16). Outlier-checked: one +0.33 SOL
   fill in the mid cohort inflates it; the all-scored population shows
   only +0.068. Watch, don't act.
   n=499 scored-resolved / n=178 battery (2026-10-01): Pearson +0.045 but
   Spearman -0.029 - the "correlation" is one +0.33 SOL outlier in the
   mid band, not a monotone effect. Stable structure: mid copycat
   (0.5-0.8) is the LEAST-BAD cohort (23% win, avg -0.0009, near
   breakeven) vs extremes (17% win, avg -0.0035). No tradable edge; the
   mid band merely avoids the worst dumps.
   Exploratory substrate mining (2026-10-01, n=594 fills): entry-liquidity
   quartiles - Q3 (0.30-0.43 SOL real) is the only quartile positive in
   BOTH time halves (early +0.0065 n=36, late +0.0011 n=112). Q1/Q4 flip
   sign across halves. Hypothesis for forward testing: entry liquidity
   band as a gate amendment (currently min_real_sol=0.1, max=0.5 - a
   0.30-0.43 band would be narrower and needs its own held-out window).
   Not promoted - exploratory only, one split, no held-out confirmation.
   SYSTEMATIC MINER (2026-10-01, n=826 fills, 70/30 split): grid-searched
   ALL features (real_sol, buyers, mayhem, all 5 Jev signals, hour) at
   7 quantile thresholds x 2 directions. Result: **ZERO threshold rules
   survive held-out**. The Q3 band was one instance of a universal
   failure mode: any positive-on-train subset dies out-of-sample. The
   mayhem-snipe substrate is exhausted at every measurable cut. Q3 new avg
   +0.0021 (27% win) vs non-Q3 new avg +0.0019 (21% win) - the band edge
   over non-Q3 collapsed to +0.0002/fill (noise). The new non-Q3 cohort
   itself turned positive (+0.0019 avg) - that positivity was mostly the
   same migration artifacts. Clean-book recheck: Q3 n=186, 26% win,
   avg -0.0010 vs non-Q3 18% win, -0.0037. The RELATIVE band edge
   survives (+0.0026/fill) but both cohorts are ABSOLUTELY negative - a
   relative edge over a losing baseline is not a strategy. Same trap as the Jev
   signals: exploratory structure -> regime shift -> nothing.
   **AND the regime shift was partly an artifact: 12 outcome rows (+2.47
   SOL) were migration corruption** - the +60s sampler read a MIGRATED
   pumpswap pool (SOL-sized vault reserves) and compared its price to the
   curve entry, producing fake +1000..5700% "wins" (ELON +0.57 SOL on a
   0.01 entry). Fixed with a complete-flag migration guard in the
   sampler; the 12 rows are quarantined (outcome_reason=
   'invalid_migration_artifact'), outcomes left open. The honest book:
   **-2.1696 SOL over 770 fills (-0.0028/fill, -28%)** after quarantining
   all 15 artifacts (3 more appeared from the pre-guard window before the
   restart picked the guard up; zero since - guard verified live). Every "positive
   window" earlier today was corruption. The substrate is far worse
   than the corrupted view suggested.
3. **Calibration is the product.** buberlo logs (state, decision,
   outcome) triples and computes Brier/ECE/reliability, then Platt-scales
   thresholds to YOUR venue. Our journal already logs the triples; what's
   missing is Brier/ECE reporting and per-bucket reliability curves.
   **Superseded (see the audit above):** that gap was closed by *removing*
   calibration — raw Jev quality is a rubric score, not P(win), so Brier/ECE
   against it is meaningless. Do not re-add it while Jev stays an observer.
   jevbench: Jev's ECE is 0.10-0.13 on public datasets - good but not
   perfect; treat probabilities as features, verify on your own tape.
4. **Independent benchmark (jevbench, n=500):** Jev accuracy 76-95%
   across text-classification datasets, latency p50 ~380 ms (OpenRouter
   hop), $0.02-0.08/1k. Beats gpt-5-mini on accuracy at 1/5 the cost;
   loses to fine-tuned DistilBERT (as any zero-shot does). On
   text-classification jobs Jev is a strong zero-shot; nothing in the
   published benchmarks measures TRADING outcome prediction.
5. **Shadow mode is standard.** jev-harness: log what you WOULD do before
   changing live behavior. Our dry-run journal IS shadow mode; keep it.
6. **Nobody publishes a profitable Jev trading result.** The survey's
   pattern list ends with "no prediction-market, arbitrage or DeFi
   projects" and every live desk is days old. Our n=290 zero-correlation
   finding is consistent with the field's public state.

## Historical Jev promotion analysis (withdrawn as evidence)

The following figures used invalid paper labels and, for Brier, an invalid
probability interpretation. They are retained to explain earlier decisions, not
as findings about predictive power. Jev remains outside the trading decision.

Jev enters the live entry gate only when `pnl_by_quality` shows high
scores (>=0.6) systematically outperforming low scores (<=0.4) on a
sample large enough to trust (target: >=50 resolved outcomes per bucket,
stable across separate windows). Current sample: n=39 resolved across five
windows (2026-09-30). Variance is the story: the 08h and 20-22h UTC
windows dumped (avg -0.0045 to -0.0077, curve-to-zero exits); the 21h
window ran +0.0057 avg across 13 fills, including e/acc at +913% in 60s
(entry 1.15e-9 -> 1.17e-8, real curve reads). Jev quality did NOT separate
winners from dumpers (e/acc 0.91, Loop 0.91 - one +913%, one -100%).
n=63 resolved (7 windows): 23% winners, total
-0.222 SOL. Every high bucket negative (0.8: -0.0062, 0.9: -0.0007,
1.0: -0.0065, 1.1: **-0.0079 — the highest-rated coins did WORST**); only
the 0.6 bucket (n=3) is positive. Approaching measurably anti-predictive.
Win rate by bucket: 0.6: 33%, 0.7: 50%, 0.8: **12%**, 0.9: 30%,
1.0: 20%, 1.1: **14%**. Mechanism hypothesis: coins rated 0.8+ are the
ones that look impressive (credible names, visible momentum) - exactly
the bait a manufactured dump optimizes for. **n=290 review (2026-10-01):** Pearson
r(quality, pnl) = **+0.014** - ZERO correlation. The earlier -0.119
inversion washed out with more data: the honest verdict is "uninformative"
on this substrate. Avg pnl/fill: -0.0018 SOL (-18% on 0.01 entries).
Report verdict logic hardened: it now refuses to compare cohorts until
each side has >= 10 outcomes (the 288-vs-1 split made a "predictive"
verdict off one lo outcome). Windows: 3 positive of 17, all small-n.
**Brier(win) = 0.666 vs a 0.161 no-information base rate** - treating
quality as a win-probability is far worse than guessing the base rate.
Formally: Jev's quality score does not forecast wins on this substrate
(journal.py `_brier`, surfaced in learning.report).

**n=106 checkpoint review (2026-09-30):**
r(quality, pnl) = **-0.119** (weak inverse). Every bucket 0.8-1.1
negative; best buckets are the LOW ones (0.6: +0.0032). Only 2 of 12
windows positive (04h +0.001, 21h +0.006), both small-n. **Jev: NOT
promoted** - at best uninformative, at worst mildly anti-predictive.
Deeper verdict: the underlying strategy (mayhem snipe at creation,
+60s exit) loses -0.42% per fill on its own paper book (-0.449 SOL over
106 fills), consistent with the held-out research. No Jev configuration
rescues a negative-strategy substrate. Jev stays an observer.

**Widened-gate replication (2026-10-01, mayhem_only=false):** with the
gate no longer restricted to mayhem coins, non-mayhem fills resolve at
~10x the prior rate (n=106 -> 122 in one window; ~18k lessons/day). The
inverse pattern REPLICATES in the mixed cohort: buckets 0.8-1.1 all
negative (17-23% win rates), 0.6-0.7 the least-bad. Jev quality is
anti-predictive across both coin classes - the "impressive launch" bait
hypothesis holds in non-mayhem coins too.

10. **A hung startup looks identical to a running bot from outside.**
    The 12:55 run churned httpx RPC calls for 7+ hours with zero trader
    output - never connected the geyser, never started the trader. pgrep
    (the keeper's check) sees a live process and does nothing. Fix: the
    keeper also checks log staleness - if the newest run log grew in the
    last 5 minutes but contains no non-httpx lines, kill and restart.
    (2026-09-30 12:54 incident, keeper.log)
    Note: the keeper itself is not checked into this repo — verify where it
    lives before relying on this mitigation after a restart.

## Operational status

Liveness and wallet balances are point-in-time observations, not durable research
claims. This audit did not read wallet secrets, restart a collector, or authorize
trading. Observe current process, ledger and wallet state under the repository's
safety rules before any separately authorized run.

## Latency: mobile host vs GCP (2026-10-01, sealed 4572e45f)

Same probes, three vantages (n=20-30 each): local mobile, Cloud Run
us-central1, Cloud Run us-east1 (Chainstack node is us-east, 64.31.55.5).

| Target | mobile p50/p95 | us-central1 | us-east1 |
|---|---|---|---|
| RPC getHealth | 188 / 978 ms | 96 / 169 | **68 / 133** |
| Geyser TCP | 54 / 324 ms | 32 / 37 | **21 / 168** |
| Jev API | 195 / 738 ms | 159 / 220 | 181 / 247 |

These are historical RPC health-request and TCP-connect timings, not measured
Geyser event arrival, slot-consistent bank readiness or transaction inclusion.
They do not establish an optimal execution region. The paper path is not
latency-insensitive: arrival, pre-entry scoring and delayed reads can change its
sample. Follow the current London-runner policy for any new latency acquisition.

## Historical horizon conclusion (withdrawn)

The earlier 123-pair comparison covered 60 and 300 seconds, not 900 seconds, and
both prices used the invalid real-inventory ratio. Its claims that losses were
structural and 60 seconds was optimal are unsupported. The new paired diagnostic
above answers coverage and gross price-mark questions only; a profitable exit
policy still requires executable, costed, fresh held-out evidence.

## Whale-ride graduation exit (2026-10-05, single tape, not promoted)

Offline re-examination of `lifecycles_24h.jsonl` (24,015 coins, Sep 4-5 2026)
found the first held-out-positive policy family in the record — and the reason
every earlier milestone backtest disagreed with it. The whale sweep that
completes a curve makes pump.fun seed the PumpSwap pool at ~79% of the curve's
final marginal price (`pool_quote ≈ 0.79 × raised SOL`,
`pool_base = raised / final_marginal_price`, verified on the tape: CAT crossed
X=20 at 1.25e-4, pool opened 3.26e-4 = 2.61×). A mid-curve entrant who holds
through graduation and sells into the pool captures the sweep; every
curve-only exit either dumps before it or cannot exit at all.

`learning-examples/token-lifecycles/simulate_graduation_exit.py` reproduces
it. On that tape, entry at the X-SOL crossing (+1-slot landing, skipped when
graduation beats the buy), exit at pool+2 slots:

| X SOL | hold | test mean | win | test p90 |
|---|---|---|---|---|
| 30 | 5 | +10.8% | 23% | +108% |
| 40 | 5 | +13.9% | 27% | +110% |
| 50 | 5 | +14.0% | 36% | +75% |
| 60 | 5 | +8.9% | 49% | +41% |

Train/test halves agree; 2,000-draw bootstrap CIs on the held-out half put the
mean strictly positive at X=20 (+8.8% [+4.5, +13.3]), X=40 (+13.9% [+8.0,
+19.6]) and X=50 (+14.0% [+8.8, +19.5]) — within-window sampling noise does
not explain the edge; the remaining caveat is that one tape is one draw from
the era. 17/22 hourly windows positive at X=40. The whale
beats a +1-slot buy in ~35-40% of crossings (free skip). Replaying the tape
through a 2s poller with +2s/+5s discovery lag (the shadow's real granularity)
costs only 0.5-3.3pp: test means stay positive at every X (X=40 +13.4%, X=20
+7.9%, X=50 +11.4%, both lag brackets). The shadow's cold-side (non-graduate)
rows are expected negative; the pooled mean flips on the graduation cohort.
The recorded corpus
era (Jun-Jul) had no such pattern — the June milestone result (-10-13%,
curve-only exits) and this result are both era statements, not contradictions.

Known biases, all optimistic: one 24-hour window; failed transactions are
excluded from tapes (our reverting buys/sells would add fees, not trades);
pool+2 assumes the exit wins the race against the first sniper wave. The
phantom that inflated the first pass — `sell_value` pricing a sale into a
completed curve's virtual remainder — is exactly what this policy avoids:
never price a post-buyout curve state as a curve sale. Confirmation needs a
fresh recording (London runner, provider-only projection; acquisition is a
user decision). The bot-side capture path exists: TradeEvent-gated crossing
entry with zero-RPC build + TPU-QUIC (1-2 slots), and #237's graduated-market
seller for the exit. Shadow-mode first, `enabled: false` until then.

`run_whaleride_shadow.py` collects the forward test: PumpPortal discovery plus
a paced public-RPC trace of every tracked curve, with pool and vault rows on
graduation (`--report` scores it with the same cost model). Traces land in
`.state/whaleride-shadow/shadow.jsonl`; the traced entry is the first observed
state at or after the crossing, so shadow results are conservative on entry
price. Zero credentials, zero signing; SIGINT stops it cleanly.

## Paper-session schema debt (2026-10-06, fixed before launch)

The whale-ride paper session's first accept crashed on `paper_marks`: the
running journal expects `scheduled_utc`, `exit_state`, and a nullable
`started_utc` — the DB predated all three. Two lessons: (1) schema drift
between code and a long-lived DB crashes at WRITE time, not load time —
the "one bot start to migrate" note in the report section did not cover
these columns; (2) migrate additive-first (ALTER ADD COLUMN), then rebuild
only when a NOT NULL must become nullable, with a backup before each step.
Backups: `lessons.sqlite3.bak-20261006` and `.bak2-20261006`.

## Cross-instrument overlap verified (2026-10-06 16:46Z)

The shadow and the paper arm are no longer independent narratives: JEANPHILF
is in BOTH — the paper arm's lesson 59944 (accepted 76.89 SOL) and the
shadow's pool row at 14:08:58Z. Two instruments, same coin, same graduation,
independently priced. Cohort velocity: pool rows 15 → 21 → 27 across ~90
minutes (fastest yet); completions 37; max curve in the paper window 53.34
SOL and climbing toward the 60 gate. The promotion case's cohort threshold
(~30 pool-priced outcomes) is roughly an hour away at this rate; the
duration gate (24-48h of sealed snapshots) remains the longest pole.

## Focused 6-hour rollover test (2026-10-14 05:06Z, closed)

Relaunched the paper collector (`keep_whaleride_paper.py`) at `2026-10-13 23:05Z`
after it had been down since Oct 11 (`74587` baseline, 42 prior rollovers) and
ran a bounded 6-hour live window (`23:06Z` → `05:06Z`, 736 decisions: 354
`window_expired`, 191 `creator_sold`, 187 `timeout`, **4 `buyers_present`**).
All 4 accepted coins were `buyers == 3`, non-mayhem, `real_sol 60.50..63.47`,
and all 4 graduated within `2.5..5.8s` and resolved across 60s/300s/900s:

| ID | UTC | Symbol | Curve SOL | Grad Open | Peak (G+s) | **Rollover (G+s)** | Fixed 60s | Fixed 300s | Fixed 900s | **Edge vs 60s** | **Edge vs 900s** |
|---:|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 74705 | `00:05Z` | `SEALYS` | 60.50 | −20.6% | −14.7% (37.4s) | **−43.7%** (57.8s) | −43.7% | −44.2% | −91.4% | **+0.0 pts** | **+47.7 pts** |
| 74727 | `00:17Z` | `CLAUSDIA` | 63.45 | +81.3% | +107.9% (10.3s) | **+83.7%** (19.4s) | −56.4% | −54.9% | −91.3% | **+140.1 pts** | **+175.0 pts** |
| 74974 | `02:21Z` | `VRAXTARD` | 63.47 | +112.6% | +112.6% (0.0s) | **+89.3%** (2.0s) | −59.8% | −56.8% | −91.3% | **+149.0 pts** | **+180.5 pts** |
| 75143 | `03:41Z` | `BUPE` | 60.94 | +62.2% | +71.8% (10.2s) | **+45.8%** (14.2s) | +77.6% | +119.0% | −91.7% | −31.9 pts | **+137.5 pts** |

- **6-hour window verdict (n=4)**:
  - **Rollover exit (5% drop, peak seeded with `open_price`)**: **3W / 1L**, **+175.1 aggregate points** (**+43.8% mean**, **+64.8% median**).
  - **Fixed 60s**: **1W / 3L**, **−82.2 points** (−20.6% mean) → rollover beats/ties **3/4** by **+257.3 points** (+64.3 pts/coin).
  - **Fixed 300s**: **1W / 3L**, **−37.0 points** (−9.2% mean) → rollover beats/ties **3/4** by **+212.0 points** (+53.0 pts/coin).
  - **Fixed 900s**: **0W / 4L**, **−365.7 points** (−91.4% mean) → rollover beats **4/4** by **+540.7 points** (+135.2 pts/coin).
- **20 live resolutions under the armed gate & peak-seed rollover (`74974..103430`)**: including `FLAMBO` (`91174`: **`+96.8%`** vs `−92.5%` at 900s, and **`24.73 SOL`** creator fees), `TRALALA` (`91328`: **`+114.5%`** vs `−92.5%` at 900s), `JPG` (`98015`: **`+41.6%`** vs `−51.4%` at 60s), `PUMPCITY` (`98933`: **`+53.6%`** vs `−45.5%` at 300s), and `FOMOCHI` (`103430`, `13:53Z`, `buyers=3`, `63.22 SOL`: opened at **`+108.8%`** all-time high, rolled over at `G+2.0s` for **`+87.2%` net** vs `+56.7%` at 60s and **`−49.3%` at 300s**, **`+136.5 pts` saved vs 300s**).
- **Cumulative post-fix 5% rollover cohort (`id >= 66872`, n=51 clean resolved, excluding `IOF` 73272 vault squeeze)**: **46W / 5L (90.2% win rate)**, **+3,141.9 aggregate points** (**+61.6% mean**, **+53.6% median**), beating 60s (+2,302.8 pts, 35W/16L) by **+839.1 points** and 300s (+2,718.7 pts, 23W/28L, −21.8% median) by **+423.2 points**.
because the operator's proc-service kills never touched the keeper's
lineage. SQLite's WAL serialized the writes (no corruption), but the
one-writer-by-convention invariant was violated and the position JSON
was exposed to lost updates. Resolution: single supervisor restored —
the keeper detected its child's death (process_dead, restarts=4,
quick_deaths=0), backed off 30s, respawned with the latest code
(04:19:30Z). Rule going forward: **the keeper owns the bot lifecycle**;
operator restarts must kill the keeper first, or never spawn a second
supervisor. The stale child also explains why some pre-dawn censoring
patterns looked inconsistent: two gates were observing the same sweep.

## The thresholded cohort: 7 records, the rule's verdict (2026-10-08 01:3xZ)

Three new rollover records under the 5% threshold — all real drops
(29.3%, 18.9%, 5.2%), zero noise fires. MIMI (62.17 SOL, grad 29.5s):
rollover +15.1% vs the 60s mark's −16.4% — the mark landed after the
crash, the rollover fired at it. PAHC (64.81 SOL, grad 7.0s): +41.9% vs
+14.1% (60s) and −90.8% (900s). CAPES (69.77 SOL, grad 15.6s): +27.2%
vs +26.9% (60s) and −93.0% (900s).

**Seven records: rollover mean +50.8%, 900s-timeline mean ≈ −89%. The
rollover rule beats the timeline 7/7 and never rides to −90%.** Against
the fixed 60s mark it wins when the peak lands late (MIMI, PAHC), ties
when the peak lands near 60s (CAPES), and loses when the 60s read
catches the peak exactly (ELONPHIL +212.5%). The fixed mark is a
coin-flip on peak timing; the rollover is bounded below by the crash
detection itself.

The condition-driven exit is no longer a hypothesis — it is the
measured, threshold-tuned, 7-for-7 rule. The rollover-threshold sweep
(2/5/10%) over the accumulated price paths is the remaining refinement.

## The four-record rollover cohort (2026-10-07 20:0xZ)

FOMOPUP (64087, accepted 78.56 SOL, graduated 3.4s): peak +14.6% at
G+~14s, rollover caught G+14.6s — exit captured +14.6%; the grace was
shorter than the detection latency. PEA (64220, accepted 60.40 SOL,
graduated 28.3s): pool NEVER rose (below open at G+5s), rollover at
G+6.1s captured −11.2%. The 900s timeline marks: −93.88% and
−70.99% — the dump is universal, only the grace period differs.

Cohort table (rollover exit vs best timeline mark):
ELONPHIL +87.9% (timeline 60s +212.5%), Sentients +180.4% (900s
−92.35%), FOMOPUP +14.6% (900s −93.88%), PEA −11.2% (900s
−70.99%). The rollover rule's floor holds (3 of 4 crush the timeline;
the fourth loses little); its ceiling is set by the grace-vs-latency
race, which is the same latency frontier — the faster the pool dies,
the less any follower exit captures.

The measurable refinement stands: a rollover-threshold sweep (exit on
>N% drop from peak, N in 2/5/10) over the recorded price paths. The
grace-period distribution is the strategy's real parameter, and the
G-clock is accumulating it.

## Peak-baseline bug found in the rollover detector (2026-10-08 18:5xZ)

The decay loop's peak started from its own first poll (~G+2s) — the
graduation-open price was never a peak candidate. For open-and-die
coins the recorded peak sat BELOW the open (MEMENCY: open 6.130e-7,
recorded peak 5.837e-7), so rollover thresholds were measured against a
phantom baseline. Fixed (0bb6510): the peak now seeds from the open
sample; a censored open keeps the first-poll behavior. Corrected
cohort read (14 records, capture vs curve entry, 900s fixed mark):

ELONPHIL +87.9% (900s pending), Sentients +180.3% vs -92.35%, FOMOPUP
+51.3% vs -93.88%, PEA +29.8% vs -70.99%, WIFAUTON +35.8% (pending),
MIMI +15.1% (pending), PAHC +41.9% vs -90.83%, CAPES +27.2% vs -93.02%,
MEMENCY -61.2% vs -94.36%, butter +76.4% vs -92.27%, solcat +108.0% vs
-92.57%, Mishu -98.4% vs -98.48%, CAPYWIFGUN +256.6% vs -92.79%,
TARDTANK -64.2% vs -93.39%.

Cohort mean: +41.3% rollover vs -90.9% fixed-900s (12 paired; the two
pendings excluded). The rollover rule never rode to -90%; the timeline
almost always did.

## The rollover cohort at 12: every archetype and both failure modes (2026-10-08 14:5xZ)

Twelve rollover-capable graduations since the continuous detector went live.
The detector polls the pool every 2s from graduation open; the exit fires on the
first poll below the running peak. Outcomes vs curve entry (all rolls captured
from the same G-clock samples):

| coin | grad delay | peak vs entry | roll exit | timeline exit (60/300/900) | archetype |
|------|-----------|---------------|-----------|---------------------------|-----------|
| FOMOPUP | 3.4s | +1.9% | +51.3% | — | small wave |
| PEA | 28.3s | +2.4% | +29.8% | — | small wave |
| WIFAUTON | 2.5s | +1.3% | +35.8% | — | small wave |
| MIMI | 29.5s | +7.5% | +15.0% | — | small wave |
| PAHC | 7.0s | +6.3% | +41.9% | — | small wave |
| CAPES | 15.6s | +1.9% | +27.2% | — | small wave |
| poopcat | 8.6s | +8.6% | +58.0% | — | small wave |
| ELONPHIL | 2.7s | +16.5% | +87.9% | 60s +212.5% | blind rise |
| Sentients | 261.8s | +194.0% | +180.4% | 900s -92.4% | slow grad, false roll |
| butter | 38.7s | +98.9% | +76.4% | 900s -92.3% | slow grad (censored row) |
| solcat | 2.3s | +250.1% | +108.0% | 300s +221.6%, 900s -92.6% | false roll, rising pool |
| Mishu | 11.0s | +2.3% | -98.4% | all -98.5% | instant death |
| CAPYWIFGUN | 2.6s | +401.0% | +256.6% | 60s +264.8%, 900s -92.8% | big wave (4x) |
| TARDTANK | 2.4s | +0.9% | -64.2% | 900s -93.4% | instant collapse |

(the table has 14 rows: 12 records + 2 earlier records; FOMOPUP..CAPES and
poopcat/ELONPHIL predate the full timeline marks - their fixed-horizon rows are
in the sqlite, not the table. The 5 Oct-8 records have the full picture.)

**The distribution:** 9 positive (+15% to +257%), 3 negative (-64%, -98%).
The rule wins on rise-then-decline (CAPYWIFGUN: fire near the peak captures 4x),
loses on instant collapse (Mishu, TARDTANK: the dump happens between polls - no
exit rule beats it; the exit happens at the first post-collapse poll regardless).

**Both failure modes measured:**
1. **False rollover** (Sentients, solcat): a dip in a rising pool fires early -
   the exit captures half or less of the true peak (solcat +108% vs the 300s
   peak +221.6%). The pool kept rising after the fire.
2. **Instant collapse** (Mishu, TARDTANK): the pool's first post-open poll
   already read collapsed - the detector never saw the open. TARDTANK's entry
   was the highest curve ever accepted (78.5 SOL); the pool opened at 0.9% above
   entry and collapsed to -64% within one poll.

**The bimodal fate confirmed on 5 more coins:** all 5 dumped at 900s
(-92.3% to -98.5%). The pool's fate: rise-then-dump is the modal shape.

**The threshold is now a measurable parameter.** A >N%-drop-from-peak condition
would delay the false rollovers (solcat's pool kept rising - a 10% threshold
would have captured the +221.6% peak) while CAPYWIFGUN's true rollover was an
11.2% drop (a 10% threshold still fires; 15% doesn't). The G-clock's 2s price
paths can sweep the threshold per coin. The current threshold: any drop (the
most sensitive setting).

## SWORDPEPE: the instant-graduation archetype (2026-10-07 19:3xZ)

Fourth rollover-capable record. The 60-crossing led graduation by 5s (instant
sweep: vs poopcat 8.6s, ELONPHIL 2.7s, Sentients 4m24s), and the G-clock caught
the pool open within 3.1s. The pool then: open 43.0 -> 47.9 (G+6s, +11%) ->
dip 44.2 (G+33s) -> ROSE to 83.5 by G+300s (+94% above open) -> dumped to
34.0 by G+900s (-3.8% vs entry). A rising pool with a mid-course dip.

The rollover fired at G+19.3s on an -11.2% drop from the running peak
(58.3 -> 51.7) - a false-early fire: the pool rose +60% above the exit price
over the next minutes (60s mark 46.3, 300s mark 83.5). But the dump came
anyway: the 900s fixed-horizon exit was -3.8% vs entry. The rollover exit
captured +46.6% vs entry - it beat the 900s horizon by 50 points and LOST to
the eventual peak (+136.6% at 300s) by 90 points.

Cohort vs the 900s fixed horizon (rollover / fixed-900, all vs entry):
- poopcat: +249% / +181%
- ELONPHIL: +87.9% / +212.5%
- Sentients: +180.4% / -92.35%
- SWORDPEPE: +46.6% / -3.8%
3 of 4 favor the rule; ELONPHIL lost (fired late on a fast-peak coin). Mean
rollover exit 141% vs mean fixed-900 exit 74.3% - on 4 coins, with the
giveback-to-peak the tuning question: Sentients gave back 14 points,
SWORDPEPE 90, poopcat 42, ELONPHIL 125 (the fast-peak coins give back the
most). The next measurable step is a threshold sweep: replay each G-clock
price path at 5/10/20/50% drop-from-peak thresholds and compare the exit to
the eventual peak and the fixed horizons. The threshold that maximizes
captured-of-peak while minimizing false-fire giveback is the design answer;
4 paths is too few to declare it, but the sweep can run on every new record
as the instrument accumulates paths.

## Historical buyer-count exploration — not confirmation

The old `analyze_accept_clues.py` selected complete cases, changed LOCK
membership as more outcomes appeared, and claimed 12 preregistered candidates
while executing nine comparisons. Its historical buyer-band “survivor,”
causality and confirmation interpretations are withdrawn. The source journal
and historical captures remain unchanged.

The replacement freezes planning-selected membership, features and observations
before analysis and reports each comparison's actual available pair counts.
It retains repeated mints and incomplete outcomes; a fixed retrospective split
does not become an untouched forward test. Descriptive correlations and gross
price marks do not establish fees, executable returns or an entry edge.

Existing buyer/mayhem gate support is not an authorization to change or arm a
policy. This correction changes neither the running paper settings nor risk
caps. Missing historical Jev scores cannot be invented, and enabling a paid
scorer is not part of this repair.

## Creator-vault observations: earlier annuity verdict withdrawn

The earlier “earned fees,” collection amounts, pacing causality and payback
claims in this section were not established by the retained snapshots and
are withdrawn. The historical JSONL/logs remain unchanged.

The tracker observed WSOL token-account balances belonging to creator-vault
PDAs, not pool-specific cumulative revenue. One creator may own several
pools; adding per-pool observations can count the same balance repeatedly.
Decreases do not identify fee claims or recipients, increases do not
attribute fees, and an absent account was historically recorded as zero.
Truncated creator identities and omitted mints in old rows cannot establish
an identity-bound baseline or lifetime income.

Schema v2 retains complete creator/PDA/ATA identity, raw WSOL units, explicit
null censors, baseline status and actual elapsed interval. Reads validate
token mint/program/authority and deduplicate shared vaults. Only exhausted
transport reads are unavailable; malformed responses and permanent errors
fail the snapshot. The passive summarizer verifies retained schema/baselines,
not independent account authenticity or on-chain PDA provenance.

First v2 observations cannot pair with legacy rows. At least two valid
same-identity v2 observations are needed for a balance change, which still
does not establish fees, payouts or launch profitability. Creator economics
remain unvalidated, not merely blocked on funding. Synthetic launch quote
checks do not attest native execution, applicable live fee tiers or an
actually collected rebate. See the corrected creator-pilot design.


## The wallet census + the net regime re-test (2026-10-09 14:9xZ)

Two deliverables from the "success leaves clues" directive.

**The wallet census** (`token-lifecycles/census_wallet_pnl.py`, n=24,015
coins, 101,672 wallets, exact constant-product valuation at pre-trade
reserves, 1.25% fee per side, mayhem vault excluded, tape-gap coins
flagged): realized-positive 19,513, realized-negative 57,605 - most
wallets lose, matching the public Dune aggregate. The top 20 by realized
PnL share one signature: **median first-trade dslot = 0** - they enter at
the CREATION SLOT across 43-114 launches each, flip in 17-209 slots,
win 42/43, 43/44, 54/54. These are the launch-inside complex (creator
side wallets, launch services, bundled snipers) - the same cohort the
whale census found on the supply side. Outside followers cannot copy the
dslot-0 entry: it is part of the launch bundle itself.

**The net regime re-test** (the queued entry-verdict re-check; all 38
resolved accepts are within the instrument's 3-day life, so the window
is the full record; net = gross minus the measured 1.25% x 2 sides):
- All accepts (n=38): r60 +12.13% gross = **+9.63% net**; r900 -27.6%
  gross = -30.1% net. The 60s window is net-positive on average in the
  current regime; the 900s hold is not.
- The buyer band (2-3 buyers, non-mayhem, n=7): r60 +128.5% gross =
  **+126.0% net**; r900 +45.7% gross = **+43.2% net**. Net-positive at
  BOTH horizons, consistent with the TRAIN/LOCK split.

The crossing-entry verdict upgrades from "dead in moderate regime" to
**conditionally alive in the current regime, gated by the buyer band**
(n=7 - the >=30-cohort referee still applies; slippage on exits is not
modeled; one regime is one sample). The inventory entry 3 is now the
measured follower-entry candidate; arming the band gate is the
operator's call.

## The strategy inventory: verdicts are conditional, not terminal (2026-10-09 13:5xZ)

The operator's thesis: hold a large inventory of measured strategies, each
with pre-registered regime conditions, and deterministically strike when a
strategy's risk is acceptable. Adopted - with two disciplines that keep it
honest:

1. Inventory entries are MEASURED-CONDITIONAL, not hypothesis-stocked
   (the threshold-miner lesson: a wide net of untested candidates is an
   overfitting factory, not an arsenal).
2. Every entry ships a pre-registered deterministic regime detector -
   no discretionary "it feels hot" switches.

The artifact: `docs/STRATEGIES.md` - 7 strategies (2 measured-live, 3
measured-dead-with-detectors-armed, 1 dead-by-architecture, 1 forbidden),
the regime panel (volume, fee configs, riser-share, net marks, coupling),
and the queued censuses that widen the net.

The reframe this adopts: the week's verdicts were CONDITIONAL all along -
"crossing-entry dead" meant "dead in a moderate regime"; "LP dead" meant
"dead at the current fee config / riser mix." The regime panel makes the
conditions explicit and the re-arming deterministic.

## The leaderboard challenge: house revenue vs trader distribution (2026-10-09 13:2xZ)

The user's challenge: thousands of pump.fun users profit daily, the
leaderboard shows it, the platform makes fees daily - so "no one wins"
cannot be right, and the research must reconcile with it. Researched
(external sources, Oct 2026):

- **House revenue**: pump.fun ~$1.1M/day (Sep 2026; Fomo briefly beat it
  at $1.76M), $50M daily volume / 905k transactions on the mobile app
  (Aug 2026). Curve fee 1% per trade. The house wins ~$1M/day
  REGARDLESS of which traders win - the market does not require the
  average trader to profit, it requires the house's tax on every trade.
- **Trader distribution** (Dune, 6-month cohort): median trader ~$0,
  bottom 25% negative, top 10% >$557, top 1% >$22,471. 99.6% never
  locked >$10k. So "thousands win daily" = the visible top tail; the
  median is flat and the bottom funds it.
- **The regime fact**: 73.3% of pump.fun traders were monthly-profitable
  in April 2026 (CoinGecko/Dune ATH) after ~2 years of majority-red
  months. Profitability is regime-conditional: in a sustained pump
  regime the MEDIAN follower wins monthly; in chop the median loses.

Reconciliation with this instrument's verdicts:

1. "No one ever won" was never the finding. The finding is narrower:
   specific follower MECHANISMS (crossing-entry at retail latency,
   follower LP on both venues) are EV-negative. The winners the
   leaderboard shows are creators (our measured +EV role), launch
   services, and insiders - the census cohorts.
2. The challenge corrects one scope error: the entry verdict was
   measured in a moderate regime ($1.1M/day, post-peak). The 60s/300s
   gross marks were POSITIVE on several coins; net-of-cost EV is the
   question, and it is regime-conditional. A trailing-30-day net mark
   re-test is cheap and due.
3. The leaderboard is an untapped census: the top-100 consistent
   winners' behavior (entry timing vs creation, hold time, coin
   selection) is measurable from the same tape the whale census used.
4. "We can't figure out a consistent way" - the instrument already
   found it: the creator annuity (measured +EV, ~15-day payback). What
   is pending is capital and approval, not understanding. The AI
   advantage built the thing that identified it; the binding constraint
   on the trading roles was latency and structure, not model quality.

Added to the roadmap: the regime re-test (net marks over 30 days) and
the leaderboard winner census.

## The letsbonk CPMM decay census: the LP route measured dead (2026-10-09 12:3xZ)

The aggregator was blind, so the decay was read on-chain. The CPMM pool
PDA is derivable: `["pool", amm_config, quote_mint, base_mint]` under the
CPMM program — amm_config sits at offset 8 of the pool account (the live
one for this cohort: D4FPEruKEHrG5TenZ2mpDGEfu1iUvTiqBxvpU8HLBvC2), the
WSOL vault at offset 72. Verified against a known pool.

Cohort: 197 WSOL migrated pools on the current epoch (byte-9 >= 0x04),
171 with fund >= 40 SOL, 21 with a CPMM pool on the D4FPE config (the
other 150 migrated under a different amm_config — a sampling bias, not
an absence). Their WSOL vault balances today vs the fund raised:

| fund | SOL side now | decay |
|-----:|-----:|-----:|
| 85.0 | 108.00 | +27.1% |
| 85.0 | 61.45 | -27.7% |
| 159.9-164.5 | 63.9-65.5 | -59% to -61% |
| 2620.8 | 904.3 | -65.5% |
| 85.0 (x9) | 18.6-30.4 | -64% to -78% |

**Mean SOL-side ratio 0.352, median 0.271** — the migrated pool's SOL
side loses ~73% (median) of its liquidity post-graduation. An LP who
joined at migration is down ~73% before fees; the CPMM fee stream would
have to exceed that to break even. The one +27% riser (1 of 21) is the
familiar bimodality cross-venue.

**The letsbonk LP route is mechanically open and economically dead.** The
expansion map's last open lead closes with data: every follower role is
now measured dead on BOTH venues (entry at latency, PumpSwap LP 0 bps,
letsbonk LP -73% decay, news/LLM pipelines structurally too slow). The
only measured +EV roles remain supply-side: the pump creator annuity
(~15-day payback, requires real funds and explicit approval) and the
platforms' fee streams. The instrument keeps measuring on both venues.

## The letsbonk migration census: 85.0 sweeps, locked LP, invisible on aggregators (2026-10-09 12:1xZ)

The pool-fate collector (`letsbonk-buy-sell/collect_cpmm_fate.py`) plus
on-chain traces answered the migration mechanics. All read-only:

- **Cohort**: 17,878 migrated LaunchLab pools; 11,097 WSOL-quoted (62%);
  the rest USD1 (2,010), the Trump token (776), and meme coins. The
  non-WSOL "fund raising" numbers are in junk-token units — a 22.4M
  figure is 22.4M tokens, not SOL. WSOL-only is the real cohort.
- **The 85.0 SOL signature, cross-venue**: WSOL migrated pools' top fund
  is 86.1, then a wall of EXACTLY 85.0 — the same whale self-sweep median
  measured on pump.fun, and the same number the pump forward-test
  observed as max real_quote. 170/300 sampled WSOL migrated pools (57%)
  are exactly-85 whale self-sweeps.
- **Migration mechanics** (verified in a migration tx):
  `migrate_to_cpswap` creates the Raydium CPMM pool + LP mint (2
  CPMM-owned accounts), LOCKS the LP via Raydium Lock (LockrWmn), and
  mints the rights NFT via Metaplex. Post-migration the LaunchLab vaults
  are empty; platform_scale 100% = StonkFun holds the LP-rights NFT.
  Failed migrations show LaunchLab 6006 (MigrateTypeNotMatch).
- **Fate**: every sampled whale-band coin has ZERO DexScreener pairs
  (60+ mint queries). Either the aggregator drops them or the pools
  trade in the dark — post-migration volume is unmeasured from here.

The LP verdict updates: the route is **permissionless** (anyone can join
the migrated CPMM; fees accrue to LPs pro-rata) and the seed LP is
**locked** (no platform rug of the base liquidity — a real structural
difference from PumpSwap's 0-bps-for-LPs design). But zero aggregator
visibility on 57% whale self-sweeps means the post-migration volume —
the annuity's income — could not be read from the aggregator; it needs
the on-chain G-clock extension on the CPMM pools directly.

## The letsbonk fee census: no annuity, an open LP door (2026-10-08 19:4xZ)

The cross-venue expansion question: does the pump.fun endgame transplant to
letsbonk (Raydium LaunchLab)? Measured live
(`letsbonk-buy-sell/verify_fee_config.py`, read-only; layouts re-derived
against the on-chain bytes - LaunchLab accounts carry an 8-byte anchor and
the platform config is 944 bytes with 184 undecoded trailing bytes):

- Curve trade fee: **25 bps (0.25%)** (GlobalConfig.trade_fee_rate = 2500)
- Platform fee: **1.00%**, **creator fee 0.00%**
- **Migration LP split: platform 100% / creator 0% / burn 0%** - the
  graduated pool's entire migrated liquidity goes to the platform's NFT
  (StonkFun, the letsbonk.fun operator; the fallback discovery config
  "Spots.fun" has the identical shape)

Three verdicts fall out:

1. **The pump endgame does NOT transplant.** No creator annuity exists on
   letsbonk - the creator earns 0 bps on the curve and holds 0% of the
   migrated pool. The self-sweep playbook measured on pump (66% of whale
   sweeps; ~15-day payback at 30 bps) has no letsbonk equivalent: a
   creator there is donating their sweep to the platform.
2. **The LP route is mechanically OPEN on letsbonk's migrated pools.** The
   migration type is CPSWAP into a Raydium CPMM pool, where trade fees
   accrue to LPs pro-rata - the first venue in the census where a follower
   can own a share of the fee stream (PumpSwap's fee config gives LPs 0
   bps; LaunchLab's curve has no LPs at all). The economics remain
   unmeasured: the pool-fate decay that killed the pump LP math is a
   pump-side measurement; bonk-side decay and post-migration volume are
   open rows.
3. **The platform is the fee annuity holder on letsbonk** - the mirror of
   the pump creator. StonkFun earns 1% of curve volume plus 100% of the
   migrated pool's liquidity rights.

The instrument extension that closes verdict 2: a bonk-side pool-fate
tracker (decay + fee accrual on N migrated pools over 24h, the
simulate_lp_income transplant). Until it runs, the letsbonk LP route is
"mechanically open, economically unmeasured".

## The threshold sweep: closed, no-threshold wins (2026-10-07 16:1xZ)

16 rollover-capable coins under the continuous detector. The no-threshold fire
(exit at the first poll below the running peak) beat the 900s timeline on 13,
tied on 2 already-dead coins, and lost once: WICK's fire caught +13% and the
pool rebounded to +152%. The fire beats the timeline by +1,865 points in
aggregate (Sentients +272.7, CAPYWIFGUN +349.4, solcat +200.6, butter +168.7,
FOMOPUP +145.2, PAHC +132.7, CAPES +120.2, PEA +100.8, SWORDPEPE +50.3,
MEMENCY +33.2, TARDTANK +29.2, Mishu +0.1, WICK -139.2).

The drop-from-peak distribution spans -75.2% (MEMENCY) to -1.8% (WIFAUTON).
The sweep asks: does a skip-threshold keep WICK without losing a crusher? To
skip WICK's -5.5% fire the threshold must exceed 5.5%, which also skips
CAPES (-5.2, +27 vs -93) and Sentients (-2.4, +180 vs -92). Sentients proves
small dips DO predict crushers: the same ~2% dip led Sentients to +180 and
WICK to +152. The best curve-fit threshold (~5.3%) keeps WICK by +152 but is
supported by exactly one coin - optimizing noise against +1,865. The
no-threshold fire is the measured default; the rebound counter-archetype
(WICK) is its known, quantified cost.

## The 2% trailing exit is dead: five live samples (2026-10-07 17:0xZ)

The condition-driven exit (2% dip from running peak, fires at most once per
graduation) now has five rollover-capable live records. Against each coin's
best fixed-horizon mark:

| coin | rollover exit | best fixed mark | outcome |
|---|---|---|---|
| poopcat | +427.9% | 60s +433.0% | matched |
| ELONPHIL | +87.9% | 60s +212.5% | lost 2.4x |
| Sentients | +180.4% | 300s +198.4% | lost |
| TARDTANK | -64.2% | 300s +24.5% | lost to a positive mark |
| WICK | +13.0% | 300s +193.9% | lost 15x |

It never beat the best fixed mark. The 2% dip fires on noise: it exits
rising pools' dips (WICK: exit +13.0% while the pool reached +193.9% at
300s) and rising pools' crash-starts (TARDTANK: exit -64.2% while the pool
rebounded to +24.5% at 300s). The dump-type rollover (TARDTANK) is the only
record where the exit caught a crash it could not avoid - and even there
the 300s mark was positive.

The trailing-stop design question is real but the sweep needs the full
price path: the G-clock detector polls every 2 seconds through the
post-graduation window yet stores only p5/p30/p120/peak/rollover today.
Follow-up: store the detector's sample array per graduation, then sweep
rollover thresholds (2/5/10/20% dips) on the real paths. The five live
samples already bound the 2% exit: on this cohort it is a loser.

## Sentients: slow graduations and false rollovers (2026-10-07 15:0xZ)

Third rollover-capable record, first under the continuous detector:
accepted at 60.12 SOL, graduation **4m24s** after the crossing (vs
8.6s/2.7s — slow graduations exist; the entry rides a grind, the 60s
mark shows +2.37% still on-curve). The detector fired at G+26.9s on a
2.7% dip — a FALSE rollover: the pool rebounded above the recorded
peak (p30 7.416e-7) and stayed elevated (p120 +16.3%). The exit still
captured **+180.4%** vs entry; the true peak was +194%.

The timeline's fate on the same coin: 300s **+198.4%**, 900s
**−92.35%**. The rollover rule crushed the timeline — but firing on a
2.7% dip is the tuning question: a **rollover threshold** (exit only on
a >N% drop from peak) trades exit speed against giving back gains, and
the G-clock's price paths make the threshold sweep measurable per coin.
The recorded peak is the peak at rollover time (first-write-wins);
post-rollover rebounds are visible in the scheduled samples.

Cohort: 2 rollover records — ELONPHIL +87.9% (fired late, blind rise),
Sentients +180.4% (fired early, rebounded). Both beat the fixed 900s
horizon (−91.5%, −92.35%); the fast-peak coins (fixed 60s +212%) are
where the timeline still wins. The threshold sweep is the next
refinement.

## The LP route is locked: fee config measured (2026-10-07 11:0xZ)

Read the live fee_config PDA (the only tier, all PumpSwap pools):
**lp=0 bps, protocol=95 bps, creator=30 bps.** Liquidity providers earn
nothing — depositing into a graduated pool donates capital to absorb
the dump's IL while protocol and creator split 100% of the fees. The
follower cannot own the fee stream by LPing.

The corrected annuity: creator=30 bps × 1,858 SOL/24h median volume =
~5.6 SOL/day — payback ~15 days on the median 85 SOL self-sweep (not
the 5–9 days estimated at 50–90 bps; the actual config is lower but
still a real annuity).

The complete, measured economics of the pump.fun endgame:
1. Creators self-sweep (66%) to graduate — buying a fee annuity.
2. The followers' churn IS the annuity's income; the decay pays them.
3. LPs are locked out (0 bps) — the stream is creator+protocol only.
4. Entry trading is dead at retail latency (−9% to −32% EV).
5. The serial snipers are paid launch services (no fees, no sells).

The only profitable roles are creator (annuity) and protocol (95 bps).
A follower at retail latency has no +EV role in this game — measured,
sealed, and re-openable only if the fee config or the regime changes.

## The whale census: the self-sweep fee annuity (2026-10-07 09:0xZ)

Wallet-level census of 477 whale-swept graduations (24h tape): **66.2%
of sweeps are SELF-SWEEPS — the whale is the creator.** Cashback is not
the driver (1.5%). The remainder splits into a scripted launch-service
class (four wallets × 2 coins × exactly 170 SOL, all self-sweeps) and
serial snipers who never create (top: 15 coins, 392 SOL).

**The business model is a fee annuity**: median self-sweep 85 SOL buys
a graduated pool with median 1,858 SOL/24h volume; at creator-set fee
bps (≈50–90) that pays ~9–17 SOL/day — **payback in 5–9 days**. The
creator never sells because the POOL IS THE ASSET: the followers' churn
is the fee stream. This explains the whale's refusal to sell (0/476),
the pool's decay (churn pays the creator), and the manufactured-dump
pattern (volume for the annuity). The serial snipers (never creators)
are a paid launch-service class — the creator pays off-chain to have
their coin graduated.

The follower's whale-ride is dead at latency; the creator's game is
owning the fee stream. Open question for the follower: can a non-creator
own the stream (LP into graduated pools — fee share × liquidity share)?
Measurable without funds via simulation against the observed volumes.

## First dual-clock record: rise then dump (2026-10-07 06:22Z)

poopcat's complete picture (both clocks on one coin): the pool rose
+101% by the 60s mark (G+51s, +40% above open at G+2min), rolled over by
the 300s mark (−39%), and dumped to −91.5% by the 900s mark. The
post-graduation liquidity window is real but brief: **~2 minutes of
rise, then the crash** — the tape's 18s half-life was the dump-type
without a grace period.

The condition-driven exit rule the data defines: **hold while the pool
rises, exit the moment it rolls over.** The G-clock's samples (G+5/30/
120s) measure the rise; the accept-anchored marks measure the rollover
and dump. A live rule would watch the pool price path and exit on the
first lower sample — dynamic, condition-driven, no timeline. Every
coin eventually dumps to −90%+: the rise window is the only exit
opportunity, and missing it is the difference between +101% and −91.5%
on the same coin.

## First complete G-record: the pool fate is bimodal (2026-10-07 06:09Z)

poopcat (entry 62798): accepted 06:06:13Z at real_sol 60.01, graduation
detected **8.6s** after entry, pool open 4.621e-7. The G-clock samples:
G+7.4s **+2.0%**, G+32.5s **+16.9%**, G+122.3s **+40.0%** — this pool
RISES after migration, contradicting the tape's 18s median half-life.
The post-graduation pool fate is **bimodal**: the tape's median captured
the manufactured-dump majority; a minority of pools hold and climb.

And the first **positive** paper mark: +100.98% at the 60s pool exit
(entry 2.517e-7 from the accept event, exit 5.058e-7 pool-priced). Even
paying the measured 1-2-slot entry drift (+25%), the real return is
~+61% on this coin.

Cohort: 4 complete pool-priced records — 1 winner (+101%), 3 losers
(−75%, −84%, −91%). Mean still deeply negative; variance enormous.
The latency frontier's mean-EV death stands for the cohort; the bimodal
tail is why the ≥30-cohort threshold is the referee. The G-clock now
accumulates the live pool-fate distribution — the promotion question
re-opens only if the cohort's mean crosses its break-even.

## The latency frontier: the promotion question answered (2026-10-07 05:4xZ)

Measured on 215–273 graduated coins with slot-level trades: the entry
price is **+25–56% above the X crossing by the time a realistic accept
lands (1–2 trades/slots)**, settling to +9–14% by +10 trades. Lower
gates are worse — crossing X=40 earlier means more sweep ahead:
X=40 +1 trade **+56%**, X=50 **+55%**, X=60 **+25%**.

EV arithmetic (coupling EV at the crossing ÷ 1-trade drift):
X=60 **−9.4%**, X=50 **−28.9%**, X=40 **−31.8%**. Even the p25 tail
eats 5–17%. **At retail latency the whale-ride entry is dead at every
gate level** — the +13.3% theoretical EV exists only at the crossing
price, which is unreachable. The live marks' honest losses (PUP −91%,
SPILL −75%, CLAUDIA −84%) are this drift, measured, not bad luck.

The edge exists only for same-slot execution (the whale's own game:
colocated infra + priority-fee advantage). That is an infrastructure
decision requiring explicit approval, not a strategy change. The
instrument stays live: if the regime changes or infra upgrades, the
G-clock and the sealed bundles re-open the question with evidence.

## The whale never sells: exit condition found in the tape (2026-10-07 03:0xZ)

Analysis of 476 whale-swept graduations (20+ SOL sweeps, 24h tape,
wallet-level trade records): the whale **never** sells into the pool
— 0 of 476. 12.2% of whales buy MORE. The post-graduation decay is
not the whale dumping — it is the followers dumping on each other,
and the pool's liquidity halves in a median of **18 seconds** after its
peak (p25: 2s; 68% gone within 60s, 89% within 300s).

The condition-driven exit rule follows directly from the measured decay:
**the exit condition is graduation detection, priced in seconds.** The
pool's opening liquidity IS the peak; every second of detection or
submission latency is paid out of the exit price. This is exactly the
user's hold directive made concrete: no timeline, hold by conditions —
and the condition (graduation) with its measured urgency (18s half-life).

New instrument deployed: **graduation-anchored marks**. Each planned
paper entry spawns a watcher polling the curve every 2s until complete,
then records the pool-open price and samples G+5/30/120s into
`grad_marks` (first detection wins; every failure censors a column,
never fatal). The accept-anchored horizons measured the mixed clock;
the G-clock measures the strategy's actual exit window. First
G-anchored records land with the next whale-swept graduation.

## Hold policy: conditions rule, never a timeline (2026-10-07, user directive)

Holds are **dynamic and condition-driven** — never a fixed timeline. The
strategy holds until conditions say exit (TP/SL bands, graduation, flow or
liquidity deterioration), bounded only by a safety cap (max_hold_time,
stop-loss) that prevents losing more than the edge pays — a backstop, not
a target. The 60/300/900s paper-mark horizons are **measurement
scaffolding**: they record the decay curve so an exit rule can be designed
from evidence; they are not the policy, and no config forces a minimum
hold. The bundle reports **average priced-mark observation elapsed time**
(`avg_observation_elapsed_s`, `observation_elapsed_by_reason`), not average
realized hold. A price mark or rollover trigger does not attest an executed
exit, received proceeds or net profit.

## First complete pool-priced whale-ride cycle (2026-10-07 02:29Z)

CLAUDIA (lesson 62158): accepted 02:14:04Z at real_sol 60.54 (in-band),
graduated within minutes, and all three horizons priced off the canonical
pool under the fixed code — 60s **−74.99%**, 300s **−80.0%**, 900s
**−83.74%**, every mark `graduated_pool_exit`, journaled without crash
or censor. The mark path works end to end: gate accept → graduation
→ pool-priced honest exit → journal.

The honest pattern so far (n=3 complete records): PUP −91% (mid-band),
SPILL −9.5/−29/−75% (on-curve death), CLAUDIA −75/−80/−84%
(pool decay). All heavy losses. The tape's +94–165% graduator mean is
entry-at-sweep-start; the paper arm enters when it sees the accept —
mid-sweep, after the first wave. That entry-timing gap is precisely the
question the instrument exists to answer, and the answer accumulating is:
at retail latency, accepting at the visible accept loses heavily. The
coupling indicator's ACTIVE band says whales beat the curve up through 60;
it does not say a follower can enter after the fact and survive the
pool-open decay. Both facts now live in the same honest ledger.

## The fatal root cause, closed (2026-10-07 02:10Z)

The 'Failed to initialize or start trader' crashes at 17:55Z and 21:57Z
were not the Jev 402 or parse errors — the full traceback finally showed:
`price, reason = pool_price, "graduated_pool_exit"` parses the RHS as a
2-tuple, so `price` received the helper's whole `(price, reason)` tuple.
`finish_paper_mark`'s `isfinite` then raised TypeError, which
`_paper_mark_finished` forwards as fatal by design (persistence failures
kill the bot) — so EVERY graduated_pool_exit mark crashed the session.
Both call sites now unpack properly; proven with a stubbed-helper run
delivering a numeric exit_price to the journal. The three mark-path fixes
(settle+retry, TypeError fallthrough, tuple unpack) compose: the next
graduation prices honestly instead of censoring or crashing.

## Pool exit price read retry (2026-10-07 01:23Z)

The overnight run's graduated coins (CITED, GOLDBONER, SDOGE) censored
their marks with TypeError because the pool/vault accounts were briefly
unreadable mid-migration — the same transient race the shadow's
pool_unreadable handles. Verified live: CITED's pool reads fine minutes
after the same migration (650B base tokens, 0.010 SOL quote — the whale
took the SOL, the honest exit price is a loss). The pool price read now
settles 2s and retries once on TimeoutError/ValueError/TypeError before
censoring — bounded by the mark's own budget. 830 tests pass.

## First overnight evidence (2026-10-07 00:38Z, 14h autonomous)

The paper arm ran 14 hours unattended and produced the first real evidence
cohort: 8 in-band entries (real_sol 60-79 SOL, all inside the ACTIVE band
the coupling indicator called), 1328 decisions (~50/hour sustained), 71
completions / 61 pool rows on the shadow (37 new graduations captured).

**SPILL (61646)**: the first complete honest 3-horizon outcome — entered at
62.39 SOL, the coin died on the curve (never graduated): 60s **-9.5%**,
300s **-29%**, 900s **-75%**. Exactly the cold-arm decay pattern the tape
predicted. Three horizons, same entry, populated prices, honest outcome.

**CITED, GOLDBONER, SDOGE** graduated but their marks censored with
TypeError (the pool/vault accounts were briefly in migration-unfinished
state at mark-read time — the same transient race the shadow documented).
The censoring is correct fail-closed behavior; the pool-exit pricing needs
a read retry to survive the race. Verified live: CITED's pool reads fine
minutes later (650B base tokens / 0.010 SOL quote — the whale took the
SOL, the exit price is honest and it's a loss).

**The active-band entries are real**: the gate accepted exactly the coins
the coupling indicator called (real_sol 60-79, the X=50-60 ACTIVE band),
8 entries in ~14 hours (~0.57/hour — matching the 8.5% coupling at 60
crossings × ~25/hour crossers). The promotion case's cohort is building.

**TypeSafe's 402** (no API credits): Jev scoring degrades gracefully
(non-fatal), the gate runs without it. The evidence does not depend on Jev.

## Trend strengthening — the ACTIVE band expands downward (2026-10-06 16:18Z)

Three hours after the first positive pooled mean, the trend REVERSED the
expected decay: X=60 pooled +1.1% (from +0.4%), coupling 11.4% vs 7.6%
break-even (ACTIVE 8/8); X=50 pooled +0.6% (crossed zero), coupling 8.5%
vs 6.7% (ACTIVE 3rd consecutive); X=40 hit exact break-even 5.5/5.5 (from
dormant). Max curve real_sol in the paper window: 52.99 SOL and climbing
(11.5 -> 23.2 -> 53 over three hours). Cohort: 21 pool-priced outcomes, 24
pool rows, 34 completions, 132 paper decisions in the last 66 min
(~120/hour capacity holding).

The "regime death" hypothesis is answered: the opposite happened. The
manufactured-sweep era did not cool — the quiet stretch was a lull inside
a warm regime. Sealed snapshots: 8 (e68fee6c was the 10:34Z reading; every
hash recorded at reading time). The promotion case now has: positive
pooled forward means at TWO levels, coupling margins of +1.8/+3.8pp, and
a warming market — the remaining gates are duration (24-48h of sealed
snapshots, ~6h banked) and the cohort threshold (~21 of ~30 toward it).


## First positive pooled forward mean (2026-10-06 15:12Z)

X=60's pooled entry mean crossed zero on live shadow data: +0.4% mean
(153 entries, 32.7% win) with coupling 9.3% vs 7.1% break-even — the
graduation exits' weight now outweighs the cold arm. X=50 flipped ACTIVE
again too (6.8% vs 6.2%, pooled -0.3%). Cohort: 15 pool-priced graduator
outcomes, 16 pool rows. The trend: X=60 ACTIVE 7/7 snapshots (8.0-10.2%
coupling), X=50 4/7 (currently ACTIVE — the flicker band keeps producing
evidence from both sides). Second in-band live entry landed (78 decisions
in the last window, max curve 23 SOL — still below the 60 gate, so the
session correctly stayed flat while the market cooled). The promotion
case's arithmetic now has its first positive forward term; the gate stays
at 60, sealed snapshots accumulate, and the promotion decision remains
gated on days-scale trend stability + the paper arm reproducing the
graduator mean on its own fills.


## Mark hardening: one lost race must not kill the evidence rig (2026-10-06 19:0xZ)

The burst arrived and killed the paper bot TWICE — both times ~60-90s after
an accept, both times the same shape: the whale sweep beat the paper entry
(zeroed virtual reserves — the buy correctly failed closed at 17:54:30 and
18:41:45), and then the ENTRY'S OWN MARK TASK raised through the same
strict decoder at its horizon read, routing to _fatal_monitor_errors ->
trader shutdown. One lost race killed every other coin's pending evidence
during the heaviest burst of the week.

Fix: the mark body is fully contained — any exception becomes a censored
mark row (read_error:<type>) and the trader keeps processing. Second-order
lesson: the gate accept racing a sweep completion is the whale-beat case;
the buy failure IS fail-closed behavior (no funds moved), but the evidence
machinery must survive losing that race. The lesson journal now has 108
deleted garbage entries (creation-time accepts from the pre-gate-fix era,
documented above) on top of today's two burst deaths — all recoverable
from backups if ever needed.



## Second live entry; first keeper recovery (2026-10-06 14:10-14:35Z)

JEANPHILF (suUDqaQk...) accepted 14:08:57Z at real_sol 76.89 SOL — inside
the X=60-80 band at 3.55e-7 — and graduated within minutes (complete=1,
curve zeroed). Its 60s mark resolved WITH the populated entry price (the
gate fix verified in production); the 300/900 marks cancelled because the
session died at 14:09:57Z on `trade_stream_interrupted` — one coin's Geyser
subscription loss invalidates the whole stream per the flow-loss design,
cancelling every other entry's pending marks with it. The keeper detected
the death in 61s and restarted cleanly (first real recovery; adoption had
been verified only in tests). The cancelled marks are the honest record of
an interrupted session, not lost evidence: censoring is what they exist
for. Design note for the next iteration: a paper-only restart could drain
marks first (the `_drain_paper_marks` path exists), but trading semantics
must not change for measurement convenience — watch the censor rate before
changing anything. Snapshot at 14:35Z: session healthy (54 decisions since
restart, no accepts — max curve 36 SOL), 16 pool rows on the shadow.


## Coupling trend (2026-10-06, continuing; X=50 flickers, X=60 holds)

Snapshots, all sha256-sealed (evidence-bundle.json overwritten in place —
each hash below was recorded at reading time): 09:30Z X50 8.7/8.0 ACTIVE,
X60 10.1/7.8 ACTIVE; 09:53Z X50 5.4/7.0 dormant, X60 8.0/7.8 ACTIVE;
10:04Z X50 7.3/6.4 ACTIVE, X60 10.1/7.8 ACTIVE; 10:14Z X50 6.2/6.4 dormant,
X60 8.6/7.1 ACTIVE; 10:34Z (bundle e68fee6c) X50 5.9/6.9 dormant, X60
8.3/7.5 ACTIVE; 10:47Z X50 7.3/6.9 ACTIVE (13 graduators), X60 10.2/7.5
ACTIVE. Score: X=60 ACTIVE in 6 of 6; X=50 ACTIVE in 3 of 6 — the stable
band is X>=60 and the paper gate sits there (min_real_sol 60). Cohort: 14
pool rows, 23 completions, ~908 crossers at X=20. The X=50 flicker is the
margin itself — no entry capital goes there until it holds.


The activation indicator's first real-time swing, now three sealed
snapshots deep: X=50 read ACTIVE at 09:30Z (8.7% vs 8.0%), dormant at 09:53Z
(5.4% vs 7.0%), and ACTIVE again at 10:04Z (7.3% vs 6.4%); X=60 tracked
10.1% vs 7.8% ACTIVE through all three. X=20-40 stayed dormant throughout.
Reading: the whale-ride's viable band is X>=50, flickering at X=50 and
holding at X=60, with 12-22 completions seen per snapshot (12 pool rows
captured). The PUP lesson stands as the honest mid-band entry example
(-91%). No promotion decision is implied by an oscillating indicator; the
trend needs sealed snapshots over days, and every bundle carries its own
sha256 so later readers can verify the sequence was not rewritten.

## Whale-ride paper session live (2026-10-06)

Launched (dry-run, designated wallet, supervisor keepers): gate waits for
the real_sol crossing (min_real_sol 50, max 80, 30s window), holds through
graduation, exits on the venue-state reveal. Launch sequence surfaced and
fixed three real layers: the wallet env-override (documented), the paper_marks
schema debt (two additive migrations + a rebuild, backups kept), and the
gate's no_wait shortcut bypassing min_real_sol when min_buyers=0 — 108
creation-time garbage entries produced and deleted before the fix. The
serial processor also cannot sustain minute-scale waits (~46 coins/hour
arrivals vs ~2/min processing): window shortened to 30s/75 slots, with the
slow-crossing blind spot documented and the shadow still covering it.

**First entry landed**: PUP (HdD6Eaw...) accepted at real_sol 50.87 SOL
(ACTIVE band), lesson 59679, marks scheduled at 60/300/900s. Evidence
aggregation: `build_evidence_bundle.py` — one command, sealed sha256,
phantom audit + held-out table + live coupling + journal counters. First
two runs showed the coupling indicator cooling in real time (X=50: 8.7%
ACTIVE -> 5.4% dormant within the hour): the market, measured, saying the
strategy sleeps.

## What the ECC harness taught (2026-10-05, adopted)

`~/GithubProjects/ECC` (an agent-harness plugin catalog: 68 agents, 293
skills, hook workflows) yielded one built artifact and one money-path
hardening:

- **Built: `keep_whaleride_shadow.py`** — a keeper for the forward-test
  collector, from ECC's loop-operator/loop-status/observer patterns:
  identity-verified PID (`ps` argv, not `kill 0` — LESSONS #5), progress
  watermark over the JSONL tail, stall reasons (process dead / rows stale
  10 min / slot frozen 15 min), TERM-then-KILL child cleanup, quick-death
  backoff ladder 30s→8min, and a `keeper.gave-up` marker instead of
  crash-looping.
- **Built: the loss circuit breaker** (`DrawdownBreaker` in
  `core/execution_policy.py`, config `execution.max_consecutive_losses` and
  `execution.max_session_drawdown_quote_raw`) — from ECC's
  llm-trading-agent-security checklist. Gap analysis against the bot: spend
  caps, mandatory min-out, full outcome ledgers, key isolation and per-quote
  caps were already covered; the missing control was a LOSS-based halt.
  Semantics: closes feed `pnl_quote_raw` from the `_link_lesson_outcome`
  funnel; consecutive losses and per-quote drawdown latch a session-wide
  entry halt while open positions keep their exits; unpriced closes are
  ignored; a per-quote mapping that omits the entry's quote fails closed.
- **Recorded, not built**: the MCP circuit breaker (persisted exponential
  backoff for unhealthy endpoints) maps to our RPC handling, but the client
  already carries per-request cooldowns; the gan-harness bounded
  generator→evaluator loop fits offline strategy iteration only; ECC's
  mle-workflow/recursive-decision-ledger disciplines (point-in-time features,
  append-only evidence, fail-closed promotion) confirm practices the repo
  already follows rather than add new ones.

## Forward-test instrument bug (2026-10-06, fixed)

The shadow's first ~10 hours under-counted graduations by design error: the
batch chunking interleaved each coin's pool/vault keys with its curve key
while the tick still sliced values as `[all curves][all extras]`. Pool
accounts were decoded as curves (992 false `curve_unreadable` quarantines),
misalignment raised IndexError (7,330 dropped ticks), and **every pool exit
was lost** — 9 completed curves were observed but none produced a pool row.
The apparent graduation drought (0.1% vs the tape's 3.3%) is therefore
confounded: the instrument, not just the regime, changed. Fixed with per-coin
batch grouping plus a batch-layout regression in `--self-check`; the
graduation-rate verdict restarts from the fix.


## Protocol-derivation discipline (2026-10-06, from the letsbonk pool bug)

Issue #214's pool lookup failed for five consecutive bonk coins, and a
one-window reproduction "proved" a reversed seed order. It was wrong: a
32-byte window that reproduces a PDA can be a field-boundary coincidence
(the pool's own base_mint/quote_vault fields, derived through the IDL's
layout, were the decisive cross-checks). The arbiter for any PDA-seed
hypothesis is the IDL's own field layout — for LaunchLab PoolState: 8 disc +
8 epoch + four u8 + seven u64 + a 40-byte VestingSchedule + two config
pubkeys put base_mint at 205 and quote_mint at 237. The original
`[b"pool", base, quote]` order was correct; the example failed because the
coin has no WSOL pool. A wrong "fix" that is verified against a coincidental
match looks green — the swapped-order rejection in
`letsbonk-buy-sell/verify_pool_derivation.py` is the tripwire for that.

## Forward-test interim (2026-10-06, ~14h)

Post-instrument-fix, the collector sees completions correctly (14 observed,
max real_quote exactly 85.0 SOL) but the graduation rate is ~0.14% of
WSOL-paired coins versus the Sep 4-5 tape's 3.3% — roughly 20x quieter.
Neither the whale-ride's pooled verdict nor a regime conclusion is possible
at this rate; the collector keeps running. The tape's day may simply have
been an exceptional mayhem burst.
Separately verified live: ~37% of current PumpPortal launches pair against
quote mints that are neither WSOL nor USDC (e.g. `XsoCS1Tf…`, `A7bdiYdS…` —
distinct tokens per coin, confirmed by reading their BondingCurve accounts).
The WSOL-only whale-ride cohort is therefore smaller than the tape's coin
population; rate comparisons against the tape should use WSOL-paired
denominators on both sides.

## Four queued hypothesis censuses completed (2026-10-14)

Closed all four queued censuses in `docs/STRATEGIES.md` against the 24,015-coin exact-CPMM tape (`lifecycles_24h.jsonl`) with strict TRAIN (`0..12,006`) → LOCK (`12,007..24,014`) out-of-sample evaluation:

1. **Leaderboard winner census (top-100 realized-cashflow winners, `≥10` coins, unsold bags = 0 SOL)**:
   - `31/100` (`+1,662.8 SOL`) are slot-0 bundled insiders (`med_dslot = 0.0`); `1/100` (`+19.4 SOL`) is a `dslot 1..2` sniper; `15/100` (`+292.2 SOL`) scalp Mayhem `sol_vault` (`BwWK17cb…`) automated buybacks (`+2.99%` LOCK median, `−4.16%` LOCK mean); `53/100` (`+460.3 SOL`) trade non-Mayhem mid-curve clusters (`med_dslot = 41.5`, `med_rsol = 15.29 SOL`).
   - Selecting the 161 profitable non-Mayhem retail-latency wallets strictly on TRAIN and following `≥2` wallets buying within 3 slots on LOCK (`n=209`) fails out-of-sample: **`29.7%` win rate, `−4.33%` mean, `−10.67%` median**.
2. **Creation-window re-test by regime (`dslot 1..3`, `4..10`, `11..25` at `+15` / `+50` / `+150` slots)**:
   - Non-Mayhem (`14,950` coins) is negative across every cell (`−8.08%` to `−19.45%` mean, `−3.20%` to `−64.56%` median). Mayhem (`9,065` coins) is deeper negative (`−12.26%` to `−47.80%` mean, `−16.00%` to `−88.52%` median). Creation-window sniping at `dslot >= 1` is dead in both regimes.
3. **Buyback fee stream (`BUYBACK_FEE_RECIPIENTS`)**:
   - Protocol-owned (`Global.authority`), not creator-accessible, and 0 curve trades on the 24h tape.
4. **Serial-sniper clone (`50` wallets with `≥10` coins bought and `0` curve sells)**:
   - Either slot-0 graduation bundlers (`100%` grad rate at `85 SOL`, `med_dslot = 0`) or multi-wallet token routers (`756..779` coins, `5.4%..7.3%` grad rate). Following the top 8 non-slot-0 never-sellers loses in every cell (`−1.87%` to `−44.80%` mean, `−3.19%` to `−84.62%` median).

## Waves 5–9: Executable Crossing Funnel, Organic Trigger Guards, Flow Exits & Rollover Stabilization (2026-11-02)

1. **Organic Buy Trigger Guard & Buyer Cohort Discrimination (Waves 5, 7 & 9)**:
   - Auditing `lessons.sqlite3` ($n=2,280$ `buyers_present` passes) surfaced 6 historical gate passes where `EntryGate.observe` accepted on a `MAYHEM_SOL_VAULT` (`BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s`) trade after earlier human buyers had already satisfied `min_buyers`. In `learning.report`'s `paper_by_buyer_cohort`, `mayhem_vault_buyer` entries had **`0%` win rate ($0/4$)**, **`-86.66%` mean 60s return**, and **`-83.20%` policy return**, whereas `organic_repeat_buyer` entries yielded **`81.8%` win rate ($18/22$)**, **`+80.43%` mean 60s**, and **`+72.04%` policy return**.
   - `EntryGate.observe` now requires an organic non-creator buy to fire accept (blocking sell events `is_buy=False`, `MAYHEM_SOL_VAULT`, and creator self-buys), paired with `max_single_buy_sol: 45.0`, `max_buyer_share: 0.75`, `max_creator_launches: 1`, and `max_inflow_sol_per_slot`.

2. **Flow Rules Hardening (Wave 5)**:
   - Holding coins through prolonged curve stalls or immediate post-entry dumps degraded returns. `FlowRules` now enforces:
     - `max_stall_slots: 35` (`velocity_stall`): exits when curve peak real SOL fails to advance for 35 slots.
     - `entry_floor_pct: 0.15` (`entry_floor`): stops out if real SOL drops $\ge 15\%$ below entry SOL before establishing a higher peak.

3. **Three-Tier $G+5\text{s}$ Stabilized Rollover Exit (Wave 6)**:
   - Across 91 graduated coins in `grad_marks`, 16 suffered early whipsaw stop-outs ($\le 10\text{s}$ below open) before rebounding above open at 30s/120s.
   - Replacing the immediate flat trailing stop with a three-tier stabilized schedule (`0.85 * open` for $0\text{--}5\text{s} \to 0.90 * \text{open}$ for $5\text{--}10\text{s} \to 5\%$ trailing drop off peak) absorbs initial migration sell-pressure, lifting `p5`-confirmed shakeout returns from **`-1.11%`** to **`+1.49%`** vs pool open.

4. **4-Stage Executable Entry Funnel & 5.3-Day Latency Shadow Corroboration (Waves 8–9)**:
   - Progressive filtration in `learning.report` (`executable_funnel`) and `dashboard.py` separates unexecutable Poll-1 slot-0 completion bundles from fillable post-slot entries:
     - Stage 1 (`unexecutable_poll1_bundle`, $n=73$ planned, $72$ observed): $70.8\%$ win, $+1,949.62\%$ 60s, $+1,937.43\%$ policy (unfillable slot-0 completions).
     - Stage 2 (`executable_curve_all`, $n=56$ planned, $41$ observed): $51.2\%$ win, $-10.45\%$ 60s, $+5.58\%$ policy.
     - Stage 3 (`executable_in_band_post_slot`, $n=3$ planned, $3$ observed): $100\%$ win, $+22.32\%$ 60s, $+37.11\%$ policy.
     - Stage 4 (`executable_full_gate`, $n=3$ planned, $3$ observed — `SPOOF` + `Mask` + `KIMVERSE`, matching `max_single_buy_sol: 45.0` and non-vault trigger): **`3/3` (`100%`) win**, **`+0.2232` (`+22.32%`) 60s mean**, **`+0.3711` (`+37.11%`) policy gross mean**.
   - Independently corroborated by the 5.3-day `qd1946_tests` live latency shadow ($n=116$ signals): `92%–96%` of slot-0 signals ($\text{age} < 0.4\text{s}$) were infeasible (curve already completed at $85\text{ SOL}$), while the `52` feasible post-slot signals at $L=0.4\text{s}$ achieved **`75.9%` graduation hit rate** and **`+53.9%` median net ROI**.

## Wave 10: Exit-Grid Calibration (`60..80 SOL`), Pre-Fill Slot Guard & Keeper Supervisor Resilience (2026-11-02)

1. **Exit-Grid Calibration on the `60..80 SOL` Band (`whaleride-paper.yaml`)**:
   - On the `lifecycles_24h` `60..80 SOL` crossing cohort, `max_stall_slots: 35` prematurely stopped out **`73.0%` of eventually graduating curves** (median pre-graduation stall gap = `74` slots, `p75 = 107` slots), dragging out-of-sample LOCK mean return to **`-7.2%`**. Likewise, `max_hold_time: 60` cut **`31.7%` of `qd1946_tests` graduating winners** that graduated after `60s` (including `71452 SPOOF` at `62.74s`).
   - Calibrating `trade.max_hold_time: 90` (capturing `80.5%` of `qd1946_tests` graduating winners) and `flow_exit` to `max_stall_slots: 180`, `entry_floor_pct: 0.10`, and `single_sell_pct: 0.25` turns the `lifecycles_24h` LOCK cohort positive at **`+6.0%..+6.8%` LOCK mean (`67.6%..70.6%` LOCK win rate)**.

2. **`entry_gate` $\leftrightarrow$ `flow_exit` Pre-Fill Slot Guard & Queue Resubscription**:
   - Pre-entry trade events buffered in `TradeQueue` prior to fill slot could leak into `FlowPosition` evaluation or leave the queue unsubscribed if `TradeHub` overflowed during gate evaluation.
   - `EntryGate` now records and propagates `entry_slot` into `FlowPosition`, `_consume_trade_flow` discards events with `ev.slot < entry_slot`, and `UniversalTrader` resubscribes `trade_hub` if the shared queue overflowed (`q.overflowed`), ensuring continuous post-fill flow monitoring without false pre-entry triggers.

3. **Keeper Supervisor Startup Grace & Uptime Budget Reset (`keep_whaleride_shadow.py`)**:
   - Newly spawned children before their first row flush or long-lived keepers accumulating isolated transient restarts over multi-day runs could trip false stale kills or exhaust `MAX_RESTARTS`.
   - Stale-output detection now gates on `elapsed > STALE_SECONDS` since child spawn, and `restart_count` resets to `0` after `HEALTHY_RESET_SECONDS = 3600.0` of healthy uptime.

## Wave 13: exit-counterfactual validation, cost audit, reclaim instrumentation (2026-11-03)

1. **Exit-stack counterfactual replay (5.3-day `qd1946` shadow, 54 feasible signals at `L=0.4s`)**:
   the calibrated exit stack preserves all 41 winners — 22 identical exit points,
   3 improved, 8 censored-with-positive-floors — and is provably non-harmful on 9 of 13
   catastrophic losers (cannot lose more than the recorded `-89%` at the 90s cap).
   Rescue magnitude stays honestly censored: curve tapes were never persisted.
2. **The ONE winner-cutting channel**: `max_hold_time: 90` cuts 8 of 41 graduations
   that graduate at `92–332s` (median `+61%` ROI forfeited). Kept at 90s because the
   broader 24h-tape grid prefers it; flagged for forward arbitration by `paper_flow_marks`.
3. **Flash-dump entries** make `single_sell_pct: 0.25` a near-zero absolute threshold —
   unverifiable offline, measurable forward.
4. **Cost audit**: ~3,540 RPC/h in dry-run was 100% unconsumed fee polling
   (0 accepts). Cut 4x to ~900/h via `fee_schedule.poll_interval_seconds: 8.0`
   (8s stays inside the 10s `max_observation_age` freshness bound).
   Re-attestation was observed at +60/+120s and then absent — flag to re-check.
5. **`buy_amount` 0.01 → 0.05**: at 0.01 SOL the fixed-fee drag (200k-lamport priority
   fee) is 4.1% round-trip vs 0.82% at 0.05. Marks are marginal-price-based, so
   recorded ROI is unchanged; the sizing is cost-realistic.
6. **Reclaim arm instrumented** (`reclaim_marks` table): forward-tests the only
   measured-positive OOS strategy — post-graduation pool reclaim at
   `+5.3%` TRAIN / `+1.68%..+2.15%` LOCK. Frozen parameters:
   pullback `0.10–0.35` below open, reclaim `+5%`, `min_quote_sol 55`,
   trail `10%`, target `15%`, `max_hold 180s`, trigger window `300s`.

## Wave 14: hold-cap resolution, true fee cost, reclaim divergence correction (2026-11-03)

(a) **Hold-cap**: the old grid's slot<->second mapping was mis-calibrated
   (tape `0.315 s/slot` vs `0.6` assumed); live `90s` sat at the worst point
   of the LOCK curve. Late grads (`>90s`) are winner-heavy — `12/12` on the
   24h tape, `8/8` pattern on live. `max_hold_time: 90 -> 180` recorded
   (live `+6.8..+9.0pp/entry` vs the 24h-tape's `-4.4pp` on an n=15
   negative-EV band; reversible knob, `paper_flow_marks` arbitrate).
(b) **True fee cost**: the original `3,540/h` estimate counted only polls
   (~19% of true cost); attestation sweeps (~`122 RPC` / ~`63s`) are ~81%
   (~`13,900/h` at an 8s poll). `reattest_after_seconds` /
   `max_attestation_age_seconds` are now a paired config
   (`300s`/`600s` -> ~`4,500/h` steady state).
(c) **Reclaim divergence correction**: the documented `+5.3%` TR /
   `+1.98%` LK organic baseline came from an ad-hoc no-lag config; the
   committed `simulate_reclaim` (+1-slot lags) produces **`+2.58%` TR /
   `+0.40%` LK** net; the faithful 2s-poll live-arm emulation delivers
   **`+3.30%` TR / `-5.60%` LK** (LOCK inverts) with **27%** structural
   silent misses (dip+reclaim inside one 2s gap) that are adverse-selected
   (missed coins' trade-cadence entries ran `+32%` mean r60).
   Mitigation shipped: a `0.5s` fast-poll window over the first `120s`
   post-open. Honest status: thin edge, live-arm adversely selected at the
   2s cadence; forward `reclaim_marks` arbitrate at the fixed cadence.

## Wave 15: LetsBonk lifecycle capture and the venue verdict (2026-11-03)

`run_letsbonk_lifecycle_capture.py` ran a clean 12h mainnet soak (578 coins,
32,736 trades, 7 graduations: 4 fund-target, 3 migrate-instruction; zero
unbounded failures; 122 bounded slot-gap journal pairs). Findings from the
tape, all recorded under the standard evidence rules:

(a) **The B1 decoder-wiring verdict is NO.** On the 329 WSOL-quoted coins
    (the unit-clean sample), the full 31,104-policy grid is negative
    everywhere — best policy `~-0.0019` SOL over 6 qualifying entries, 0%
    win rate, and no marginal knob (entry latency, stops, holds, gates)
    moves it — before applying the measured `-8..-11%` letsbonk round-trip
    spread. No hold horizon clears the spread; the LaunchLab decoder stays
    unwired in the bot's listener, per the capture-first gate.
(b) **43% of letsbonk launches are not WSOL-quoted.** Multi-asset pools
    (`6GmAFSYs...`, `WXMRyRZhsa...`, USDC et al.) dominate the overnight
    wave; amounts and `fund_target` live in each quote mint's raw units.
    All 171 WSOL coins decode with `fund_target` exactly `85 SOL`, and the
    fund-target graduation fires exactly at the crossing (verified at
    `85.000000447 SOL`), so raw-vs-raw detection stays valid on every
    quote asset — only SOL-denominated summaries must filter
    `quote_mint == WSOL`.
(c) **Creator behavior inverts vs pump.fun.** Only 10/226 traded WSOL coins
    have a dev buy (pump: near-universal), while 97/226 creators sold
    (median first sell at `0.83 SOL` of reserves). The pump-derived
    dev-buy-anchored gate/policy families barely fire on this venue.
(d) **Median coin peaks at `4.86 SOL` of reserves — 5.7% of the 85 SOL
    graduation target** (p90 `37 SOL`, max `129 SOL`; 1 graduation in 329
    coins, `0.3%`). Final/peak median `0.00`; launch volume is bursty
    (456 of the 578 coins arrived inside one ~2.5h campaign window).
(e) **Summarizer portability fixes** (found by the real tape, committed):
    null-`mayhem` counting, `None`-tolerant `q`/`dist`, and
    `dev_buy_sol` None-guards — pump-tape regression unchanged
    (`mayhem=9065`).

Venue-lane conclusion: letsbonk at retail latency joins cross-chain and
raw-AMM minute-0 as measured-negative. The council plan's top workstream
(wallet-intelligence + executable-net-SOL modeling on the pump tapes) is
unaffected.

## Wave A1: wallet-PnL persistence and delayed-follow falsification (2026-11-03)

`analyze_wallet_pnl.py` ran the council's top-ranked lane on the 24h tape
(1.9M curve trade events, 100,175 wallets, average-cost realized matching;
system program and the mayhem vault excluded).

(a) **Profits are real and concentrated**: top-10 wallets earn 17.0% and
    top-100 earn 30.4% of all positive realized curve PnL.
(b) **Persistence is real but modest**: Spearman +0.184 across day halves;
    top-decile half-1 wallets capture 30.5% of half-2 positive PnL
    (walk-forward, threshold from half-1 only).
(c) **Delayed-follow does not extract it**: every (delay 1-8 slots x hold
    10-60 slots x winner-threshold) cell is negative net; gross-of-fee EV
    ranges -0.00004 (delay 4-8, hold 10, top-decile winners) to -0.00265
    (delay 1, hold 60, median winners) on 0.05 SOL trades, vs the all-buys
    baseline gross -0.00069 at (2,25). Winner-wallet buys are not followed
    by positive drift at retail delay — their flow marks local tops, which
    is consistent with winners selling into follower demand rather than
    their entries signaling upside.
(d) **Entry-side wallet-following is closed at these parameters** (single
    tape; >=3 non-contiguous tapes rule blocks any reversal claim). The
    wallet signal's remaining testable value is exit-side: winner SELLS as
    dump timers (the sniper-exit baseline of A2) and funder/buyer-graph
    clustering (argon's lane) to characterize how the top-100 extract,
    rather than following their entries.

## Wave A2: entry-feature harness — stage-1 verdict, three tape traps (2026-11-03)

`analyze_entry_features.py` priced 1.84M warm decision points on the 24h
tape with executable net-SOL labels (entry at next trade >= signal+2 slots,
one position per coin, both-side pump fees, dead coins marked at their last
observed trade). The run surfaced three method traps now handled in the
harness, then closed the lane:

(a) **"Missing exit = no-trade" is survivorship, not conservatism.** Coins
    that die inside the horizon are mark-to-last losses; excluding them
    inflated hold-10 means by ~+17%/trade gross.
(b) **Means on this tape are dust-tail contaminated.** Hold-10 gross showed
    mean +4.5% vs median -0.43% with a max single-window return of
    +3.1e6% — dust-trade implied prices that 0.05 SOL could never fill.
    The harness now reports medians and winsorized means; raw means are
    not citable on this venue's marks.
(c) **Stage-1 verdict: no entry feature beats cash on robust statistics.**
    Every baseline (always-up, momentum, reversal) is negative at every
    hold; every feature-decile curve is negative in every decile
    (n_unique_buyers, buy_sol_total, max_single_buy, price_change_pct,
    inflow_last5, n_trades). The pre-declared best cell — prior pump
    >= 3.6x — merely loses *less* (median -0.0012 vs -0.0120 SOL on 0.05
    SOL trades) and its coin-clustered bootstrap CI excludes zero on the
    downside; the typical coin in the best cell loses -1.1% per trade.
(d) **Stage 2 (gradient-boosted model) is not justified**: no single
    feature separates toward positive EV, and a model cannot combine
    universally negative-conditional returns into positive EV. The GBT
    dependency is not installed.

Curve-phase entry modeling joins entry-following (A1) as measured-negative.
The live A-lanes are now exit-side: A4's sniper-exit/winner-sell timing and
A3's post-washout delayed entry on the 4.95M-trade post-graduation tape —
the last untested home of the direction-model idea.

## Wave A4: winner-sell is a strong, creator-independent exit timer (2026-11-03)

`analyze_exit_signals.py` measured forward price paths after half-2 trades,
grouped by the trader's strictly-prior realized PnL (walk-forward, top-decile
of positive half-1 PnL, threshold ~2 SOL). The event study found the program's
first positive result:

(a) **Winner sells time the dumps.** After a winner-wallet SELL: median
    forward move `-13.5%` / `-21.8%` / `-36.9%` at 10/25/60 slots
    (n=191,503, coin-clustered bootstrap CIs entirely negative:
    `[-8.8%,-7.7%]`, `[-8.3%,-6.9%]`, `[-10.7%,-8.5%]` on the median).
    Ordinary sells: `-0.7%` / `-1.2%` / `-2.4%`. The gap is ~12-15x at every
    horizon.
(b) **It survives creator exclusion.** With every creator event removed,
    winner-sells still precede `-14.5%` / `-23.1%` / `-38.6%` medians
    (n=182,049) vs `-0.7%` / `-1.2%` / `-2.5%` for other sells — so this is
    NOT the bot's existing `creator_sell` flow-exit rule in disguise; it is
    an independent and much stronger exit signal.
(c) **Winner buys remain non-actionable at retail delay** (forward medians
    +2.3% at 10 slots then negative), consistent with A1: winners buy early,
    followers cannot.
(d) **Deployable shape**: an exit overlay on held positions — "exit when a
    top-decile-prior-PnL wallet sells into your position" — needs only a
    running realized-PnL map over observed flow, no new infrastructure.
    It composes with (does not replace) `creator_sell`.

Honest limits: single tape (the multi-tape rule blocks live activation;
the overlay is a config-gated paper-mode candidate until >=3 non-contiguous
tapes confirm), marks not quotes, and the threshold is venue/day-specific.
Next: validate on the letsbonk WSOL tape as tape #2 (same harness, data on
disk), and spec the `winner_sell` flow-exit rule for the paper profile.

## Wave A3: post-washout entries — positive but censor-fragile (2026-11-03)

`analyze_postgrad_washout.py` priced 30-minute-post-graduation entries on the
4.95M-trade post-grad tape (812 pools with pool tape; 269 dead before the
washout — entry impossible, excluded per the astra rule). One measurable
window on this capture: 30-min washout + 60-min hold.

(a) **The deployable cell is marginally positive at the median.** The
    liquidity-retention rule (pool quote reserves >= 50% of opening at
    entry, observable pre-entry): median `+0.0007` SOL on 0.05 SOL trades
    (+1.4%/trade), 53% win, n=341 — with pools that go quiet marked at
    their last trade. Winsorized mean `-0.008` (fat left tail: quiet-pool
    marks). Bled-liquidity pools: median `-0.0014`, 9% win. The retention
    feature splits the cohort exactly as the council predicted.
(b) **The fully-covered subset is strong but survivor-conditioned:**
    pools whose recording covers the whole [washout, washout+60] window
    show `+0.0032` median (+6.5%), 67% win, n=256, with exits 100%
    observable. Conditioning on 90-minute post-grad survival is not a
    deployable entry condition; it is evidence that active post-washout
    pools drift up.
(c) **The 2h/4h horizons are unmeasurable on this tape**: per-pool
    coverage is p50 46 min, hard-capped ~120 min (this capture's postgrad
    bounds). Holds beyond ~90 min have no denominator.
(d) **Recorder-bounds lesson**: the post-grad horizons a tape can price are
    set by `--postgrad-seconds` at capture time. The running tape #2 uses
    the 10-min default (sized for A4's curve-phase validation) and cannot
    validate A3; a dedicated 4h-postgrad capture is the A3 follow-up.

Composability note: A3's retained-liquidity entry + A4's winner-sell exit
overlay is the coherent candidate pair this program has produced — still
single-tape, both halves pending multi-tape confirmation.

## Wave N1: Novig public-data lane — structure real, edge not deployable (2026-11-04)

Ten trade days (2026-10-23..11-01, pulled from the free Novig exchange-data
CDN; 8-12k STRAIGHT markets/day) plus the 91-day markets.csv OHLC tape.

(a) **The longshot-bias structure is day-invariant**: 10/10 days show
    longshot (<30c open) day-open->day-close mean +7.2..+14.1c/contract
    with negative medians, and favorites (>=70c) mean -15.3..-22.2c.
    The sign asymmetry is a stable venue property, not noise.
(b) **The apparent edge is a close-mark artifact.** Resolution truth from
    finalized markets (68,475 STRAIGHT <30c opens, final close >= 95c as
    the win proxy): actual win rates are 0.9-6.7% by type — NCAAF-SPREAD
    6.7%, MLB-SPREAD 2.7%, NFL-TOUCHDOWNS 2.5% — versus the 29-44%
    "close-hit" the same-day close-marks suggested. In-game day-closes
    >=50c usually resolve to zero. Hold-to-resolution mean EV is
    +0.3..+9.4c/contract pre-fee on 12-21c entries: a 93-97%-loss lottery
    whose mean is entirely tail-carried.
(c) **No deployable strategy from public data alone**: cohort harvesting
    is median-negative at trade granularity; resolution-hold is a lottery
    profile unvalidated for fills; the strongest cells (NCAAF|SPREAD,
    MLB|SPREAD) are thin and single-tape-class. The A2 lessons (tail
    means, mark realism) transferred and closed this lane quickly.

Program note: the polymarket-alpha-bot repo carries the two experiment
scripts (`novig_public_ohlc_study.py`, `novig_trade_longshot.py`) and the
live-verified venue clients (Novig/ProphetX/Kalshi, cross-venue modules);
PR #2 merged with the full continuation 2026-11-04.

## Wave A4+: multi-tape confirmation and paper activation (2026-11-04)

Tape #2 (fresh capture, non-contiguous with tape #1; 15,961 coins, 820k
curve trades; slot stream lost at ~10.5h — the recorder's designed
no-coverage exit with 15 bounded reconnects and 2,069 gap records; every
tracked coin force-flushed, 1.4GB lifecycles intact) confirms the A4
winner-sell signal at full strength:

- winner-sell forward medians `-12.3% / -16.7% / -26.2%` at 10/25/60
  slots (n=119,890; coin-clustered CIs `[-11.4,-10.0]`, `[-8.8,-7.6]`,
  `[-10.2,-8.0]` — all entirely negative) vs other-sell
  `-0.9% / -1.5% / -2.7%`. Tape #1: `-13.5% / -21.8% / -36.9%`
  (n=191,503). Same ~10x separation on both days across volume regimes
  and thresholds.
- Fixed-threshold sweep (1..10 SOL, both tapes, all cells negative,
  monotone in threshold): a quantile-free config value is satisfiable;
  2 SOL selected for the paper profile.

**Activated in paper mode** (the >=3-tape rule gates live money; paper
mode is the forward-measurement phase): `flow_exit.winner_sell: true`,
`flow_exit.winner_sell_min_realized_sol: 2.0` in whaleride-paper.yaml
(config validated); collector reloaded via the keeper procedure at
2026-11-04T11:40:27Z (kill -> 0 positions -> 0 processes -> respawn ->
single writer). Forward expectation: when a wallet with >=2 SOL strictly-
prior realized profit sells into a held position, the position exits
immediately instead of riding the measured -13..-26% forward damage.
`paper_marks`/`grad_marks` arbitrate the effect against the pre-activation
cohorts.

## Wave A4++: third tape confirms; AMM lane void on tapes 2-3 (2026-11-05)

> CORRECTED in Wave F1 below: the post-grad void was truncated migrate_v2
> logs in the recorder, not provider lane starvation.

Tape #3 (13,365 coins flushed when the 40GB raw ceiling hit at ~7h;
9.99M raw transactions) confirms the A4 winner-sell signal a third
non-contiguous time: forward medians `-7.1% / -10.7% / -19.0%` at
10/25/60 slots (n=100,538; coin-clustered CIs `[-6.6,-5.5]`, `[-7.1,-5.9]`,
`[-9.5,-7.6]` — all negative) vs other-sell
`-1.1% / -1.8% / -2.9%`. **The >=3-tape requirement for the winner-sell
signal is satisfied**; the remaining live gate is the forward paper effect.

**The AMM lane was silently void on BOTH monster tapes.** Tape #3's raw
stream contains zero pump-amm transactions (whole-file grep; the lane was
dead from minute zero, not degraded late), and tape #2 has zero
pool_trade/graduated records for the same reason. This is a capture
defect, NOT an on-chain graduation collapse: pump-amm runs ~28k tx/h
on-chain, and a live probe with the identical two-entry subscription
delivers ~20.5k amm txs in 90s while the collector streams. Recorder code
is unchanged across all three tapes (subscription() byte-identical
since the original research commit). Verdict: provider-side silent lane
starvation during the two high-strain capture windows (correlated slot
gaps/reconnect storms; the probe on the same endpoint works fine now).
Consequence: **A3's 2h post-washout horizon remains unmeasured** —
tapes 2/3 have no post-grad data; tape #1's ~46min coverage p50 stands
as the only post-grad measurement.

Recorder fix (`record_lifecycles.py`): `amm_transactions` counter on
every deduped transaction + `lane_void` journal record (once, when
>10min elapsed, >1000 transactions, zero AMM deliveries) + `amm=` in
the console heartbeat; offline self-check covers both. A dead AMM lane
now announces itself at minute 10 instead of silently voiding 7h of
post-grad data.

Capture-sizing lessons (pump-only recording regime):
- raw retention ~5.7GB/h pump-only; with the AMM lane alive expect
  ~2.5GB/h more. Tape #3 hit BOTH ceilings nearly simultaneously
  (9.99M events ~= 10M cap; 37GB raw ~= 40GB cap).
- Provider strain pattern: two consecutive 7-12h captures degraded
  (slot silence at 10.5h; disk ceiling at 7h with 1,209 gaps/8
  reconnects; collector hiccup windows correlated 15:15/16:28/18:20/
  19:00/22:05). A 6h capture is the conservative ceiling under this
  regime; the lane_void assert makes longer gambles cheap to detect.

## Wave F1: fidelity fixes — deployed ledger, quote mixing, truncated logs (2026-11-05)

**Gate drought is not a bug.** Replaying the live `entry_gate` over tapes
2-3: 0 passes in 24,457 verified SOL coins (the three replay "passes" were
all non-SOL-quoted coins the bot correctly never sees); live: 0 in ~75k
decisions since hardening. In the current regime coins reaching 60-80 SOL
inside 75 slots arrive with 4+ buyers (32 near-misses) or overshoot with
1-2 (171 `too_much_sol`). The paper book therefore cannot arbitrate
winner_sell forward; offline replay through the production `FlowMonitor`
is the arbitration path until an entry rule admits coins.

**~18% of pump.fun launches now quote in PUMP (`pumpCm…`) or memecoins.**
The bot rejects them at parse by design ("unsupported quote mint"); the tape
analyzers did not, and treated their raw quote amounts as lamports (tape-2
A4 winner threshold inflated 12.4 → 2.4 SOL). All pump analyzers now keep
SOL-paired coins only. A4's ledger also walked coins in flush order (up to
~20 min of future PnL in "prior"); it now walks global slot order. Corrected
A4, 10/25/60-slot medians: tape 1 `-13.4/-21.6/-36.8%`, tape 2
`-10.1/-14.1/-23.0%`, tape 3 `-7.3/-11.0/-19.4%` vs other sells
`-0.7..-2.9%` — the signal is unchanged by both corrections.

**The deployed rule implemented ~1% of the validated signal.** The hub
ledger decoded only subscribed mints (gate windows): replayed on tapes
2-3 it saw 1.7-2.1% of SOL trades, 16-18 wallets ever cleared 2 SOL (tape:
~1,200), and live-winner sells were n=458/1,275 vs 137k/97k tape-winner
sells. `TradeFlowHub(track_wallets=True)` (on when `flow_exit.winner_sell`)
now decodes every pump.fun trade for an LRU-bounded ledger (100k wallets,
100k positions) and still delivers only subscribed mints.

**Incremental exit value, production FlowMonitor replay** (3 tapes, later
half of coins, 10 entries/coin, fill one trade after the signal, 225-slot
cap, net SOL per 0.05 SOL): adding winner_sell to the deployed
`flow_exit` moves the median from `-0.00335/-0.00402/-0.00461` to
`-0.00225/-0.00269/-0.00312` (+2.2..3.0% of the position), win-mean
+0.0012..+0.0018, and improves 71-76% of the outcomes it changes; it
becomes the first trigger in ~60% of exits. Better exits, not an edge:
every cell stays negative because the entries carry none.

**Winner-sell as an entry veto: closed negative.** "No winner sold in the
last 10 trades" has better medians but a 16-19% win rate (quiet coins
bleeding fees); no hold cell is positive on any tape.

**Solana truncates logs at 10 KB, and every migrate_v2 overflows it**
(create_pool + init_boost + migration event: 155 lines, 10,010 bytes). The
recorder refused the truncated stack and dropped the CreatePoolEvent, so no
pool was ever mapped: tapes 2/3/4 graduated 209/120/55 coins, all with no
pool and no post-grad trades, while tape 1 (pre-migrate_v2) mapped 794 of
797. Wave A4++'s "provider lane starvation" was wrong: its whole-file grep
for the AMM program id cannot match inside zlib+base64 protobuf payloads.
Fix: on a truncated stack in a successful transaction, decode the
program's own event-CPI copies (instruction data, never truncated; only
the program signs its event authority). The recorder (`event_source`,
`event_cpi_fallbacks`) and the src trade/LaunchLab/migration decoders both
use it — in tape 4, 64 creation transactions of SOL coins (~2.7%) were
truncated create+buy bundles whose buys the gate never saw. Tape 5 (fixed
recorder, 10h, 4.5h post-grad windows) logged 658 pool trades after its
first 2 graduations.

Housekeeping: `read_error` paper marks are all Oct 6-7 (before the
completed-curve fallback); `trade_stream_interrupted` rows are provider
hiccup windows, fail-closed by design; per-event CreateEvent parse chatter
(54% of log bytes; rotation held ~7h) moved to DEBUG. Four 2026-10-28
Cursor PRs (#2-#5, an offline meme-studio line) were consolidated and fixed
as PR #6 (merged).

## Wave C1: what long-lived community coins look like (2026-11-05)

Outcome, read from our node per SOL-paired coin of tapes 2-4 (27,553 coins,
3-33 h old): **alive** = graduated, pool traded in the last hour, pool still
>= 10 SOL. Only 0.18% of launches (50 coins) and 8.5% of graduates qualify.
Survival odds, not trade returns.

- **Launch score** (all known within ~60 s): first launch by this creator;
  creator did not sell in the first minute; dev buy > 2 SOL; > 25 first-minute
  buyers with no wallet above 50% of that buying; not mayhem. Score >= 4 is
  ~6% of launches but holds 72-84% of alive coins on each tape (alive 2.2-2.4%
  vs 0.18%); tape 4 was held out. Score 0-2 (two thirds of launches): 1 alive
  in 18,053.
- Serial creators: 2 alive in 14,891. Creator sold within 60 s: 0 in 9,482.
  Mayhem: 0 in 10,351.
- **Graduation timing** (known at graduation): same-slot bundled graduation
  12.7% alive (n=118), 1-10 min after launch **2.6%** (n=191), > 10 min 13.6%
  (n=59). Fast frenzy graduations almost never last.
- **X, posts carrying the contract address** (APInex search, 400-coin
  stratified sample, weighted back to all 603 graduates; base 8.3% alive):
  >= 5 distinct authors in the first 2 h -> 24.9% alive (3.0x, 15% of
  graduates); a 10k+ follower account posted -> 26.6% (3.2x); no posts ->
  4.7% (0.6x). Strictly before an entry 30 min after graduation, >= 3
  authors -> ~21% (small n). Cashtag/name searches collide with other assets;
  link searches add nothing. Posting accounts are a median 3.5 years old
  (< 1% under 30 days), so account age does not separate.
- **LunarCrush**: contract addresses are not indexed; cashtags are, but 76%
  of tickers were active before launch. Excess distinct creators in the 6 h
  after launch over the ticker's own 72 h baseline: alive median 6.8 (61% >=
  5) vs graduated-then-died 0.7 (24%). Hourly data: an hours-scale signal.
  X >= 5 authors AND LunarCrush >= 5 -> 30% alive (3.6x, 10.5% of graduates).
- Metadata (59% fetched; public gateways throttle): linking someone else's
  tweet (76% of coins) does worst; an X account shared by 2-20 coins beats a
  unique one; descriptions > 120 chars 2.8x. Suggestive only (48 alive).

Infrastructure lessons:
- The recorder checked `max_age` before the post-graduation branch, so every
  graduated coin was flushed 20 min after launch: tape 5 asked for 4.5 h of
  pool trades and got a median of 14 min. Fixed (graduated coins follow only
  the post-graduation limits); tape 6 is the first capture that can price
  multi-hour post-graduation entries.
- `.state/configs/capital-research-rpc.env` holds the PUBLIC Solana RPC, not
  our node; research reads should use the Chainstack URL from Secret Manager.
- APInex's subscription tokens are a model allowance: Twitter tools bill only
  the USD balance (`402` otherwise). Search costs $0.0006 per tweet; the
  400-coin study cost $1.39.
- Provider secrets live in GCP Secret Manager (`ultimate-model-ai`, label
  `app=pumpfun-bonkfun-bot`); local `.state/configs` projections remain for
  running services. The root `.env` family and wallet secrets stay local.

Next: run the pre-declared post-graduation entry test (graduation timing, X
authors before entry, LunarCrush, liquidity retention) on tape 6 when it
closes, and re-label tape-5 coins for an out-of-sample survival check.

## Wave C2: council review of the "safe passage" radar (2026-11-05)

Owner vision: detect hype before the contract address is posted, monitor
promoters, score real-vs-bot interest and rug risk, keep influencer and
wallet intelligence (money flow, bundles, LPs, Jito), and reverse-engineer
manipulators to ride their pumps safely with recoup-then-ladder exits.
Packet and all three reviews: `docs/council/2026-11-05-safe-passage/`.

New evidence for the packet:
- The first X contract-address post usually follows the on-chain move: for
  graduates that later died, 56% after graduation and 83% after the curve's
  price peak (alive coins 24% / 56%). 45% of 316 posting accounts posted >= 2
  coins; 56 posted >= 5.
- Coins' own X accounts before launch: only 6/50 alive coins link one;
  25-27% of dead/non-graduated coins named the coin before launch.
- Wallet radar first cut (11,315 coins, strictly-prior classes, > 50% of
  first-minute SOL): serial snipers 0.5x survival, never-seen wallets 1.7x,
  creation-slot bundles 0.4x (1.4x graduation); >= 10 identical-size buys
  5.5x (terminal presets or volume bots: unresolved).
- Wash check of the 50 alive pools: median 0% one-tx round trips, but 10/50
  have < 10 distinct real traders; ~40/50 are genuinely alive.
- External: Bitquery (8.2M launches) dev bundles in block 0 on 21% (median
  10% of supply), devs sold within 1 min on 65%, manufactured graduations
  return >= 90% to their operators; 58% of Solana DEX volume not real (mostly
  one-tx two-signer round trips); KOL calls +1.83% day 1 then -6.53% by day 30.

Council (astra, fable, argon) - unanimous:
- Speed entries (pre-CA watchlist, ShredStream latency race, curve-stage
  "safe passage") are not viable at 1-3 slot latency against block-0
  bundles. Do not build: pre-launch watchlist, ShredStream, fake-follower ML,
  "bots as our exit", breakout up-sizing.
- Build first: an insider-inventory exit (dev + block-0 bundle + one-hop SOL
  funders + SPL transfer recipients; exit on first distribution), which
  generalizes the only validated signal (winner-sell).
- Replace the survival label with simulated executable PnL: entry at decision
  + 2-3 slots against real reserves, all fees, the real exit engine, dead
  coins in the denominator, fixed horizons; report means with bootstrap CIs
  and drop-top-coin checks (memecoin payoffs are tail-driven; median gates can
  hide negative EV).
- Treat fresh-wallet and buyer-count lifts as fakeable (manufactured
  graduations hand tokens to fresh wallets); split identical-size buys into
  terminal presets vs wash before using them.
- Recoup-then-ladder is mental accounting: no passive limit orders exist on
  the curve or PumpSwap; exits should be signal-triggered (insider
  distribution, winner-sell), time/velocity stops, sized to pool depth.
- Most plausible edges: slow post-graduation PumpSwap entries (graduated
  > 10 min after launch, score >= 4, strictly-prior X breadth, wash-stripped
  inflow) with insider exits; and the untested KOL wallet-buy -> post gap
  (Kolscan wallets; include unposted buys; kill if gaps < 5 s or EV <= 0).
- Probability of positive net EV within 3 months: 15%, 15-20%, 15-20%
  (argon: < 5% on the curve or at 0.05 SOL; PumpSwap at 0.25-1 SOL).
- Disagreements: whether >= 5 aged X authors is purchasable (fable) or
  relatively robust (astra, argon); what to run first (fable: KOL gap study;
  astra: fixed-horizon re-label + launch-score entry backtest; argon:
  reserve-exact simulator + insider exit).

## Wave C3: council round 2, prediction markets and crypto candles (2026-11-05)

The owner's challenge: somebody is making money, so where is it, and can we
take a piece? The packet (`docs/council/2026-11-05-prediction-markets-candles/`)
contains:

- the cross-venue desk's live record;
- the Novig resolution study;
- a 38-test negative study of Kalshi 15-minute crypto;
- a vendor census of 2,500 Polymarket BTC bots;
- a reverse-engineering of the gabagool22 wallet;
- two option-implied pricing-gap studies;
- the candle/seasonality/carry literature.

All three reviews are archived beside it.

Unanimous:

- **Where the money goes.** Passive two-sided liquidity on wide, rebated
  venues: taker fees are transferred to makers through rebates. Sellers of
  speculative longshot YES. Carry. Not prediction. Buying both outcomes below
  $1 is ordinary two-sided market making, not arbitrage (argon).
- **No latency play** at our ~1.3 s order turnaround: the 53% streak reversal,
  cross-coin lead-lag (t = 0.56), Kalshi-leads-Binance and Kalshi 15-minute
  pair-locking are all dead.
- **Candlestick patterns carry no edge after costs.** Only the underlying
  effects survive (carry, order flow), never the pattern itself.
- **The desk's +$1.98 on 10 hedged lots is selection.** Over all 18 lots it is
  +$3.14, mostly unhedged coin flips, and the n = 4 maker fills at +$2-2.9 are
  luck. Count every lot and measure markouts before reading any per-lot edge.
- **The one directional candidate is buying NO against rich longshot YES on
  crypto thresholds.** Its evidence (5.6-11 pp over option-implied digitals) is
  September 2023 data. Kalshi's 15-minute crypto volume alone grew about 90x
  from January to July 2026. Re-measure it against option-implied digitals
  before risking capital.
- **Realistic upside at a $10k bankroll is single digits to ~$35 a day.** The
  largest risks are engineering time spent on small edges, and adverse
  selection read as edge.

Probability of positive net EV within 3 months for each reviewer's top
candidate: astra 25-30%, fable 35-40%, argon 35%.

Disagreements:

- **Which maker variant to run first.** fable: two-sided pair-locking on
  Polymarket US crypto brackets. astra: options-priced fair-value quotes on
  those brackets. argon: pre-game cross-venue maker-taker.
- **How to hedge a NO book.** astra: unhedged, with per-expiry caps. fable: a
  static spot overlay, never a delta-hedged digital. argon: delta-hedge with
  CME micro futures or Coinbase perps, exiting more than 24 h before expiry.
- **NO price band.** astra 90-97c, fable 85-95c, argon 75-90c.
- **Odds that carry beats cash.** 15-40%.

Checked after the reviews: argon's latency fixes are already in place. The
desk VM runs in `us-east4` (Ashburn; 62 ms to Polymarket, 21 ms to Kalshi REST)
and takes fills from the private stream. The remaining ~1.3 s is Polymarket
US's own order handling.

Agreed next measurements, each free or under $100:

1. Markout audit (+60 s, +10 min, settlement) with all-lots P&L on the desk's
   existing fills.
2. A 10-day logger comparing Kalshi and Polymarket US crypto strike prices with
   option-implied digitals, adjusted for settlement time and index.
3. One small live maker test with markouts, on Polymarket US crypto brackets or
   pre-game lines.

Kill criteria are in each review.

## Wave C4: tape-6 entry test, desk audit and option-gap first look (2026-11-05)

**Tape 6 (post-graduation entry, pre-declared): negative, lane closed.**

Tape 6 ran with the flush-order fix and stopped cleanly on its 55 GB storage
ceiling after about 5.5 h. With post-graduation windows live, the raw stream
grows about 9.7 GB/h. The tape holds 10,591 coins, about 93 graduates with
post-graduation windows, and 876k pool trades.

The pre-declared test (entry 30 min after graduation, 0.05 SOL, 1.25%/side +
tx fees, mark-to-last) covered 90 SOL graduates with pool trades:

- 43 pools died before the entry, so 47 entries were possible.
- Every filter lost money at 60/120/240 min, with win rates of 0-13%.
- The A3 retained-liquidity rule was the worst: median -0.0497 SOL on 0.05 at
  every hold (n=15). Pools still liquid at 30 min were the ones drained next.
- Base median -0.0028; graduation timing, X authors and LunarCrush filters
  were all negative.

This closes post-graduation entries. The winner-sell exit remains the program's
only validated signal.

**Cross-venue desk audit** (read-only ledger copy; `polymarket-alpha-bot/data/research/desk-audit/`):

- **The ledger reproduces every documented figure to the cent.** Over the
  full sample since 10-30, the desk lost $50.87 on 83 lots (mean -$0.61, CI
  upper bound -$0.25). 57 unwound lots from the build-out week cost $56.16.
  Since 11-02: +$3.13 on 19 lots, CI [-$1.14, +$1.28], which is
  inconclusive.
- **Fill rates.** 1,771 attempts covered 1,322 unique opportunities. Maker
  rests filled 1.1% of orders and taker fill-or-kill 6.8%.
- **Markouts.** Polymarket taker opens are favorable (+3.6 c/share at 60 s,
  +8.4 c at 10 min). Unwinds are the loss: -11.9 c at 60 s and -19.5 c at
  10 min.
- **No rebate at our size.** Polymarket US pays maker rebates only from
  $10M of maker volume, so the 25% maker rebate assumed in round 2 does not
  apply.
- **Ledger defect.** Seven lots from 10-30/31 were overstated by $0.38 in
  total (a fee credited as proceeds); the code is fixed but the rows were
  never restated.

**Option-implied gap logger** (10-day run started 2026-11-05T19:15Z):

- First snapshots priced 1,462 Kalshi and 963 Polymarket US BTC/ETH strikes
  against SVI-fit Deribit digitals.
- Low-probability YES is rich by about 1-2 pp, not the 5.6-11 pp of the 2023
  study.
- After fees, taker edges are almost all negative.

## Wave C5: winners contest (2026-11-05/06)

The owner's challenge: reverse-engineer Polymarket and Kalshi winners, find
the hidden edges, the consistent "sweet spot" and multi-account entities.
Full record: `docs/council/2026-11-05-winners-contest/` (REPORT.md, sealed
pre-registrations, rules, findings, verifier results).

Setup:

- **Data.** Public Polymarket Data API v2 (v1 was retired 2026-11-02):
  71,524 board wallets with full daily P&L series, 9.2M trades and 853k
  markets, plus on-chain linkage for 1,872 wallets on our Polygon node.
  Kalshi's unauthenticated `v1/social` API gives 6,418 traders, but per-trade
  history only where public: 14 of the top 250. Polymarket US exposes nothing
  per account.
- **Scoring.** Discovery data before 2026-10-06; holdout 10-06 to 11-05.
  The universe of 1,332 wallets was chosen from pre-cutoff P&L only.
- **Leak control.** Contestants saw an as-of-cutoff view in sandboxes;
  scoring used a deterministic event-clustered verifier.
- **Leaks found and fixed before launch:**
  - today's board P&L, which minus discovery P&L reveals holdout P&L;
  - wallets collected only via short-window boards;
  - outcome and position features built with post-cutoff resolutions;
  - markouts crossing the cutoff;
  - a newest-first trade cap whose truncation revealed October volume.

Result: **argon won, 42 pts (9/18 verified)**. astra 24 (best blind round:
24 vs argon 22), fable 4, orchestrator benchmark 1 (non-competing).

Verified holdout edges:

- **15-minute crypto maker fills:** found by all four, +$0.57-0.75/trade.
- **Pre-end "already decided" 96-99.9c carry** outside sports and crypto:
  +$3.16/trade, 33k trades, 116 wallets.
- **Multi-outcome slate NO at 85-96c** on events with 3+ markets:
  +$18.24/trade, 20k trades.
- **Smart-wallet shadowing:** +$3.94/trade, 92k trades.
- **Weather:** +$1.44/trade, 11k trades.
- **After-end carry:** +$0.77/trade; the CI lower bound is only $0.02.
- **Near-certainty "bonds"** in crypto 4h/1d, mentions and tech: small n.
- **Two linked 6-wallet fleets** with 100% positive holdout weeks.

Outside the contest universe, SybilHunter found the owner's hypothesis
verbatim: a 32-wallet sports farm (27 wallets created within 20 minutes, $110
clips at 0.99, markets split between wallets, 100% positive weeks, +$200k in
the holdout).

Failed in the holdout:

- pre-game sports, for every entrant;
- in-play sports and broad politics/geopolitics books (event-clustered CIs
  crossed zero);
- every sweet-spot and wallet-persistence claim: no excess over controls
  matched on P&L decile and market type.

The last failure is the main lesson. Most consistent wallets selected before
the cutoff went dormant (the U_disc median holdout P&L is $0). Persistence
exists only among wallets that keep trading, and that cannot be selected in
advance.

Why copying winners fails: the data shows the mechanism but not the signal;
followers pay the spread the winners earned as makers (72% of winners' fills
are maker fills); exits, hedges and sharding are invisible; leaderboards
select on outcomes. What transfers is a process signature: price band ×
timing to end × category × maker share, inside capacity limits.
