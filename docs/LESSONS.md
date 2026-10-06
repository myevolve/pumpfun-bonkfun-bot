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
