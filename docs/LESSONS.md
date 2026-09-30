# Paper-Trading Lessons — living document

Updated: 2026-09-30. Source artifacts: `state/paper-trading/*.json`,
`.state/learning/lessons.sqlite3`, `learning-examples/token-lifecycles/README.md`.
Every claim here is grounded in a sealed artifact or a commit; anything
uncertain is marked. Update this file when the evidence changes.

## What the market taught us (negative results, held-out)

1. **No post-creation entry signal has positive held-out expectancy.**
   31,104 policies tested (gates: mayhem/min-buyers/liquidity/creator-holding;
   exits: trail/stop/TP/hold) across slots 1-10 — best in-sample policies
   lose -0.5% to -3.3% per trade out-of-sample. Entering at curve milestone
   X SOL loses -10% to -13% at every X. (token-lifecycles README, Sep 2026)

2. **Post-graduation dumps are manufactured.** Curve bought out in <=10
   trades, 50-100 SOL sniped at pool creation, pool drained within 25 min
   (Sep 2026 live tape). The 0.7-0.9 Jev-scored paper fills that died inside
   60s (avg -0.0055 SOL, n=10, 2026-09-30) are consistent with this — a high
   launch-quality rating does not survive the first minute.

3. **Follower/copier models lose.** A locked cohort of 17 wallets lost
   -3.12% per follower trade at +2 slots held-out. Copying actor receipts
   does not reproduce their execution. (tx 2CQgjcdN audit: 261 failures
   among 403 txs, 1.50 SOL fees including 0.79 on failures.)

4. **Mayhem coin flow is time-clustered.** Gate accepts: 23 in a 10-min
   window (15:40 UTC Sep 29), then 0 accepts for 2+ hour stretches. Any
   sampling or reporting that assumes steady-state will mislead. Paper-run
   restarts on the 600s one-shot budget are normal and must not be counted
   as crashes.

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

7. **Jev quality scores arrive on a 0-1 scale** (SDK Score normalization),
   regardless of the "0-4" wording in the question. Bucket on
   ROUND(quality,1); thresholds HI_MIN=0.6, LO_MAX=0.4 (fix 1903944).

8. **Jev latency is ~0.9-6s per call.** Fine for per-decision annotation;
   disqualifying for a <1.5s snipe gate. Never put it in the hot path.

9. **Dry-run evidence discipline works.** The journal caught every issue
   above because decisions, fills, and outcomes land in SQLite with Jev
   annotations — no memory, no vibes.

## Jev-vs-gate correlation (2026-09-30, n=5,864 scored lessons)

Accepted coins (buyers_present) average jev_quality **0.897**; skipped
cohorts average 0.33-0.48. This correlation is **expected and partially
circular**: the scorer's prompt includes the gate state (buyers, real_sol)
that also drives the accept decision. It is NOT independent evidence of
predictive power. The only admissible test remains jev_quality vs realized
outcome PnL on resolved fills.

## The Jev promotion rule (unchanged until evidence says otherwise)

Jev enters the live entry gate only when `pnl_by_quality` shows high
scores (>=0.6) systematically outperforming low scores (<=0.4) on a
sample large enough to trust (target: >=50 resolved outcomes per bucket,
stable across separate windows). Current sample: n=39 resolved across five
windows (2026-09-30). Variance is the story: the 08h and 20-22h UTC
windows dumped (avg -0.0045 to -0.0077, curve-to-zero exits); the 21h
window ran +0.0057 avg across 13 fills, including e/acc at +913% in 60s
(entry 1.15e-9 -> 1.17e-8, real curve reads). Jev quality did NOT separate
winners from dumpers (e/acc 0.91, Loop 0.91 - one +913%, one -100%).
**Sample still far too small; verdict remains: not promoting.**

10. **A hung startup looks identical to a running bot from outside.**
    The 12:55 run churned httpx RPC calls for 7+ hours with zero trader
    output - never connected the geyser, never started the trader. pgrep
    (the keeper's check) sees a live process and does nothing. Fix: the
    keeper also checks log staleness - if the newest run log grew in the
    last 5 minutes but contains no non-httpx lines, kill and restart.
    (2026-09-30 12:54 incident, keeper.log)

## Current system (as of this document)

- Paper bot alive under keeper (auto-restarts on 600s budget), instrumented
  sampler, WAL journal. Dashboard 🧠 Learning tab + `learning.report` CLI
  render the evidence live.
- Journal scale: ~5k-6k lessons/day, ~90% Jev-scored.
- Wallet 9MFfWXdT…: 0.562180 SOL, zero real transactions — every submission
  still blocked at the dry-run gate.
