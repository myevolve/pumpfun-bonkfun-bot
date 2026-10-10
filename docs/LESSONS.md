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

## Cross-instrument overlap verified (2026-10-06 16:46Z)

The shadow and the paper arm are no longer independent narratives: JEANPHILF
is in BOTH — the paper arm's lesson 59944 (accepted 76.89 SOL) and the
shadow's pool row at 14:08:58Z. Two instruments, same coin, same graduation,
independently priced. Cohort velocity: pool rows 15 → 21 → 27 across ~90
minutes (fastest yet); completions 37; max curve in the paper window 53.34
SOL and climbing toward the 60 gate. The promotion case's cohort threshold
(~30 pool-priced outcomes) is roughly an hour away at this rate; the
duration gate (24-48h of sealed snapshots) remains the longest pole.

## Duplicate-writer incident (2026-10-07 04:14Z, resolved)

Two bot_runner processes wrote the same journal for ~6 hours: the paper
keeper's child (spawned 21:59Z) survived every operator-managed restart
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
hold. What we report is **average realized hold** — the elapsed time of
resolved exits, by reason — now in every evidence bundle
(`avg_hold_s`, `avg_hold_by_reason`) so the hold statistics accumulate
with the rest of the honest record.

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
