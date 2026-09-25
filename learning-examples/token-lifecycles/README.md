# Token lifecycle research

Tooling and findings from the 2026-09-03–16 investigation into where, if
anywhere, a bot at retail latency and size can enter a pump.fun coin's life
and exit positive. Everything here is read-only against Geyser/RPC; nothing
submits a transaction.

## Tools

| script | purpose |
|---|---|
| `record_lifecycles.py` | Stream pump.fun + PumpSwap over Geyser with an explicit `--env-file`; retain local receive times, a processed-slot sidecar and incomplete-coverage flags. Bounded by `--minutes`, `--idle`, `--max-age`. |
| `summarize_lifecycles.py` | Population stats plus a 31k-policy entry/exit backtest with a time-split holdout (rank on the earlier half, score on the later half). |
| `summarize_graduations.py` | Post-graduation price paths, actor concentration, and entry/exit backtest on the PumpSwap tape. |
| `replay_gate.py` | Legacy gate/flow replay; separate `--lock <LOCK.json> --out <NEW.json>` mode evaluates only a preregistered immediate-handler model with source/fee provenance and observation-coverage guards. |
| `corpus_to_lifecycles.py` | Convert the Slinky21 PumpFun corpus (Jun-Jul 2026, 798k launches) into the same JSONL so the same scripts run on it. |
| `evaluate_creation_paper.py` | Source-locked creation-arrival (C) versus two-processed-slot delay (D2) paper comparison; separates conditional quotes, unpriced inventory and capture censoring. |
| `run_paper_trader.py` | Continuous credential-free discovery, canonical account-based paper entries/exits, durable virtual capital and forward-only online exit-horizon learning. Separate from live execution. |
| `verify_solana_actor_receipts.py` | Bounded Geyser actor cohorts and exact native receipt replay, with held-out closed-cycle/failed-fee components rather than invented portfolio PnL. |
| `verify_evm_actor_receipts.py` | Frozen sparse receipt/trace sampling and replay; unresolved wrapper balance changes cannot become a combined native cashflow figure. |
| `simulate_atomic_cycles.py` | Freeze a Raydium AMM v4/CPMM universe, quote same-bank reserves, and immediately simulate unsigned candidates before their frozen +2/+5-slot checks. Includes an offline `--self-check`; never signs or submits. |
| `simulate_reference_cycles.py` | Derive native CLMM ↔ PumpSwap instructions from a finalized receipt, simulate complete cycles under fixed caps, and replay frozen profit-guarded candidates at +2/+5 slots. Never signs or submits. |
| `simulate_geyser_cycles.py` | Bounded cloud Geyser observer for frozen AMM v4/CPMM pools, or the explicit FARTCOIN AMM ↔ Orca native mode. Immediately validates unsigned candidates without competing delayed work. |
| `simulate_orca_cycle.py` | Caller-address/mint-bound Whirlpool adapter: attests SPL vaults, directional tick arrays and either a static pool with no initialized oracle or the complete adaptive oracle. The deployed program executes each leg. |
| `simulate_clmm_cycle.py` | Ordinary-SPL Raydium CLMM adapter: validates pool-bound accounts and bounded initialized tick arrays; native `swap_v2` supplies prices and execution. |
| `verify_clmm_cycle.py` | Offline controls for negative/extension tick-array boundaries, non-PDA observation accounts, native instruction layout and ordinary-SPL exclusions. |
| `simulate_menu_orca_pair.py` | Preregistered fixed-quantity two-Orca comparison: state and both unsigned native simulations in one batch, exact-slot eligibility, bounded hash-chained capture and offline replay. |
| `verify_atomic_cycle_simulation.py` | Offline loopback checks for native fee/rent/profit guards, freshness, unsigned signatures, reversed batch delivery and terminal truncated/duplicate batches. |
| `verify_rpc_bank_readiness.py` | Offline minimum-slot error classification, mixed/malformed batch refusal, public metadata acceptance and configured-secret rejection. |
| `observe_evm_meme_cycles.py` | Cloud `newHeads` observer for canonical V3 ↔ V3 and V3 ↔ hookless native V4 cycles, with pinned banks, exact-envelope fee checks and shared-capital episode accounting. Code-only override; no signing or deployment. |
| `verify_v4_pool_keys.py` | Resolve at most five frozen Robinhood PoolKeys from canonical Initialize logs in fixed 256-block windows around creation-time hints. Provider-only reads, bounded requests, no guessed keys or execution claim. |
| `simulate_evm_meme_cycle.sol` | Native callback executor used only as an `eth_call` code override at an attested empty address. Repays V3/V4 debts, settles native/wrapped assets and measures caller payout. |
| `verify_evm_meme_cycles.py` | Offline checks for first-pending deadlines, sticky reorg rejection, exact fee net, fatal RPC boundaries and censored opportunity episodes. |
| `probe_creation_execution.py` | Bounded verified-CreateEvent → actual buyer → unsigned simulation; one response+10-second shadow mark, explicit provider file, no submission. |

Row format: `trades` is a list of `[dslot, wallet, is_buy, sol_lamports,
token_raw, real_sol, virtual_sol, virtual_tok, ts]` relative to the creation
slot; `post_trades` is the same shape on PumpSwap with `[..., pool_base,
pool_quote, virtual_quote, ts]`. PumpSwap pool reserves are **pre-trade**,
not post-trade. Wallet `BwWK17cb...` is Mayhem's `["sol-vault"]` PDA under
`MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e`, **not** the System Program.
Its trades remain in the tape, but it must not count as an independent buyer.

New recordings include `trade_received_ms`, aligned
`trade_real_token_reserves`, `observed_duration_ms`, and a `.slots.jsonl`
companion. They answer Yellowstone ping requests and remain deadline-bounded
when no transactions arrive. A head gap above one second marks tracked
observations incomplete; a long wall-clock lifetime alone does not prove
continuous coverage. Importing the recorder does not load dotenv, and root
`.env`/`.env~`/`ENVDATA` paths are refused. Old tapes are not backfilled.

## Running online paper learning

```bash
# Offline regression: isolated temporary SQLite, no keys or network.
uv run --offline --no-sync python -B learning-examples/verify_online_paper.py

# Continuous paper-only service; SIGINT/SIGTERM stops it safely.
uv run --offline --no-sync python -B learning-examples/token-lifecycles/run_paper_trader.py

# Passive status; no network, writer lock, recovery or configuration changes.
uv run --offline --no-sync python -B learning-examples/token-lifecycles/run_paper_trader.py --status
```

The separate `.state/paper-trading/paper.sqlite3` contains paper decisions,
qualified account evidence, costs, censored inventory and policy revisions.
Only one writer may run. `--seconds 120 --db /absolute/path/new-paper.sqlite3`
creates a bounded separate experiment; it is not a way to reset the main
paper portfolio. Source/config mismatches fail closed. Carrying an existing
portfolio across versions requires a separately verified offline migration of
the stopped book, with its accounting and historical events preserved; it is
not a fingerprint bypass or capital reset. Unrelated databases are refused
before schema writes.
Status is the last committed observation: check `runtime.observed_utc` and the
supervisor's process state. A stored `running` flag alone does not prove liveness.

Discovery uses PumpPortal's free `subscribeNewToken`, **not** its paid token
trade stream. Marks use public Solana `getMultipleAccounts`: the canonical
fee account plus mint and derived curve accounts in one confirmed bank.
Requests start no faster than every two seconds, have a two-second read bound,
and require advancing slots. HTTP compression is decoded before bounded JSON
parsing. No keys, dotenv, funded API wallet, signing, submission or live bot
configuration are used. `PAPER_READY` requires a verified mainnet genesis,
acknowledged discovery and an accepted account mark—not just a heartbeat.

`evaluate_online_paper.py` starts with **1 SOL virtual cash** and uses 0.01-SOL
paper entries. At most 12 funded or unpriced portfolio exposures and 12 active
market-observation cohorts are allowed. Network/tip/cleanup costs are fixed
scenarios; exit fees are reserved before portfolio entry. Each side receives a
1% adverse haircut and rent is conservatively modeled as 0.0021 SOL unreturned.
Each observed cohort compares 10-, 30- and 60-second exits. Only a selected
portfolio arm affects cash; shadow returns never fund the portfolio.
Warmup is shadow-only. Afterward, no positive scored exit, insufficient cash,
or exhausted exposure capacity blocks portfolio spending **without stopping
shadow observations or learning**. Capital is never silently replenished.
The version-2 reserve model retains the hypothetical buy's net SOL deposit
and pre-haircut token removal in both real and virtual reserves at later marks.
It overlays those deltas on observed external reserve changes; it does not
assume the other traders would react identically in a real counterfactual.
If the adjusted inventory crosses an unsupported curve boundary, valuation
still fails closed. All shadow arms apply the same entry once, independently.

The first online run exposed why this matters: erasing the hypothetical
deposit at each new native snapshot made quiet curves unable to repay their
paper buyer. Its 54 entries, 42 selected closures and 12 unpriced positions
remain unchanged in `.state/paper-trading/model1-no-entry-impact.sqlite3`;
the earlier source archive and verification snapshot are retained beside it.
The corrected version is a **separate experiment with a new 1-SOL virtual
opening balance**, not recovery of those positions or erasure of the first
run's modeled losses. Neither outcomes nor training samples are pooled.

Learning uses only fully completed paired cohorts, rolling over the latest
60. After five complete cohorts it ranks exits by mean modeled net minus two
sample standard errors, and compares the best score against **not trading:
zero additional cost and return**. A nonpositive score means observation-only,
not a forced choice of the least losing trade. This is a **heuristic, not a
formal confidence interval**. Each cohort freezes its portfolio decision,
policy revision and horizon; updates affect later cohorts only. Missing/late
exits, unsupported migrations and restart interruptions remain censored and
never become zero-return training examples. Censored portfolio inventory keeps
its reservation; censored shadow arms create no portfolio inventory.
Censored cohorts do not advance the learning revision. Status distinguishes
`cohorts_started`, `shadow_entries`, portfolio `entries`, `policy_action`,
`portfolio_entry_block`, complete pairs and unknown inventory. `last_entry`
is the last portfolio entry; `last_cohort` includes current shadow decisions.

The September 16 version-2 run exposed the capital/learning coupling: after
375 entries, usable cash fell to **0.011162552 SOL**, below the **0.012185 SOL**
entry reservation. Learning stopped at 07:10 UTC while the process continued
receiving discoveries. The version-3 upgrade preserves all **27,678 prior
events**, **−0.952282448 SOL known modeled net**, and **three unpriced positions
reserving 0.036555 SOL**. It adds no cash, changes no past outcomes, and retains
the same quote economics and training history. An explicit
`paper_learning_upgrade` event records the source/config transition.
The stopped predecessor and its exact source are archived as
`.state/paper-trading/paper-before-shadow-learning-20260916T162418Z.sqlite3`
and the adjacent `.source.zip`. Future shadow learning now continues even
when that portfolio cannot fund another entry.

These remain **conditional account-quote scenarios, not fills or income**.
Native fee-program execution and configurable creator-fee overrides are not
attested. The non-Mayhem fee-market-cap supply comes from the curve, not a later
holder-burned mint supply. Mayhem/non-SOL curves and transfer-affecting mint
extensions are excluded. There is no independent chain-head freshness
attestation or complete creation denominator, and the effect on other traders'
behavior is unmodeled. Selection among complete pairs can be biased. No
paper policy is promoted to live trading; the earlier profitability and
heldout-evidence blockers remain unchanged.

## Findings (tape-model results unless noted)

Coins recorded: 1,769 (1h), 10,610 (8h), 24,015 (24h). The readiness wallet's
retained bot ledger records 8 completed live trade cycles: two 0.0001-SOL-budget
micro-tests and six 0.01-SOL-budget trials. Budgets differ from actual spend;
five trials used the entry gate. The earlier "7, all at 0.01 SOL" count is
not supported by this ledger. No live trades occurred in the atomic research.

**Slot 0 (creation slot, not proven bundle membership).** The earlier +17%
figure was a trade-tape cash estimate, not audited portfolio ROI: the tape
omits failed transactions, execution fees, tips, and outside transfers.
A locked cohort of 17 wallets selected from the first half of the 8h tape
lost -3.12% per follower trade at +2 slots in its held-out half and -4.31%
in the disjoint 24h tape; +5 slots lost -5.74% in the 24h tape.
These are follower-model results, not fills.

A separate finalized-transaction audit of `2CQgjcdNEo7WtbQLpJTAVcC3Ga61pNvRDTgP5grzctFG`
found 261 failures among 403 transactions. Actual transaction fees totaled
1.504217773 SOL, including 0.786149106 SOL on failures. This does not establish
the wallet's complete portfolio return.

**Slots 1-10.** No gate or exit rule is positive out-of-sample across
31,104 policies (mayhem/min-buyers/liquidity/creator-holding gates;
trail/stop/take-profit/hold exits). Best in-sample policies score -0.5% to
-3.3% per trade on the held-out half. An apparent +10% signal in the first
hour was Mayhem's protocol vault being counted as a buyer. The five gated live
trades landed buys at creation +2 to +5 slots and have a combined logged swap
spread of -0.015248325 SOL before network/cleanup costs, with one positive and
four negative spreads. The earlier "-19% net" summary is withdrawn: its
denominator and costs do not reconcile with the retained records. See
[own-live accounting](#own-live-accounting-and-attribution) for scope and limits.

**Curve milestones (20-60 SOL).** Entering when the curve crosses X SOL
loses -10% to -13% at every X, confidence intervals fully negative. The
market prices P(graduate | reached X) at almost exactly the breakeven level
for each X.

**Post-graduation (corpus, Jun-Jul).** A gradient-boosted classifier on
graduation-time features lifts precision for good outcomes from 14-20% to
29-33%, but the top decile is -3% to -20% in every walk-forward fold. The
only positive variant used the corpus's first post-graduation snapshot as a
feature, which is look-ahead.

**Post-graduation (live tape, Sep 2026).** The previously reported price-path
and entry-PnL numbers are withdrawn: they treated PumpSwap's pre-trade
reserves as post-trade state. Do not use those estimates as executable
quotes or evidence of an edge.

**Conclusion.** No tested post-creation directional strategy demonstrated
positive held-out expectancy. Same-slot actor activity does not prove a
bundle, and copying actor receipts does not reproduce their execution.
Atomic execution is a safety property, not a demonstrated profit source.
These results do not justify enabling the live bot.

## Atomic forward probe

```bash
uv run learning-examples/token-lifecycles/simulate_atomic_cycles.py --self-check
uv run learning-examples/token-lifecycles/verify_atomic_cycle_simulation.py
uv run learning-examples/token-lifecycles/simulate_atomic_cycles.py \
  --env-file .state/wallets/live-readiness.secrets \
  --payer 9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe \
  --minutes 10 --markets 20
```

Fixed sizing: a 0.01 SOL input cap, 65,000 lamports of network/priority fees,
10,000 lamports of tip, and a 1,000-lamport profit floor. Both ATAs must be
absent initially and must close on success; simulated rent refunds are not
income. API data discovers addresses only; quote accounts share one RPC bank.
Repeated positive quotes for one mint form one episode, reset only after an
observed nonpositive quote—not a missing snapshot.

The corrected 2026-09-06 forward run froze 16 eligible markets and 66 pools.
In 600.31 seconds it read 9,821 snapshots and evaluated 146,112 directed
route quotes: **zero candidates cleared the guard**. The closest cap-based
quote was 72,844 lamports below the required floor, or **-71,844 lamports
(-0.71844%) net** under the fixed spending/fee model. This is a quoted result,
not realized PnL or an estimate of live expectancy.

Evidence: `atomic_cycles_20260906_144619.jsonl` records the universe and final
counters. The earlier `atomic_cycles_20260906_143025.jsonl` is superseded:
it subtracted one raw token before quoting a cycle, which could suppress
low-decimal candidates. The offline self-check now defends that boundary.

Separately, forced losing Fartcoin cycles exercised both AMM-v4 → CPMM and
CPMM → AMM instructions at actual +2/+5-slot delays. In all four unsigned
simulations the buy executed and the sell rejected at the profit guard
(CPMM 6005 / AMM v4 30). These are negative controls, not detected candidates.
No transactions were submitted; the wallet stayed at 0.562213411 SOL.

Coverage is deliberately bounded: top-volume discovery, at most six pools
per mint, SPL Token only, no CLMM or PumpSwap. A short sample with no
candidates does not prove arbitrage is never profitable. Simulation cannot
measure landing probability, competition, or realized expectancy; a real
rejected transaction would still charge fees despite atomic rollback.

## Cloud Geyser probe

Run latency observations in the cloud, not on the development machine. The
frozen source archives include the canonical Geyser stubs, hashed dependencies,
pinned container image and Cloud Run Job specification. Mount only a projected
provider JSON secret containing `rpc_url`, `geyser_endpoint` and `geyser_token`;
never mount the wallet secrets file or the root `.env`.

The six-venue Chainstack diagnostic treats a correlated `-32016` on a
floor-bounded state read or unsigned simulation as a missing current sample,
not a fatal transport failure. It does not retry, lower `minContextSlot`, reset
the original deadline, or price a missing sample as zero. Later fresh updates
remain eligible; other RPC errors and audit failures retain their stop rules.
Public URLs in on-chain data are not credentials. Configured service values
remain forbidden in retained evidence, and provider error bodies are not saved.
Check these boundaries offline with
`uv run learning-examples/token-lifecycles/verify_rpc_bank_readiness.py`.

Inside the prepared cloud Job:

```bash
python -B -u /tmp/probe/learning-examples/token-lifecycles/simulate_geyser_cycles.py \
  --credentials /var/run/provider-config/provider.json \
  --scope /tmp/probe/learning-examples/token-lifecycles/scope.json \
  --output /tmp/geyser_cycles.jsonl --seconds 90
```

The default observation bound is 90 seconds, including initial RPC reads, plus
a final wallet audit. `--seconds` accepts at most 1,800; `--http-budget` defaults
to 200 and accepts at most 3,600. Dependency installation and container startup
are separate. HTTP starts remain at least 500 ms apart, with one request
reserved for the final audit. Do not run concurrent Jobs against the same
provider: their individual rate limits would add together. The earliest coalesced notification starts a
one-second snapshot/immediate-validation deadline; newer notifications cannot
extend it. Only a bank-not-ready response (`-32016`) retries within that deadline.
Throttling, transport failures and stream failure stop the probe; they do not
trigger reconnects or submission retries.

The scope fixes one legacy-SPL mint, its expected decimals and 2–6 distinct
direct-SOL Raydium AMM v4/CPMM pools before observation. Decimals and pool identity
are verified on chain. Input and modeled fee remain capped at 0.01 SOL and
75,000 lamports. Missing, partial, stale or rejected-best-route observations
cannot manufacture new independent opportunities: only a complete fresh
all-negative quote bank closes an episode. The current observer schedules no
delayed +2/+5-slot work on its shared HTTP boundary.

The optional scope field `venue_mode: "fartcoin_amm_orca_native"` selects only
FARTCOIN mint `9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump`, Raydium AMM
`Bzc9NZfMqkXR6fz1DBph7BDf9BroyEf6pnzESP7v5iiw` and Whirlpool
`Tuy6gMupGQN7wCZ8rVP1EuLRYB132VSo9Smy4AJvQgn`. Omitting it preserves the Raydium
mode. Both directions use one AMM-sized quantity, actual native swap
instructions, closed temporary ATAs and a final whole-wallet balance guard.
The adaptive oracle must be writable and attested; no fixed-fee fallback is
allowed. Three directional tick arrays are frozen per direction; dependency
changes censor coverage rather than silently expanding scope.

One HTTP batch carries exactly two unsigned simulations and counts as two RPC
methods. Reversed response order is supported; missing, duplicate or unexpected
IDs are terminal protocol failures. A present `-32016` item remains an explicit
coverage gap. A complete native state requires both outcomes at the same
returned slot within the original one-second deadline. Equal slots do not
prove fork identity or inclusion of the streamed write. Only both profit-guard
rejections close a native episode; missing results cannot do so.

The 2026-09-13 London experiment replayed **216 snapshots / 6,480 directed quotes,
zero positive states**. The initial 50-ms-paced attempt returned one native
negative-control result in 193 ms, then stopped on HTTP 429 after 14 seconds;
its final audit also failed, so a separate confirmed-bank audit checked the
wallet after cooldown. The 500-ms-paced attempt completed: 158 snapshots,
598-ms median latest-notification-to-quote latency, 12 readiness misses and one
missing immediate result. Missing results are not successful validations or
economic negatives. Its delayed checks targeted +2/+5 slots but actually ran at
+3/+10. These samples establish neither reliable subsecond execution nor profit.

Evidence: `cloud_run_geyser_research_20260913.json` links both attempts, frozen
source archives, offline replay and cleanup records. The wallet remained at
0.562213411 SOL, no transactions were signed or submitted, and both temporary
Jobs, the provider secret and the temporary service account were deleted.

## Fixed two-Orca native comparison

`simulate_menu_orca_pair.py` tests the public route recovered from a historical
Menu transaction, not Menu's proprietary routing or its complete portfolio.
Its scope freezes the two pool addresses, one intermediate quantity of
17,285,678 raw units, the public payer, source hashes, and the existing
10,000,000-lamport input cap, 75,000-lamport total fee/tip scenario and
1,000-lamport profit floor. The two directions use the **same exact intermediate
quantity**, not the same exact initial SOL debit.

```bash
# Offline. Save the printed scope to a new immutable JSON before collection.
uv run learning-examples/token-lifecycles/simulate_menu_orca_pair.py self-check
uv run learning-examples/token-lifecycles/simulate_menu_orca_pair.py \
  scope --study-id <NEW_STUDY>

# Only inside the prepared London cloud Job, using its provider-only secret.
python -B /tmp/probe/learning-examples/token-lifecycles/simulate_menu_orca_pair.py \
  collect --scope <SCOPE.json> \
  --provider-json /var/run/provider-config/provider.json --out <NEW.jsonl>

# Offline, under the exact source closure frozen in the scope.
uv run learning-examples/token-lifecycles/simulate_menu_orca_pair.py \
  analyze --scope <SCOPE.json> --tape <NEW.jsonl>
```

The full scope fixes 240 windows at 15-second intervals, split chronologically
120/120. `scope --pilot` fixes four windows instead; it does not alter economics.
There is no catch-up, extension, retry, route reselection, signature, submission
or account-state override. Capture and final wallet auditing are separately
bounded. Temporary ATAs must start absent and close on successful simulation.

Schema 2 reads preliminary metadata, then batches the full dependency bank and
both native simulations together. Rebuilding the candidates from the returned
bank must reproduce the exact submitted unsigned bytes. Both simulation slots
must equal the state slot within the original window deadline to qualify.
The requested minimum slot is the preliminary metadata floor: a simulation
between that floor and the returned state slot is ineligible, not malformed.
All reads use finalized commitment. Equal slot numbers do not establish a
cryptographic bank identity, current-leader inclusion, fills or subsecond edge.

The first pilot's separate state/simulation calls consistently differed by two
slots under the 500-ms response-header pacing rule. Those eight native guard
rejections remain retained but supply **zero paired-eligible windows**. Batching
the state and simulations produced four eligible windows in the corrected
pilot without loosening slot equality or changing the trading policy.

Only two eligible final whole-wallet guard rejections close a threshold episode;
missing, stale or unclassified failures never close one. **A guard rejection
means below the configured +1,000-lamport threshold, not necessarily a loss.**
The separate preregistered fee sensitivity decodes exact Orca `Traded` events,
including rolled-back intermediate execution. Its 6,000-lamport profile is a
counterfactual no-priority-fee scenario, not verified lower-fee inclusion.
Sampled returns must not be summed into income or daily opportunity capacity.

## Cloud native EVM observer

`observe_evm_meme_cycles.py` consumes the explicit scope schema in its module
docstring and a provider-only JSON containing exactly `rpc_url` and `wss_url`.
Its scope references the frozen runtime package compiled from
`simulate_evm_meme_cycle.sol` with solc `0.8.30+commit.73712a01` and the enforced
compiler settings. Archive the source, runtime, scope and job specification
before running in the cloud; never upload a wallet key.

```bash
python -B -u /tmp/probe/learning-examples/token-lifecycles/observe_evm_meme_cycles.py \
  --credentials /var/run/provider-config/provider.json \
  --scope /tmp/probe/learning-examples/token-lifecycles/scope.json \
  --out /tmp/native_evm.jsonl
uv run learning-examples/token-lifecycles/verify_evm_meme_cycles.py
```

Only explicitly attested canonical V3 pools and native-quote, hookless,
static-fee V4 PoolKeys execute. V4 descriptors require the exact pool ID,
canonical manager and StateView, currencies, fee, tick spacing and zero hooks.
Initialize-log evidence from `verify_v4_pool_keys.py` can supply these fields;
the observer independently checks the key's EVM Keccak hash and current state.
V2, hooked/dynamic-fee V4, V4 ↔ V4 and other families remain coverage gaps,
not substitute V3 routes. The executor must have no code,
nonce or balances, and the public caller must cover the frozen maximum gas
cost. The sole override supplies executor code: no balance, allowance or storage
overrides, signing, submission or deployment. A historical public caller is not
an authorized trading wallet.

Coalescing preserves the first pending head's deadline. Observed reorgs remain
invalidating even after a later descendant arrives. An eligible result needs
fresh native completion, a still-live valid stream, a canonical pinned bank and
a supported exact-envelope gas estimate. Unknown fees remain unknown. Missing
coverage does not close an opportunity episode; truncated observations exit
nonzero. A revert is a rejected execution, not a measured negative return.
Simulated payout minus gas excludes deployment, inclusion competition, paid
failed submissions and infrastructure/provider costs: it is not earned income.

`realtime-09131641_results.json` links the exact cloud tapes, frozen source
archives, selection exclusions, wallet/ledger audit and verified resource
cleanup. Its $100/day objective remains **unestablished**: the selected Solana
markets produced no positive quote state, and no Robinhood native cycle
completed. These bounded, partially censored observations are not a
market-wide profitability verdict.

`coverage-0913210433_results.json` records the expanded native-venue experiment:
canonical V4 key recovery, compiler/runtime identity, rejection controls,
actual hybrid and Orca simulations, censored coverage, exact-revert traces,
wallet state and owned-resource cleanup. Repayment shortfalls, partial fills
and gas-cap failures are separate from measured fee-net returns. Neither
successful negative controls nor a completed observation establishes $100/day.

## Receipt-derived CLMM/PumpSwap probe

```bash
uv run learning-examples/token-lifecycles/simulate_reference_cycles.py \
  --env-file .state/wallets/live-readiness.secrets --minutes 10
```

The default reference is a [successful TROLL cycle](https://solscan.io/tx/4Avyz6pAepyvQJZuax5x5VWbBSoCFL5YDaewnjKzNihemCJyMjsRB7s4Anib3Dgf6sZcowL2ZhJurtJd9S9h6v5c)
at slot 444985228: CLMM → PumpSwap, **+0.001710733 SOL after the transaction
fee**, with non-SOL inventory flat and no rent liquidation counted as income.
It spent 0.91855452 SOL, about 92 times this probe's cap. This is another
actor's transaction, not our return or proof of their portfolio profitability.

The probe substitutes native venue instructions for the actor's private
router. Quote-only simulations spend 0.0099 SOL; complete cycles buy that
exact token quantity with a **0.01 SOL maximum** and sell all of it. Both ATAs
and the PumpSwap user-volume account must start absent and end closed.
A final System Program self-transfer requires the wallet's frozen initial
lamport balance plus 1,000, after fees, tip and rent cleanup. Each simulation
also rejects a changed initial wallet balance. Every run checks the deployed
guard's exact-balance/one-lamport-short boundary; `--minutes 0` runs that check
without starting market observation.

The 2026-09-07 ten-minute run completed **3,540 full-cycle economic
simulations, zero positive screens**:

| Direction | Network + tip | Simulations | Best net SOL |
|---|---:|---:|---:|
| CLMM → PumpSwap | 65,000 + 10,000 lamports | 885 | -0.000326770 |
| CLMM → PumpSwap | 5,000 + 1,000 lamports | 885 | -0.000258953 |
| PumpSwap → CLMM | 65,000 + 10,000 lamports | 885 | -0.000244716 |
| PumpSwap → CLMM | 5,000 + 1,000 lamports | 885 | -0.000175716 |

These are repeated bank simulations, not independent trading opportunities.
The lower-cost profile is a diagnostic: no priority fee and a minimum tip
do **not** establish leader inclusion. Profiles execute sequentially, not
against an identical frozen bank. The sampled CLMM configuration charged
2% (`20,000 / 1,000,000`); a decoded PumpSwap sell charged 30 basis points
(20 LP + 5 protocol + 5 creator), with buyback drawn from the protocol share.
Removing priority fees does not remove those venue costs.

All eight explicitly labelled negative-control replays reached the final
balance guard and rejected, at actual delays of 2–3 slots and 5 slots. Their
transaction hashes stayed fixed within each +2/+5 pair. No transactions were
submitted; the wallet stayed at **0.562213411 SOL**.

Evidence: `reference_cycles_20260907_062519.jsonl`. This initial run covered
one non-Mayhem, non-cashback, legacy-SPL market and captured CLMM tick arrays.
Unavailable layouts or exhausted tick windows fail closed. Processed-bank
simulation does not measure competition, failed-submission fees, landing
probability or realized expectancy. This result does not authorize live trading.

### Lower-fee follow-up

The next selection used the first **200 direct-SOL CLMM pools by volume** from
Raydium's API. The ≤0.25%-fee, legacy-SPL shortlist contained 75 pools across
63 mints; only **NEET** intersected an existing canonical PumpSwap pool.
Its on-chain CLMM fee was **0.1%**, rather than TROLL's 2%. This is a bounded
API shortlist, not an exhaustive scan of all markets.

```bash
uv run learning-examples/token-lifecycles/simulate_reference_cycles.py \
  --env-file .state/wallets/live-readiness.secrets --minutes 10 \
  --reference 3N53Y3P3EFZcheL5J5PWsC3grd2wjA9qXDkbnDjupZQc9euqXq1cjEKsXtMU3bbrxpJBgkHe97442re7pTmidCkF
```

That reference is a sponsored split **user sell**, not an arbitrage receipt.
The probe separates fee payer from trader, normalizes the CLMM input direction,
and reports `reference_native_net: null` when source token inventory changes.
The receipt supplies account layout, not profit evidence.

The probe now refreshes initialized CLMM arrays from the pool and extension
bitmaps. An uninitialized current array is legal: native traversal begins at
the nearest initialized array in the swap direction. Coverage is bounded to
three arrays per direction. Raydium's public pool-key API supplements captured
lookup tables; their ownership and active state are verified on chain.
Unsupported layouts, unavailable metadata, excess packet size and exhausted
array coverage still fail closed.

The 2026-09-07 ten-minute NEET run, under the same 0.01 SOL cap, completed
**2,276 full-cycle simulations with no execution errors and zero positive
screens**:

| Direction | Network + tip | Simulations | Best net SOL |
|---|---:|---:|---:|
| CLMM → PumpSwap | 65,000 + 10,000 lamports | 569 | -0.000089706 |
| CLMM → PumpSwap | 5,000 + 1,000 lamports | 569 | -0.000020706 |
| PumpSwap → CLMM | 65,000 + 10,000 lamports | 569 | -0.000108584 |
| PumpSwap → CLMM | 5,000 + 1,000 lamports | 569 | -0.000039584 |

All eight frozen negative-control replays reached and failed the final balance
guard, at actual delays of 2–3 and 5–6 slots. A subsequent sequential-bank grid
at quote inputs of 0.0005, 0.001, 0.002, 0.005 and 0.0099 SOL added 20 complete
cycles; all were negative. This does not prove every size or future bank is
unprofitable. Neither experiment establishes live landing probability.

Selection and observations: `lowfee_selection_20260907.json`,
`reference_cycles_neet_20260907.jsonl`, and
`reference_cycles_neet_sizes_20260907.jsonl`. No transactions were submitted;
the wallet remained at **0.562213411 SOL**, all simulated temporary accounts
remained absent, and the live bot stayed disabled.

## Broader route and actor research

The next 2026-09-07 investigation combined a frozen actor cohort, noncanonical
PumpSwap discovery, and native cross-venue experiments. All remained read-only;
the input cap stayed 0.01 SOL. These are bounded observations, not a market-wide
proof or a live expectancy estimate.

### Successful receipts versus the full actor sample

The latest 300 finalized receipts for `CQwT1byuHgjKnL6vzmuNaAywKfDBVxDmFVgsQDBWxcWt`
were frozen before inspecting cashflow. They cover 10:57:05–10:59:10 UTC:
**5 successes and 295 on-chain failures**, with no missing receipts or transport
errors. Actual fees were **0.049301803 SOL**, including **0.049019117 SOL on
failures**. Four successful receipts closed identifiable native swap cycles:

| Route | Observed input SOL | Native gain after transaction fee |
|---|---:|---:|
| Orca → PumpSwap | 0.073506923 | +0.000148968 |
| Orca → CLMM → CLMM | 0.805114899 | +0.001909446 |
| Scorch-labelled venue → CLMM | 2.614212496 | +0.000155507 |
| DLMM → DLMM | 61.097348396 | +1.592130946 |

The [dominant DLMM receipt](https://solscan.io/tx/2XM9qDYztRZVgddhVtaW8jUxSw4LDMjR7FtgnPyShxUjw7dKftN4wRG9KSsUTQckkJRxKuU7H6E1X4LvWtWh4CKA)
was independently re-read and its native delta recomputed. It contributes
**99.86%** of the four closed-cycle gains. Without it, the other three gains
minus observed failure fees are **-0.046805196 SOL**. This is a concentration
check, not complete portfolio PnL: the fifth success changes token inventory,
Scorch's layout is only partly decoded, and off-chain costs, LP exposures,
other wallets and unlanded attempts are outside this audit.

All four closed successes exceed our cap; the four identifiable first-SOL
inputs at or below 0.01 SOL all failed. The actor used pre-existing WSOL
inventory of roughly 160–162 SOL and durable nonces, not our temporary-ATA
setup. Raw System-wallet changes alone show only fees and miss WSOL gains.
No visible committed System transfer tip proves neither zero bundle cost nor
leader inclusion.

A separate 50-CLMM/50-PumpSwap signature sample found no additional qualifying
closed native cycles. Address-based discovery is noisy: only 4/50 CLMM-address
receipts actually invoked CLMM. Merely referencing a program is not execution.
Full cohort, dictionaries, references and independent receipt:
`actor_routes_20260907.json`.

### Canonical-only and allocation-size blind spots

For the original 63 low-fee mints, all exact-filter PumpSwap GPA requests failed
(4 timeouts, 59 HTTP 429s). Those are coverage failures, not absent pools.
A documented, non-exhaustive DexScreener fallback found **12 attested pools
across 10 mints**, including **11 noncanonical pools**, versus one canonical
intersection. PENGU, Fartcoin and JitoSOL had materially more liquidity than
the mostly dust alternatives. Native finalized swaps were found for four
noncanonical pools; these are not owned atomic-cycle profit proofs.

Allocation sizes were **six 300-byte, four 301-byte and two 261-byte pools**.
All documented Pool fields end at byte 261. An exact `dataSize: 301` filter
therefore misses usable pools. A successful 25-account native buy through
`7bVhc426RvNkXsHumQzVyjHZ3fATtkfFiikbxjzpczKM` and its current 261-byte account
were independently verified. Production's existing minimum-261 decoder already
handles this; the receipt probe's minimum-300 gate is narrower.

The new receipts also expose 23-account sells, 25-account buys, CLMM `swap_v2`,
and different router/user authorities. Do not identify instructions by account
count or blindly relax the existing receipt template. Its one-legacy-swap,
24-account-sell, same-user contract does not cover all these routes.
Evidence: `noncanonical_pools_20260907.json`.

### Broader native execution checks

Discovery expanded to **600 direct-SOL CLMM pools and 1,000 standard pools**.
Their supported SPL intersection contained **63 mints, 106 CLMM pools,
70 AMM-v4/CPMM pools, and 142 pairs**. Same-bank, zero-venue-fee/zero-impact
upper bounds screened 284 directions; 28 could theoretically cover configured
costs, and 117 could cover the diagnostic fee floor. These optimistic bounds
were never counted as executable returns.

Native instructions then tested all 117 shortlisted directions at the 0.0099
SOL quote size: **108 complete cycles, all negative**, and nine native CLMM
`LiquidityInsufficient` rejections. Best configured net: **-0.000076527 SOL**.
Seven smaller quote inputs on all 28 configured-bound candidates added 196
screens: 139 completed, all negative; 57 rejected for insufficient liquidity.
Best configured size-grid net: **-0.000071636 SOL**. An adaptive 19-point,
actual floor-fee refinement of the closest route also stayed negative.
This adaptive search is not a held-out profitability estimate.

Eight frozen negative controls across both AMM-v4/CPMM families and both
directions reached and failed the final wallet-balance guard at actual delays
of 2 and 5–6 slots. Evidence, including the original RPC account snapshots:
`cross_venue_probe_20260907.json`.

The observed DLMM winner was also rebuilt with official SDK **1.9.14** native
instructions, not its private router. Our own setup, exact-output buy,
exact-input sell, fee/tip limits, account cleanup and final balance guard
remained in force; SDK transaction fee defaults were not used. Both directions
at five sizes completed **20 cycles, all negative**, plus eight correctly
rejected frozen negative controls. Evidence: `dlmm_cycle_probe_20260907.jsonl`.

On the requested fresh five-minute DLMM retry, ten fresh-state passes produced
200 full-cycle screens: 199 completed negative and one rejected with native
`InsufficientInAmount`. No positive screen; best configured net was
**-0.000080831 SOL**, best actual diagnostic-floor net **-0.000011831 SOL**.
All 78 eligible delayed negative controls rejected. Evidence:
`dlmm_retry_20260907.jsonl`. A historical winning receipt did not reproduce
under the current state and our unchanged capital/fee assumptions.

The matching fresh retry on the closest Raydium route (Elon4AfD,
CLMM `AVAn1sReNx8medTeaZSu5KfHcJEMjhreeFkuLS7rmaZk` versus standard pool
`H6QKN2x9bCP27auuAWB4RKE9T2YjkMYXD3jWJWvKxZRV`) covered both directions
and six quote sizes. It finished its last pass at **310.86 seconds**:
**600 complete cycles across 547 distinct simulation slots**, zero execution
errors and zero positive screens. Best configured net: **-0.000077439 SOL**;
best actual diagnostic-floor net: **-0.000008439 SOL**.
Evidence: `raydium_retry_20260907.jsonl`.

Of the DLMM retry's 78 delayed rejections, 76 reached the final balance guard
and two rejected earlier in native execution. All 39 +2/+5 pairs preserved
their transaction hash. These failures were not relabelled profitable or
discarded from coverage. The diagnostic fee floor still does not establish
live inclusion or failed-submission economics.

All probes stopped; no transaction was signed or submitted. The wallet remained
at **0.562213411 SOL**, temporary accounts remained absent, and durable status
had no active positions, pending cleanup or unresolved buys. The live bot stayed
disabled. These extensions used disposable experiment tools; production source,
configured caps and repository runtime dependencies were unchanged.

## Capital sizing across Solana, Polygon and Robinhood Chain

The 2026-09-11 research adds **read-only capital analysis**, not funded Polygon
or Robinhood bot adapters. No transaction was signed or submitted; live limits
and repository runtime dependencies were unchanged. The funded test wallet
remained at **0.562213411 SOL** and the live bot remained disabled.

Run the retained tools without a signer:

```bash
uv run learning-examples/token-lifecycles/evaluate_solana_capital.py --self-check
uv run learning-examples/token-lifecycles/evaluate_evm_capital.py --self-check
uv run learning-examples/token-lifecycles/evaluate_capital_requirements.py --self-check

# RPC-only file, never the root .env or a wallet secrets file.
# It needs only SOLANA_NODE_RPC_ENDPOINT=https://api.mainnet-beta.solana.com
uv run learning-examples/token-lifecycles/evaluate_solana_capital.py \
  --env-file .state/configs/capital-research-rpc.env \
  --sizes 0.01,0.05,0.1,0.25,0.5,1,2,5,10,20,50,100 \
  --minutes 5 --out solana_capital_new.jsonl
uv run learning-examples/token-lifecycles/evaluate_evm_capital.py \
  --chain polygon --token 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359 \
  --sizes 1,10,100,1000,10000,50000,100000 \
  --minutes 5 --out polygon_capital_new.jsonl
uv run learning-examples/token-lifecycles/evaluate_evm_capital.py \
  --chain robinhood --token 0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168 \
  --sizes 0.001,0.005,0.01,0.05,0.1,0.5,1,5 \
  --minutes 5 --out robinhood_capital_new.jsonl
```

Outputs must be new files. The EVM probe verifies chain IDs **137 / 4663**,
contract code, factory identity, token decimals and distinct fee-tier pools.
Quotes are pinned to a canonical block hash. Its buffered quoter-transaction
gas cost is a **proxy**, not the cost of executing an atomic swap route;
unknown fees remain unknown, never zero.

Initial five-minute windows and later 90-second windows produced:

| Scope | Modeled size | Valid route quotes | Fee-positive |
|---|---:|---:|---:|
| Solana, Raydium standard pools | 0.01–100 SOL | 123,696 | 0 |
| Polygon, Uniswap V3 WPOL/USDC | 1–100,000 POL | 561 | 0 |
| Robinhood Chain, Uniswap V3 WETH/USDG | 0.001–5 ETH | 565 | 0 |

All six windows were **partial**: deadlines and transport/quote errors are
retained in the tapes, not counted as negative quotes. These are correlated
observations, not independent trades or complete market coverage.

For Solana's **684 captured banks / 10,308 ordered route-bank pairs**, the
continuous constant-product bound also covers amounts between the size-grid
points. Its largest gross upper bound after pool fees was **787 lamports**,
below even one **5,000-lamport base signature fee**. Increasing capital cannot
make those recorded standard-pool states profitable. This says nothing about
other venues or future states.

The earlier profitable **61.097348396 SOL DLMM receipt** was checked separately
at larger sizes. The initial current probe had 48 valid quotes and 72 liquidity
errors; expanding the per-direction bin-array limit from 12 to 64 produced 42
valid quotes and 78 liquidity errors across six later banks. No valid quote was
profitable. The historical input size failed in both directions in all six
expanded-bin snapshots. Captured array counts were 4/15 and 1/5, below the
expanded limit. Liquidity errors are not classified as losses or full execution
proofs. Reproducible SDK source and dependency lock:
`capital_dlmm_source_20260911.zip`.

### Conditional funding, not a deposit recommendation

The calculator solves
`N * (p * principal * net_return - (1-p) * failed_cost) - operating_cost`
against the daily target. With **100 independent attempts/day**, **80% successful
execution**, **$0.05 per failed attempt**, **$5/day operating costs**, one
concurrent position and a **20% reserve**, successful cycles must average
**$12.575 net each** to target $1,000/day.

| Assumed net return per successful cycle | Total funding including reserve | SOL equivalent |
|---|---:|---:|
| 0.01% | $150,900 | 1,510.43 |
| 0.05% | $30,180 | 302.09 |
| 0.10% | $15,090 | 151.04 |
| 0.25% | $6,036 | 60.42 |
| 0.50% | $3,018 | 30.21 |

SOL equivalents use the recorded **$99.905/SOL** reference, not a fixed exchange
rate. At the 0.25% scenario, the observed wallet would need **59.855183116 more
SOL**. These assumptions are **unvalidated**; the historical actor cohort had
5 successes out of 300 attempts, not 80%. Native SOL/POL/ETH equivalents are
alternative allocations of the same capital, never additive budgets.

Entry requires a fully executable size, return to the original asset after
all costs, and independent forward evidence supporting both net return and
frequency. No current candidate passes that gate, so **no funding increase is
recommended**. A repeatable $1,000/day model remains unvalidated.

Decision, exact amounts, limitations, verification and evidence SHA-256 hashes:
[`capital_research_20260911.json`](capital_research_20260911.json).
Scenario inputs: `capital_requirements_sensitivity_20260911.json`.
Network, contract and spot-price sources: `capital_networks_20260911.json`.

## Live-trial lessons that became fixes in `src/`

- Cleanup right after a sell can hit a lagging RPC node ("mint not found");
  it now trusts the token program it just traded through and journals any
  pre-submission failure so `--status` shows it.
- The zero-RPC buy path could not prove ATA ownership for cleanup; the
  pre-buy baseline is now read from the confirmed receipt. Missing token-balance
  lists or owner metadata remain unknown. An omitted account endpoint is zero
  only when its indexed native balance proves creation or closure.
- A sell rejected at preflight (pump 6003) was filed as an unknown outcome
  and the expiry proof was a stub, freezing a position for an hour while
  the coin collapsed. Preflight rejections now release the wire for an
  immediate re-quote, expiry is proven from finalized history, and expired
  cleanup wires are rebuilt.
- Confirmed-but-unpriced sells now retain their pending signature and position,
  without resubmitting or starting cleanup. Normal and emergency recovery use
  actual receipt proceeds, never the saved trigger or entry price. An invalid
  successful seller price likewise stays unresolved.

Offline regression check (no RPC, signing, or funds moved):
`uv run --offline --no-sync learning-examples/verify_receipt_accounting.py`.
It covers missing inventory metadata, valid new/existing balances, and restart
recovery from unreadable to readable sell proceeds. These are accounting fixes,
not evidence of profitable strategy learning or automatic policy activation.

## Durable forward trade evidence

New live coordinator runs retain evidence in the existing wallet SQLite ledger:

- `evidence_profiles`: content-addressed execution kind, allowlisted effective
  settings, and SHA-256 hashes of Python sources, vendored IDLs, `pyproject.toml`
  and `uv.lock` when present. Hashing happens at startup, not on token detection.
  RPC/WSS URLs, private keys and Geyser credentials are excluded. This is a source
  fingerprint, not proof of installed dependencies or independent preregistration.
- `submissions.evidence_profile_id`: the profile bound when exact wire bytes are
  first reserved. Reuse/recovery preserves it, including historical `NULL`.
  A new observer's configuration never becomes an old submission's provenance.
- `evidence_events`: coordinator entry/exit decisions, trade results, chain
  reconciliation observations, closed-position context, and released prepared
  submissions. These survive removal from the active-position journal.
  Event profiles identify the **observer**; join the referenced signature to
  `submissions` for the original submission profile. Position snapshots retain
  the entry position ID and pre-close context.
- `evidence_receipts`: canonical public `getTransaction` responses already read
  for tracked signatures, with their requested `confirmed`/`finalized` commitment.
  Successes and reverts retain the actual `meta.fee` separately from fee budgets.
  Missing/invalid fee metadata remains `NULL`. No extra RPC is made for capture,
  and confirmed receipts are not relabeled finalized.

Identical observations are idempotent; changed observations are retained rather
than overwritten. **Rows are observations, not independent trades**: group by
signature and reconcile receipt changes before any cost aggregation. This is
not a complete listener census or an audit of manual scripts/other wallets.
Unsuccessful result rows do not turn requested prices or amounts into fills.
Raw provider exception strings stay out of evidence because they can contain
credential-bearing URLs; structured status and public receipt errors remain.

Evidence writes precede local closure/removal. A persistence failure is fatal,
not a missing RPC receipt, and leaves recoverable work in place. Prepared-release
history and release of its risk reservation commit atomically. `*_budget_lamports`
remain conservative reservations, not paid fees. Receipts improve future cost
attribution but do not by themselves establish all-in net PnL or capacity.

Paper/counterfactual and unsigned-simulation tapes keep their existing separate
manifests and qualifications; they are not imported as live fills. Evidence kinds
`live`, `dry_run`, `simulation`, and `paper` cannot share a profile identity.
The bot's dry-run mode remains an authorization check, not a paper execution
engine, and does not open the live ledger. No learned policy is activated by
these records. Existing live history has **not** been backfilled or relabeled.

Offline lifecycle proof, including a real SQLite write rejection and reopen:
`uv run --offline --no-sync python -B learning-examples/verify_trade_evidence.py`.
It uses only synthetic receipts and temporary storage, without keys, signing,
network access, or reads/writes of the live state.

### Read-only reconciliation

Audit a retained ledger without loading bot configuration, contacting a provider,
or running startup/status migrations:

```bash
uv run --offline --no-sync python -B learning-examples/token-lifecycles/summarize_trade_evidence.py LEDGER.sqlite3
```

The reader uses SQLite `mode=ro`, `query_only`, and a transaction snapshot.
Committed WAL rows remain visible; missing evidence tables stay missing.
SQLite still uses its normal reader locking/WAL bookkeeping. A missing path is
an error, never a new database. Read/schema failures and fatal data errors exit
nonzero. A valid report containing evidence gaps or integrity failures exits
zero, **not** a readiness signal.

The JSON report separates original submission kind from receipt-observer kind.
It checks stored content hashes and reconciles receipt identity, chain status,
slot, available message/balance facts, fee payer, and actual `meta.fee` by
signature. Contradictory chain facts or invalid receipt identity/integrity withhold
that signature's fee. Missing/invalid fee fields remain unknown and cannot
strengthen finality.
Practice receipts cannot supply live costs, and a later observer cannot supply
an old submission's missing provenance. Profiles/source hashes establish stored
self-consistency, not independent authenticity.

Report **version 2** also checks live event claims against the reconciled
submissions. `event_issues` exposes terminal-status/slot contradictions, mismatched
intent IDs, gate/action contradictions, and closures with missing, reverted,
same-signature, or different-wallet entry links. The transaction's `receipt_slot`
is available only when its receipt outcome is consistent. Actual observed fees
remain in the known subtotal even when a separate event claim is false.

Earlier `unknown` or local `failed` observations may converge to later success.
`success: false` with chain status `success` can mean unavailable accounting, not
a revert. A decision may fail before submission; no receipt is invented for it.
Position snapshots are captured before local removal, so `is_active: true` is
expected in closure evidence. Changed observer profiles do not relabel entry
submissions. Expiry is checked against the retained ledger, not independently
proved on chain.

These checks validate declared links and outcomes, **not** traded token identity,
fill quantities/prices, event ordering, or decision quality. Event counts still
count stored observations, including semantically inconsistent claims.
The [lifecycle verification](lifecycle-evidence-verification-20260916T043749Z.json)
retains the previously missed success-versus-revert probe and the corrected result,
plus the offline regression and real-CLI checks.

`confirmed_subtotal_lamports` and `finalized_subtotal_lamports` are disjoint:
each signature uses its strongest observation **containing the fee**.
`known_subtotal_lamports` is their potentially partial sum, including reverts.
`complete_finalized_total_lamports` stays `null` when attribution, integrity,
coverage, or finality is uncertain. This is network-fee accounting within the
snapshot, not all-in PnL, a wallet census, validated learning, or policy activation.
Event counts are observations, not trade counts.

The [retained version 1 legacy audit](trade-evidence-audit-20260916T040623Z.json) records
26 submissions (24 successful, two expired), all without modern submission
profiles or archived receipts. Their fees remain unknown: the zero attributable
subtotal does **not** mean zero fees paid. The artifact fingerprints the reader
and records offline checks; the ledger, trade log, and earlier learning-loop
verification artifact remain unchanged.

## Own-live accounting and attribution

The September 3 records for wallet `9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe`
join [the trade log](../../trades/trades.log) to its durable transaction ledger
and the retained 8h market tape. The ledger has eight successful buys, eight
successful sells and eight successful cleanups, plus an expired Ceuta sell and
an expired sol cleanup. Twenty-four successful transactions are not twenty-four
winning trades. This is the retained bot cohort, not an exhaustive wallet audit.

Amounts below are `round(logged_price * token_amount * 1e9)` converted to SOL.
They are **logged swap cash flows, not audited net wallet returns**. Retained
own-wallet trade events independently corroborate MH, wind, ANSEM and sol after
their venue-fee differences; Ceuta's late sell is absent from the market tape.
Older recovered sells could be logged using their saved trigger price. Current
recovery requires actual proceeds, but historical rows are not repaired by that
fix. Those historical ledger records did not retain finalized balance metadata
or observed network fees; forward evidence capture does not repair them.

| Trade | Logged buy SOL | Logged sell SOL | Logged swap spread SOL |
|---|---:|---:|---:|
| READ | 0.000099999 | 0.000097526 | -0.000002473 |
| FROG | 0.000099999 | 0.000053768 | -0.000046231 |
| Rizo | 0.009038680 | 0.007246511 | -0.001792169 |
| MH | 0.009204787 | 0.004567127 | -0.004637660 |
| wind | 0.007148396 | 0.005342490 | -0.001805906 |
| ANSEM | 0.007493355 | 0.008472657 | +0.000979302 |
| Ceuta | 0.007314670 | 0.000268977 | -0.007045693 |
| sol | 0.007155892 | 0.004417524 | -0.002738368 |
| **Total** | **0.047555778** | **0.030466580** | **-0.017089198** |

Successful submissions' network/cleanup fee budgets total 0.000556000 SOL
(0.000325000 for the five gated cycles). Subtracting those budgets yields
estimates of -0.017645198 SOL overall and -0.015573325 SOL for the gated subset;
the budgets are not a `meta.fee` audit. The two expired wires' combined
0.000032000 SOL fee reservation must not be counted as a paid chain fee.
Venue fees are already reflected in the logged swap amounts; do not subtract
them twice. Returned account rent is recovered capital, not trading profit.

Full scans of all three legacy tapes found our five gated mints only in
`lifecycles_8h.jsonl`: MH line 485, ANSEM 880, wind 892, Ceuta 906 and sol 2545.
READ, FROG and Rizo have no matching retained lifecycle record. These older
tapes lack continuous-coverage guarantees, so the following describes recorded
events, not proof that no other market activity occurred:

- **ANSEM:** between our buy at creation+5 and sell at +13, the only other
  recorded trades were three Mayhem-vault buys and two sells: net quote inflow
  0.081856424 SOL. This was a favorable path that we actually exited, not a
  hypothetical peak. The entry rule did not establish a way to predict it.
- **MH:** the gate counted the protocol vault as its one noncreator buyer
  at +2, with only 0.077 SOL real liquidity. We bought at +4 and sold at +16.
  All intervening recorded trades were vault activity, with net quote outflow
  0.042267040 SOL; no creator sell was recorded. Excluding the vault from the
  buyer count and requiring at least 0.1 SOL blocks that exact entry, but does
  not predict the vault's next direction.
- **wind / sol:** net recorded vault outflows during our holds were
  0.036041457 and 0.073473704 SOL respectively. On sol, the pre-entry other
  buyer also sold its entire acquisition before our exit. These are observed
  selling flows, not evidence of a creator rug, sandwich or network congestion.
- **Ceuta:** initial vault sells followed our entry. The losing exit quoted
  0.004805913 SOL at 21:16:04 UTC, then failed preflight with 6003 and was
  classified as unresolved. The emergency exit roughly an hour later logged
  0.000268977 SOL. Correct rejection classification restores retry eligibility;
  it does not prove what an earlier retry would have realized.
- **READ / FROG / Rizo:** exact market attribution is missing. FROG's early
  executable quotes were below its fee-aware profit target, and its emergency
  exit was about 14h32m after entry; the reason for that long hold is not
  established here. Rizo explicitly waited 15 seconds before buying, but
  attributing its entire loss to that wait is not supported.

The same other address, `7YqYi5iLsTwYo8WyDxiWoRkju3M3Xk5e81F9Tiip5pRP`,
bought about 0.000722 SOL before our entry in both ANSEM and Ceuta. One extra
buyer therefore did not distinguish our winner from our largest loss.
Explaining the vault's subsequent flow is retrospective attribution, not a
pre-entry predictor. No improvement in live strategy expectancy has been
demonstrated. The saved readiness profile remains disabled; its static gate
and optional, unconfigured flow exits are not a validated replacement strategy.

## Frozen meme entry/exit replication (2026-09-13)

`evaluate_meme_exits.py` tests three predeclared spot-flow entry rules against
15-minute, 60-minute and close-triggered trailing exits. Retained Binance spot
one-minute archives cover May/June WIF, BONK and PENGU, with SOL as benchmark:
351,360 rows including the benchmark. FARTCOIN and POPCAT archive requests
returned 404; missing coverage is not evidence of no trading. CSV checksums and
the frozen protocol are in `meme-exit-history-20260913/manifest.json`. The directory
was renamed after scoring to follow repository naming rules; the frozen
manifest retains its original capture paths. Archives resolve relative to the
manifest, and its bytes, CSV payloads and selection lock are unchanged.

```bash
uv run learning-examples/token-lifecycles/evaluate_meme_exits.py --self-check
uv run learning-examples/token-lifecycles/evaluate_meme_exits.py \
  --stage train --manifest learning-examples/token-lifecycles/meme-exit-history-20260913/manifest.json \
  --out /tmp/meme-exit-training.json
uv run learning-examples/token-lifecycles/evaluate_meme_exits.py \
  --stage validate --manifest learning-examples/token-lifecycles/meme-exit-history-20260913/manifest.json \
  --policy /tmp/meme-exit-training.json --out /tmp/meme-exit-validation.json
```

Use new output paths. May-only selection is locked before June is opened;
changing the evaluator or its reused helpers invalidates that lock. Entries
wait 60 seconds after a completed signal, with a predeclared 120-second
sensitivity. Trailing exits use completed minute closes and another 60-second
execution delay, never inferred high/low ordering or perfect stop fills.
Unpriced outcomes remain explicit and overlapping positions are excluded.

May selected `flow_breakout` with a 15-minute hold: 49 priced attempts averaged
**−0.871%** at 100-bps modeled roundtrip cost. June then produced **44 priced /
3 unpriced** attempts across all three assets: **−0.148% gross**, **−1.142% net**
at 100 bps; the day-block 95% interval was **[−1.404%, −0.878%]**. Results remained
negative at 20 bps, without the best day, and with the longer entry delay.
No heldout alternative was reranked. This rejects the selected rule; it does
not demonstrate an edge in another rule or a profitable short strategy.

These are disjoint retrospective CEX marks on surviving tokens, not prospective
evidence or executable DEX fills. The earlier July–September study stays frozen.
Native checks found 25-bps Raydium fees for the selected WIF/FARTCOIN pools and
successful unsigned entries, but no successful profitable guarded cycle.
At the exercised 65,000-lamport fee per transaction, two separate transactions
plus pool fees cost about **180 bps at 0.01 SOL**, before additional losses.
BONK's selected Orca pool has no adapter in the research executor. Robinhood
CASHCAT quotes worked, but unfunded router payment reverted; neither those
quotes nor the Solana research adapters are funded bot integrations.

Saved selection and outcomes: `meme_exit_training_20260913.json`,
`meme_exit_assessment_20260913.json`. Native observations and limitations:
`meme_native_execution_20260913.json`. Integrated decision and verification:
`live_readiness_assessment_20260913.json`. The funded bot remains disabled.

## Creation-slot execution continuation (2026-09-13)

```bash
uv run learning-examples/token-lifecycles/probe_creation_execution.py --self-check
uv run learning-examples/token-lifecycles/probe_creation_execution.py \
  --env-file .state/wallets/live-readiness.secrets \
  --seconds 180 --max-candidates 12 --out <NEW.jsonl>
uv run learning-examples/verify_durable_position_recovery.py
```

The probe selects only RPC/Geyser settings from the explicit file, uses a public
protocol-recipient payer and default signatures, and never submits. Quote and
fee ceilings remain 13,000,000 and 250,000 lamports; priority is 200,000
micro-lamports/CU. All HTTP RPC starts count toward the 200-start ceiling.
Read-only HTTP 429 retries use the bounded policy below; each physical attempt
counts separately, including failed attempts.
Candidate-owned RPC starts are at least 500 ms apart; fee/blockhash background
work uses the production limiter. Fee simulations reuse the actual cached
blockhash because RPC replaces it. Both fee schedules must attest normally.
This avoids spending the entire request budget on 104 fee simulations plus
104 redundant blockhash reads; it does not bypass a fee or freshness check.

The probe records receiver delivery, decision, actual task dispatch, first
consumer execution, assembly, and linked RPC request/header/body/response
clocks. Receiver delivery is **not physical network arrival**; `minContextSlot`
is a bank lower bound, not evidence of transaction inclusion. Unsigned simulation
remains a conditional current-bank observation, never an executed fill.
`http_call_monotonic` separately marks the HTTP call after request-evidence
writing, so logging time is not silently attributed to provider response time.
Decoded creations beyond the cap in the final delivered update retain their
identities and skip reasons; nothing after `receiver_closed` is observed.
Typed and raw RPCs share the same read-only method and unsigned-signature guards.
A queued request rechecks the halt after pacing; final-audit reserve semantics
remain unchanged. Accepted HTTP bodies are bounded to 8 MiB; interrupted or
oversized response hashes explicitly cover only the received decoded prefix.

The offline verification and acquisition prerequisites are recorded in
`execution_evidence_readiness_20260915.json`. This instrumentation does not
authorize a replacement capture: the provider's actual plan/node, account
limits, limiter scope, remaining quota and competing traffic remain unknown.

Three captures remain separate:

- `creation_execution_20260913T113016Z.jsonl`: startup exhausted its request
  budget before the observation window. No market conclusion.
- `creation_execution_20260913_paced_foreground.jsonl`: seven verified entry
  attempts, four native successes and three RPC `-32016` failures. Seven
  precommitted exit marks were lost to a diagnostic emitter argument collision.
  The collision is fixed and reproduced offline; those old marks remain missing.
- `creation_execution_20260913_complete_marks.jsonl`: 12 candidate records,
  including seven unverified creation-like records skipped. Five verified buys
  were simulated: four successes, all in the creation slot's **bank**, and one
  native 6002 rejection. Successful assembly had zero trade-triggered reads,
  median 1.23 ms assembly and 216 ms RPC response. All five 10-second marks
  were retained: three priced, one rejected sell quote, one failed-entry mark.

The three priced observations stayed negative even under an optimistic bound:
remove **all** newly funded account balances from the entry debit and charge
zero exit-network fees. Returns were −3.074%, −3.010%, and −2.736% (mean −2.940%).
This is not an expectancy estimate: only three marks are priced, the other
outcomes are not zeros, the hypothetical buy never changed future bank state,
and a fixed 10-second mark does not test the configured intra-window tp/sl exits.
Same-slot simulation is not leader inclusion or an executed fill; local timing
is not the cloud latency measurement documented above.

The earlier small-quantity probe bought 20 whole tokens, sold all 20, and
verified the new base ATA closed. On DELULU, the public protocol payer used 212,165 CU and
lost 1,351,217 simulated lamports. Repeating with only the actual wallet's
**public key** used 198,715 CU and lost 5,017 lamports, with no newly funded
accounts left. Both were unsigned and unsubmitted. That 1,346,200-lamport
startup difference is why cold-payer rent must not become a recurring strategy
loss. Neither run proves a sell after a future market move.

`verify_durable_position_recovery.py` reopened a real temporary journal and
SQLite ledger in four fresh processes. Unknown work preserved its exact
intent/signature and held inventory without a buy, listener or submission.
A synthetic reverted receipt produced one durable fresh retry; the real seller
then stopped on unavailable pool metadata. Reservations survived without reset
or duplication. This is shared LetsBonk lifecycle evidence, not signed-wire
replay, Pump.fun fee readiness, or a successful funded recovery.

Evidence: `full_liquidation_20260913.json`, `durable_recovery_20260913.json`,
and `creation_readiness_continuation_20260913.json`. Earlier entry/exit holdouts
remain frozen. The bot remains disabled; wallet balance is 0.562213411 SOL and
durable status has no active or unresolved work. No live order was authorized.

## Configured-size and source-bound cohort (2026-09-13)

```bash
# Public-key-only native simulation; no dotenv, signing or submission.
uv run learning-examples/simulate_v2_trades.py <MINT> \
  --configured-size --payer 9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe
uv run learning-examples/verify_durable_position_recovery.py --platform pump_fun

# For a NEW preregistered window, never overwrite the frozen paths below.
uv run learning-examples/token-lifecycles/record_lifecycles.py \
  --minutes 10 --idle 20 --max-age 40 \
  --env-file .state/wallets/live-readiness.secrets --out <NEW.jsonl>
uv run learning-examples/token-lifecycles/replay_gate.py <NEW.jsonl> \
  --lock <PRECOMMITTED_LOCK.json> --out <NEW_REPORT.json>
```

**Configured quantity is now exercised natively.** The first three SOL coins in
the initial tape's write order were selected before any result. Two refused
the pre-simulation sell quote because it exceeded existing real reserves;
neither was a native transaction failure. The third,
`22xWTUDHqegFqzcYTbwfwwycf2ZzSZfy4nVHoH4opump`, passed an exact
250,000-token buy and atomic full sell/close with the real wallet's **public
key only**. Buy consumption was 103,335/140,000 CU. The atomic transaction used
177,149/250,000 CU, 1,214/1,232 packet bytes, and a 55,000-lamport fee. Its
new token account closed; simulated native balance changed by −314,421 lamports.
That delta is not automatically trading PnL. The successful sell instruction
used 72,350 CU inside the atomic transaction, not an independently submitted
110,000-CU sell. The standalone sell's 3012 remains missing-inventory/layout
evidence only. All transactions had default signatures and a 16 MiB loaded-data
limit. No quote, fee or session cap was raised.

**Pump.fun recovery refuses safely when attestation is unavailable.** Four
fresh-process phases exercised seed state, unavailable fee account, unavailable
native attestation, and reopening the unavailable-attestation state. Exact
durable rows and reservations survived without queue/listener/monitor admission,
signing or submission. This uses a synthetic unfunded identity and synthetic
ledger amounts; it does not claim a ready or funded Pump.fun recovery.

**The first ten-minute cohort was incomplete, not negative.**
`creation_profile_tape_20260913.jsonl` retained six coins and fourteen curve
trades, but delivered heads advanced only 126 slots, with a maximum 133.164-second
receipt gap. A short arrival audit later received normally, and profiling did
not show a decoder bottleneck; the stall's cause is unproved. The tape and its
original lock remain unchanged. Its report explicitly records missing scoring
source bindings and no precommitted head-gap bound.

**A new fully source-bound window retained 150 creations and 5,686 curve
trades.** `creation_profile_fresh_lock_20260913.json` was written before
`creation_profile_fresh_20260913.jsonl` began. It fixed the existing gate,
250,000-token quantity, one-slot entry/exit delays, one-second polling, net TP,
SL/max-hold precedence, slippage floor, and 33k/27k/5k buy/sell/cleanup fee
estimates. It also fixed a one-second maximum head gap and the native-attested
canonical fee account. The scorer rejects changed pinned sources or missing
fee provenance; the frozen tape is not a policy-tuning set.

The completed window had 1,879 processed heads over 599.370 seconds, including
38 gaps above one second. Under that conservative coverage rule, 129
coin records were marked with gaps, even if an earlier portion might be usable.
The fixed model classified 126 non-entries (97 non-mayhem, 27 unsupported quote,
one slot-window expiry, one timeout) and **24 unpriced observations**. No model
entry or fully priced configured exit was established. Unknowns are not zero
losses; there is no return or expectancy estimate.

The model assumes an independent handler starts immediately on receipt. Real
queue admission, scheduling, finality, leader inclusion, hypothetical market
impact, continuous fee attestation and failed-exit retries are not observed.
The lifecycle decoder also does not perform the creation probe's full canonical
instruction/event provenance admission. Graduation before modeled closure stays
unpriced rather than treating the last curve quote as a PumpSwap fill.

Evidence: `configured_liquidation_20260913.json`,
`durable_pump_refusal_20260913.json`, `creation_profile_incomplete_20260913.json`,
`creation_profile_fresh_report_20260913.json`, and
`configured_operator_state_20260913.json`. The integrated manifest is
`configured_readiness_continuation_20260913.json`. Eight targeted tests, both
four-process recovery checks, source/fee refusal smoke checks, and the existing
v2-layout, transaction-status, creation-probe and tp/sl checks passed.

**Still no-go for live trading.** Final preflight was ready, but readiness is
not strategy evidence: the bot remains disabled, balance remains
0.562213411 SOL, and there is no active, pending or unresolved work. Historical
risk-session counters were preserved. No live order was authorized or submitted.

## Prospective actor and creation studies (2026-09-13–14)

The `empirical-0913233451` continuation kept provider traffic in
`chainstack-pumpfun`, `europe-west2`. Captures used provider-only projections and
the public payer, not wallet keys. No transaction was signed or submitted, no
balance/storage override was used, and no live amount, fee or session cap was
raised. Frozen source archives and failed/censored attempts remain evidence;
they were not overwritten by better-looking replications.

### Native public-route comparison

The one-hour two-Orca study completed all **240 scheduled windows**. State and
both simulations qualified at the same finalized slot in **239/240**; one
window's simulations were one slot older than its state read and remained
ineligible. All **480 native cycles rejected at the final profit guard**.
The chronological halves had 120/120 and 119/120 eligible windows respectively,
with zero threshold opportunities or episode starts.

Native `Traded` event decoding priced all 480 component executions without a
local constant-product approximation. Every component was negative under both
the fixed 75,000-lamport fee/tip scenario and the preregistered counterfactual
6,000-lamport scenario. At 6,000, the best A→B result was **−1,695 lamports** and
the best B→A result **−10,774**. The best observed gross swap difference was only
4,305 lamports, below the observed 5,000-lamport base signature fee. This last
comparison is arithmetic on the retained events, not another prospective arm.

These are rolled-back simulation outcomes, not money lost by the payer or
proof of future opportunity absence. Finalized-slot equality is not inclusion.
The complete primary report replayed exactly under its frozen source.
Evidence: `empirical-0913233451-menu-orca-full_verification.json`,
`empirical-0913233451-menu-orca-full_report.json` and
`empirical-0913233451-menu-orca-full_fee_sensitivity.json`. The exact secondary
analysis recipe is retained in `empirical-0913233451_menu_fee_analysis_source.json`.

### Two-hour C versus D2 paper comparison

The producer completed its frozen 60-second warmup, 7,200-second admission and
120-second drain. It retained 1,127 creations, including **1,116 admitted
recorded mints**, 1,073 supported mints and 23,372 processed heads.
The raw artifact contains **512,880 transaction envelopes / 2,105,706,780 bytes**.
Independent native decoding matched all 3,622 report state references and all
1,127 recorded creations; C uses the last trade event in the complete creation
transaction, never guessed initial reserves.

**Capture qualification is still `partial_censored`.** Three admission head
gaps leave the creation denominator unknown. The external freshness guard
passed (initial reference lag −8 slots, final +2), but explicitly reports
`coverage_complete: false`. Local replay matched every row, economic summary,
coverage result and source binding; only the host-specific absolute journal
path differs. Process success is not complete market coverage.

The unchanged model uses 250,000 tokens at six decimals, a 13,000,000-lamport
entry cap, 65,000 lamports of network/cleanup estimates, a 10-second hold and
one-slot exit delay. Its fee account is a **prior-native-attestation frozen
scenario**, not continuous/current fee attestation.

| Held-out arm | Modeled entries | Conditionally priced | Entered but unpriced | Priced-only subtotal, SOL |
|---|---:|---:|---:|---:|
| C | 539 | 235 | 304 | +0.153756879 |
| D2 | 513 | 207 | 306 | −0.128300147 |

Among the **183 mints priced in both arms**, C sums to −0.038031806 SOL and D2
to −0.117293146; D2−C is −0.079261340. This is a selected common subset, not a
replacement denominator for either arm. D2 shifts both entry and exit times,
so the difference is not a pure estimate of entry latency.

C's priced-only holdout mean has a time-block-bootstrap interval spanning
zero, before accounting for missing observations. An average net loss of only
505,779.207 lamports across its 304 entered-unpriced observations would erase
its positive priced subtotal; this is a sensitivity calculation, not an
assertion that those losses occurred. Conditional positive tails cannot price
the missing inventory.

A conservative bookkeeping scenario that never releases capital from
unpriced exits reaches 4.470792158 SOL of quote commitments for C and
4.492159558 for D2, versus the 0.562213411-SOL public payer. These exclude other
liabilities and are **not proven minimum actual capital requirements**:
censored exits are unknown. The model does not enforce a shared funded wallet,
the live session cap, actual queue admission or leader inclusion. Summing
independent-handler quotes is not a funded strategy or daily income.

Evidence: `empirical-0913233451-paper-full_verification.json`,
`empirical-0913233451-paper-full_economics.json` and
`empirical-0913233451-paper-full_coverage.json`. The original report, all six
capture artifacts and source archive are retained.

### Successful actors versus complete observed costs

The accepted one-hour Solana actor replication retained 16,625 receipts.
Its 50-minute holdout produced these **partial known components**, not complete
wallet PnL:

| Frozen payer prefix | Qualified cycle component less observed failed fees, SOL |
|---|---:|
| `4XBqViD1…` | −0.340890116 |
| `78Bo7xx…` | +0.000135754 |
| `CQwT1by…` | −0.160110960 |

Removing `78Bo7xx…`'s best trade/slot makes its component −0.002307455 SOL.
Unpriced inventory, other native changes and unobserved attempts remain
unknown. The separate censored Menu sample contains 342 qualified target
cycles totaling +0.009323371 SOL after observed fees/payments, but 321/342 use
unmapped venue programs. Its ten inputs at or below 0.01 SOL total only
31,232 lamports; an additional 5,000 lamports each would make that subset
negative. Observing fee-covered successes does not replicate the actor's
execution, capital, custody or full strategy.

The full Polygon study completed 120 sparse windows, **480 sampled blocks out
of 2,383 block numbers in the covered span**, 44,389 receipts and 422 trace
pairs. Its frozen exclusions remain unchanged. Four post-hoc native-wrapper
round trips have a combined payer-plus-executor arithmetic surplus of
0.151434833163665290 POL, but common ownership and complete actor PnL are
unproved; one executor cluster's observed failed fees exceed its two traced
successful surpluses.

The apparent +72.55-POL example was a **withdrawn-WMATIC double count**, not a
native profit: the wrapper emitted `Withdrawal` without a zero-address burn
`Transfer`. The current analyzer leaves combined native/wrapper cashflow
unknown when wrapper balance mutations are unresolved. Exact native deltas
and Transfer components remain visible separately; frozen reports are not
rewritten. The original Robinhood tape still fails its frozen pacing check
(337.790293 ms versus a 349-ms acceptance minimum); a later transport repair
does not retroactively certify it.

Evidence: `empirical-0913233451_solana_full_analysis.json`,
`empirical-0913233451_solana_full_mechanisms.json`,
`empirical-0913233451_menu_mechanism_analysis.json`,
`empirical-0913233451_polygon_full_evidence.json` and
`empirical-0913233451_wrapper_cashflow_verification.json`.

**No-go for live enablement or a $100/day claim.** The final native audit kept
the payer at 0.562213411 SOL with temporary accounts absent. The bot remains
disabled and durable work remains resolved; historical risk counters were
preserved, not reset. All 18 temporary Jobs, three provider-only secrets, the
temporary service account and the active evidence bucket were removed after
every stored object was verified locally. GCS's seven-day soft-delete
protection remains intact; this is not a claim of immediate permanent purge
or zero retained-storage cost.

The integrated evidence index and qualifications are in
`empirical-0913233451_research_report.json`; resource absence and object
checksums are in `empirical-0913233451_cloud_cleanup.json`.

## Account-backed creation exit marks (2026-09-14)

The C/D2 event study left 561 C and 560 D2 entered rows unpriced because their
latest trade-event state was stale. `record_creation_account_marks.py` adds
native account observations while reusing the lifecycle recorder and unchanged
event-entry model. `evaluate_creation_account_marks.py` writes a separate
account-mark report and the unchanged `.event-baseline.json` comparison.

```bash
# Offline; these checks make no provider requests.
uv run learning-examples/token-lifecycles/decode_provider_response.py
uv run learning-examples/token-lifecycles/record_creation_account_marks.py --self-check
uv run learning-examples/token-lifecycles/evaluate_creation_account_marks.py --self-check

# Acquisition belongs on the bounded europe-west2 runner, not the workstation.
python learning-examples/token-lifecycles/record_creation_account_marks.py \
  --base-lock paper-lock.json --marks-lock marks-lock.json \
  --provider-json /var/run/provider-config/provider.json --out NEW.jsonl

# Replay is offline; preserve the lock's companion tape/journal paths.
python learning-examples/token-lifecycles/evaluate_creation_account_marks.py \
  --base-lock paper-lock.json --marks-lock marks-lock.json \
  --tape NEW.jsonl --marks NEW.marks.jsonl --out NEW.account-report.json
```

The provider projection contains only `rpc_url`, `geyser_endpoint` and
`geyser_token`. Wallet paths, `.env` paths and extra keys are rejected.
The probe never signs, submits, creates accounts or overrides balances.
Before admission and after capture it records the public payer account.

Each native request reads the fee account plus canonical curve/mint pairs in
one `getMultipleAccounts` bank. The requested minimum slot includes the
observed head and target floors. Request starts are now at least 1,000 ms apart;
each request has an independent two-second transport timeout. Every target must receive
its response by `due_at + 2` seconds. A late member stays unknown without
cancelling its on-time peers; an expired trigger cannot schedule an exit.
Neither deadline nor the five-second exit-head wait is retried or relaxed.
Native trigger time is the actual response time, not the intended ten-second
due time. Exit marking waits for a later received head at least
one slot beyond that response's context, then another native response.
These clocks differ from event-only exits; compare timing as well as coverage.

Slot evidence now distinguishes receiver `received_monotonic` from recorder
`observed_monotonic`. RPC replay selects the latest head delivered to the
recorder at request start; freshness still ages from the original receipt,
so delayed delivery cannot make an old head fresh. Entry and exit slot/time
rules remain receipt-based. Both clocks are required by new source locks;
old captures must use their archived source, never backfilled observations.

Both research transports use `decode_provider_response.py` for bounded refusal
diagnostics. HTTP status is retained when headers arrive, even when the body
later fails. Only bounded numeric rate-limit headers, parsed `Retry-After`,
integer RPC error codes and fixed provider-reported reason categories survive;
arbitrary failure bodies, messages and other headers are not retained.
Body classification requires complete EOF within its 8 KiB diagnostic limit.
A 429 alone does not distinguish request rate, connection limits or quota.
Only the explicit HTTP 429 policy permits a retry; diagnostic message categories
do not authorize retries or convert failures into priced evidence.
New account-mark source locks bind this helper too. Missing historical headers,
bodies and clocks cannot be backfilled; old captures and locks remain unchanged.

Read-only 429 recovery is bounded to **three attempts per operation**, with
one- then two-second backoff. Parsed `Retry-After` seconds or HTTP dates impose
a minimum wait from header receipt. A required delay over five seconds, or an
invalid directive, stops recovery instead of shortening the provider's wait.
The entire operation, including admission, pacing and cooldown, is bounded to
ten seconds or its shorter existing deadline. Other HTTP statuses, JSON-RPC
errors, native simulation errors and partial/oversized responses are not retried.
The probe's shared cooldown covers foreground, background and final-audit
requests; an unhonorable cooldown leaves the audit unknown rather than sending
an early cleanup request. Cancellation propagates without dispatching another
attempt, including the Python 3.11 limiter-admission cancellation boundary.

The account recorder still enforces `due_at + 2` for each target and refreshes
the head and requested minimum slot before every attempt. Late native responses
stay recorded but unpriced; mark expiry is not an HTTP timeout. Intermediate 429s are
durable `rpc_retry` rows linked to the operation, attempt and preceding sequence;
only the final `rpc` or `miss` settles a target. Replay checks eligibility,
cooldown, deadlines, chain identity, physical request counts and audit reserve.
The reported `rpc_retries` counts extra physical requests, not waits that expired
without another send.
Recovered 429s remain visible without making an otherwise complete run partial;
expired, exhausted or missing observations remain unknown. The 8,000-request
account cap, 200-start execution-probe cap and their audit reserves are unchanged.
This policy requires new source locks for new captures; historical no-retry
captures and their locked policy are not rewritten.

Fresh account state does not prove a sell: unsupported mint extensions,
graduation, insufficient full-quantity liquidity, floor failures and missing
marks retain entered inventory with unknown net value. The fee digest must
match each native read, but the model remains a **prior-native-attestation
frozen scenario**, not a new native trade attestation or an actual fill.
Per-target diagnostics retain both the primary reason and any simultaneous
`native_reason`. The `native_errors` histogram overlaps `outcome_errors`;
do not sum the two as mutually exclusive failure counts.

Source locks must be generated in an isolated interpreter **after importing
both account-mark companions**. The base source map depends on loaded internal
modules, so bare event-scorer `--lock-schema` output is not interchangeable.
The frozen source archives retain `create_locks.py`, exact source files,
requirements, both locks and the cloud bootstrap. Reuse only the prior
`fee_input` when generating a new lock, never an old source map.

The `account-0914120619-pilot` instrumentation run completed its scheduled
60/120/30-second warmup/admission/drain. Of 57 account requests, one returned
`-32016` (`minContextSlot` not reached); it remained unknown, making the report
explicitly partial despite scheduled completion. All eight artifacts were
recovered by exact object generation and verified by SHA-256 and MD5.
Frozen-source local replay matched all rows and economics; only host-path
metadata differs. The public payer stayed at 562,213,411 lamports. The first
one-hour attempt used the pilot's unchanged source and assumptions.

That first attempt, `account-0914120619-full`, stopped after 770.125 seconds
of observed capture when request 499 received **HTTP 429**. It did not retry.
Cancellation exposed a durability bug: 50 of 159 creations remained buffered,
so replay rejected 198 target records whose mints were absent from the tape.
All seven available artifacts were recovered; the missing primary report
was not fabricated. This is **not a completed heldout sample**.

`record_lifecycles.run` now attempts its bounded final coin flush before the
terminal journal on cancellation and early failure as well as normal exit.
The regression preserves the coin while retaining incomplete/exit-130 status;
a storage-ceiling refusal stays explicitly unflushed and within the cap.
The replacement acquisition lowers the request rate from two to one per
second, without retries or wider mark deadlines. The provider's actual quota
and arrival-time window were not observed; the lower rate is conservative,
not proof that throttling cannot recur.

`account-0914120619-replacement` then exposed a different failure: one target
had only 27.658 ms left, so its deadline cancelled the entire ten-target
request after 28.145 ms despite headers arriving after 16.689 ms. This was a
**local deadline-scope bug, not established provider failure**. All 56
creations were durably flushed and all 112 arm-rows scored, proving the
cancellation repair. All eight artifacts were recovered, and frozen-source
replay matched economics with only host-path metadata differences. Neither
one-hour attempt reached holdout.

The repaired collector separates transport timeout from per-target expiry;
offline checks cover a mixed-deadline batch and preserve fatal transport
timeouts. Replay also now checks the frozen spacing policy instead of a stale
500 ms constant. The original one-replacement declaration and its failed run
remain preserved. `account-0914120619-deadline-fixed` is a separately registered
instrumentation revision with unchanged economics and deadlines, not pooled
with the truncated predecessors. The earlier `-deadline` registration was
superseded before any capture after review required simultaneous native-error
diagnostics.

The pilot also exposed a false supply invariant. Native mint supply need not
equal the curve/CreateEvent supply: holders can burn tokens independently, and
[Mayhem issues extra tokens and later burns remaining inventory](https://pump.fun/docs/mayhem-mode).
The [official SDK fee calculation](https://unpkg.com/@pump-fun/pump-sdk@2.0.0/src/fees.ts)
uses native mint supply for Mayhem fee tiers, unlike the unchanged event model.
The revised check preserves curve/CreateEvent identity and independently
requires enough native supply for the full quantity. **Mayhem inventory still
remains unknown** because entry-time native supply was not observed; later
marks cannot backfill it. No `2x` exception, reserve injection or historical
fee substitution was introduced. A real-decoder offline regression verifies
that a normal holder burn leaves the modeled quote unchanged, while
insufficient supply and unproven Mayhem fees are refused.

Failure and repair evidence: `account-0914120619-full_failure_analysis.json`,
`account-0914120619_instrumentation_repairs.json`,
`account-0914120619_deadline_analysis.json`, and
`account-0914120619_deadline_final_verification.json`.
`account-0914120619_sample_audit.json` records the predecessor samples'
incompatible coverage and fee semantics. Old source archives, locks, reports
and refused samples remain unchanged.

### Final bounded attempt and closeout

`account-0914120619-deadline-fixed` stopped on **HTTP 429** after
3,570.153564335 seconds of observed capture. It preserved all 997 recorded
creations: 27 warmup, 559 discovery and 411 holdout. Admission reached
3,510.153564335 of 3,600 seconds; the scheduled drain was never reached.
There were 44 head gaps, ten separate `-32016` refusals and eleven late targets.
All 3,864 scheduled targets have an outcome, but outcome accounting is not
successful pricing or a complete creation denominator.

The original strict account report also refused `rpc_head_identity` at
sequence 696 in discovery. The receiver timestamped the next head 106.359 µs
before the request, while the marker still held the prior delivered head.
The two-clock source repair above records this boundary prospectively;
offline checks cover pending delivery and stale receipt despite late delivery.
**No further acquisition ran, and no historical clock was invented.**

`account-0914120619-deadline-fixed_native_diagnostic.json` is explicitly
**diagnostic-only**, not the missing accepted report. It attempts every
original clock guard and quarantines the rejected mint/arm before valuation,
retaining its full unknown inventory. All other original replay checks remain.
The following figures use only this final execution, without predecessor pooling:

| Half | Arm | Entered | Conditionally priced | Entered/unpriced | Priced-only modeled net, SOL |
|---|---|---:|---:|---:|---:|
| Discovery | C | 469 | 182 | 287 | +0.216632041 |
| Discovery | D2 | 436 | 169 | 267 | −0.072887349 |
| Partial holdout | C | 357 | 159 | 198 | +0.084585950 |
| Partial holdout | D2 | 331 | 151 | 180 | −0.041347257 |

On the 145 holdout mints priced in both arms, C totals +0.040861109 SOL
and D2 −0.038519034 SOL. These are still selected counterfactual subsets.
C loses its positive holdout sum after removing its ten best priced rows
(−0.001225984 SOL). Unknown holdout entries retain modeled entry bases of
1.495007314 SOL for C and 1.436844805 SOL for D2, excluding network/cleanup
estimates; their liquidation values are not zero and are not netted away.

Native observations change the priced population, not merely its prices.
Only 27 C and 26 D2 holdout rows are priced by both native and event-only
models. The 132 newly native-priced C rows total **−0.007991845 SOL**;
the 125 newly native-priced D2 rows total **−0.043409441 SOL**.
Native priced hold medians are 13.091 s and 13.351 s, not ten seconds.
The unchanged hypothetical entry, different exit clocks, prior fee scenario,
unpriced inventory and missing inclusion/fill evidence prevent a portfolio
return or daily-income claim. Actual-net fields remain null.

All four executions are terminal. All 30 uploaded artifacts
(4,027,344,617 bytes) were recovered by exact generation and verified locally
with SHA-256 and MD5. The four jobs, evidence bucket, provider-only secret and
study service account were deleted; all seven active-resource lookups returned
404. The 30 objects' **seven-day soft deletion remains intact**, not purged.
Cloud Run telemetry implies **$0.297239** at published list prices; execution
wall time gives a separate **$0.307971** compute proxy. Seven days of retained
data estimate **$0.020129**. None is an invoice or a complete operating cost:
egress, operations, provider charges, active storage, logs, secrets, credits
and taxes remain outside these estimates.

The public payer was 562,213,411 lamports before and after the final capture.
The disabled live configuration and empty position journal remain byte-identical.
All 26 durable submissions have outcomes (24 success, two expired), with no
unmatched intents, unresolved submissions or operation intents. Historical
risk reservations remain untouched; they were not cleared to claim readiness.
No signing, submissions, live authorization or key-loading preflight occurred.

Closeout evidence: `account-0914120619_research_report.json`,
`account-0914120619_final_sample_coverage.json`,
`account-0914120619_final_verification.json`,
`account-0914120619_final_public_safety.json`, and
`account-0914120619_billing_metric_final.json`.
The locked full-schedule/strict-report acceptance criteria remain **unmet**;
the diagnostic does not establish repeatable net $100/day.

## Fixed public cycle probes

`simulate_public_cycles.py` selects one of five retained public routes:

- `--route orca` (default): **WSOL → DRb8 → USDC → WSOL**, through two Orca
  Whirlpools and Raydium CPMM.
- `--route damm`: **WSOL → HRw8 → 3ehU → WSOL**, through Raydium AMM v4 and two
  Meteora DAMM v2 pools. Mint decimals are 9 / 9 / **8**, respectively.
- `--route dlmm`: **WSOL → JUP → k3nv → HZ1J → WSOL**, through Meteora DLMM,
  two DAMM v2 pools and Orca. Mint decimals are 9 / 6 / 9 / 6.
- `--route orca-amm`: **WSOL → USDC → 6vVf → WSOL**, through two Orca
  Whirlpools and Raydium AMM v4.
- `--route dlmm-damm`: **WSOL → MET → 4KsG → WSOL**, through DLMM and two
  DAMM v2 pools.

Historical receipts are discovery leads, not current quotes or evidence of
repeatable ROI. Each route's pool, mint, payer and lookup-table identities are
fixed in the script; source hashes and financial limits appear in each manifest.

The two Orca-leading routes first apply a fee-free, impact-free **spot upper
bound** to one fully attested dependency snapshot. Exact rational arithmetic
uses the Whirlpool sqrt prices and Raydium's effective reserves, not raw vault
balances. For funded budget `B` and ratio product `P`, a successful cycle cannot
net more than `max(0, floor(B * (P - 1))) - 75000` in that state. A bound below
the existing 1,000-lamport floor yields `screened_out`, with
`net_lamports: null`, before any simulation. The negative bound is **not** an
actual loss or a bound on failed/abstained transactions, where the tip is not paid.

The screen expires after two seconds from entering the dependency call or two
observed slots, and is discarded before the first native dispatch. It does not
predict later prices; passing it proves no profit. Other route shapes remain
unscreened. Native acquisition caps, quote freshness, wallet closure, profit
guard and final wallet audit remain authoritative and unchanged.

```bash
# Offline: synthetic HTTP orchestration plus native local-VM balance-guard checks
uv run learning-examples/token-lifecycles/simulate_public_cycles.py self-check

# Explicit, bounded public-RPC simulation; no wallet file, signing or submission
uv run learning-examples/token-lifecycles/simulate_public_cycles.py observe \
  --route orca --public-rpc --inputs 1000000 --out /tmp/public-triangle.jsonl

# Same bounds, DAMM route, all three predeclared sizes
uv run learning-examples/token-lifecycles/simulate_public_cycles.py observe \
  --route damm --public-rpc --out /tmp/public-damm.jsonl

# Four-leg DLMM route; the same funding, fee, freshness and no-retry limits
uv run learning-examples/token-lifecycles/simulate_public_cycles.py observe \
  --route dlmm --public-rpc --out /tmp/public-dlmm.jsonl
```

Alternatively, use `--provider-json <provider-only.json>` instead of `--public-rpc`.
No dotenv discovery or private-key loading occurs. Output must be a new JSONL
file. The default input sizes are 100,000, 1,000,000 and 10,000,000 lamports;
one to three distinct positive sizes at or below 10,000,000 are accepted.
The maximum is `4 + number_of_quote_boundaries × number_of_sizes` HTTP requests,
including a reserved wallet audit. HTTP/transport failures stop without retrying.
For `orca` and `orca-amm`, native `two_hop_swap` combines the first two pools:
one exact-input prefix quotes both, then the final packet buys exactly that
bridge quantity with an exact-output two-hop capped at the original SOL budget.
The third pool sells that exact quantity. These routes need **two simulations /
six HTTP requests** for one size, rather than three / seven. Unspent WSOL returns
through ordinary closure; it is not profit. The manifest records `quote_legs`
and `quantity_policy`. Other fixed CLI routes keep one quote boundary per pool;
the snapshot-seeded first-leg optimization below can skip one native prefix.
The receipt-derived CLMM → Orca → Orca → AMM-v4 family uses boundaries **1/3/4**:
the middle Orca pair stays exact-input even in the closing packet, consuming
its entire non-SOL input. Only a leading Orca pair can use the bounded
exact-output bridge above. The CLMM family is admitted from fresh receipts,
not added to the fixed historical-route CLI registry.

Only the final complete packet can report simulated profit:
every route ATA must initially be absent, all must close,
and the final native self-transfer must cover the original wallet balance plus
1,000 lamports.
The 65,000-lamport network fee and 10,000-lamport tip are included in the
balance check, not subtracted a second time. Temporary rent is not income.
Changed quantities, residual inventory, unsupported dependencies or missing
native balance evidence cause refusals.
Full-range Whirlpools are supported: valid directional arrays are derived once
and the last is repeated to fill the three required instruction slots, matching
the [SDK padding](https://github.com/orca-so/whirlpools/blob/main/legacy-sdk/whirlpool/src/quotes/swap/tick-array-sequence.ts)
and [native sparse-array deduplication](https://github.com/orca-so/whirlpools/blob/main/programs/whirlpool/src/util/sparse_swap.rs).

Each candidate has a two-second budget through native-result validation and a
two-slot maximum age from its first prefix. The batched account state and each
simulation must report matching processed context slots; this does **not**
pin a bank or establish inclusion. The closing wallet audit cannot regress
behind observed slots. Started-but-failed sizes remain in the denominator;
only sizes never started appear under `unattempted_inputs`.

`completed` means the bounded sweep and wallet audit completed, not that any
cycle executed successfully. Inspect individual candidate statuses; failures
and refusals retain null returns. `observed_live_roi` is always null. The
offline verifier fabricates market responses to exercise orchestration and
uses LiteSVM only for native setup/closure and the one-lamport guard boundary,
not for venue execution or the provider's priority-fee schedule.
Old source archives, locks and economic reports remain unchanged; do not
regenerate their source identities to replay them against modified helpers.

The shared RPC journal now retains `rpc_error_codes`: signed 32-bit protocol
integers keyed by request ID, only after strict response correlation. Error
messages, data and raw failure bodies remain redacted, including responses that
echo credentials. An HTTP 200 with a JSON-RPC error is still a failed request;
without its numeric code, do not infer throttling or unsupported transaction
versions from `rpc_api_error` alone.

The public event collectors cancel their WebSocket reader before closing its
socket, so intentional teardown cannot replace an earlier RPC failure with
`websocket_stream_ended`. For older captures, inspect the first failed `rpc` row
rather than trusting that terminal label. Replay those captures with their
archived sources. Missing historical error bodies remain unavailable; a later
request is separate evidence, not permission to rewrite an old journal.

New source-locked public discovery excludes an unsupported-version receipt only
when that single `getTransaction` request returns HTTP 200, `rpc_api_error`, and
the identity-correlated code `-32015`. The `receipt_excluded` event records
`unsupported_transaction_version`, the RPC row's sequence, and its numeric code.
Actual `receipt_requests` include unsupported versions and fatal attempted
requests. Exclusions remain coverage gaps, never losses or evaluated routes.
The ceiling stays `maxSupportedTransactionVersion: 0`; no retry or version
escalation occurs. Other RPC errors, HTTP failures, malformed identities and
redaction failures remain fatal, as does `-32015` outside receipt discovery.
Old captures keep their original policy. The loopback check covers both public
collectors, exclusion continuation, and these fatal boundaries:

```bash
uv run .state/paper-trading/verify_public_capture_failure_20260917.py
```

The earlier lower-load venue capture read 200 signatures per program serially,
with two seconds from the previous response headers before each discovery
request. It capped discovery at three rounds, four receipts per round, and
120 seconds; later rounds waited at least 40 seconds after the prior lists.
Native evaluation kept its existing pacing, freshness and financial limits.
The first retained serial run completed without refusals, but every supported
family in every round had disjoint program-list slot ranges. Its zero
candidates cannot establish absent opportunities.

The aligned predecessor reused the first program's newest confirmed signature
as a shared `before` cursor for the remaining four histories, with its slot as
`minContextSlot`. Agave resolves `before` from the signature's ledger position,
not from membership in the requested program's history
([implementation](https://github.com/anza-xyz/agave/blob/v3.1.8/ledger/src/blockstore.rs#L3302-L3445)).
Each family's selection was restricted to its common complete-slot interval:
strictly below the anchor slot and above every member page's oldest slot,
within the existing 150-slot freshness bound. This excludes partially listed
boundary slots. Empty pages and intervals without complete slots are coverage
gaps, not evidence of absent opportunities. The retained selection census
records every family's interval. `minContextSlot` imposes a minimum context;
it does not pin a fork or filter the returned history by slot. Receipt,
accounting and economic gates remain unchanged.

The bounded aligned run `supported-venue-aligned-20260917T084232Z` completed
three rounds without a fatal RPC failure: all 15 family/round intervals had
complete common slots, with 32, 67 and 44 candidates respectively. Of 12
receipt requests, two excluded unsupported transaction versions (`-32015`).
Ten receipts decoded; six had unknown token accounting, one was non-flat and
three lacked a complete sequential route. None qualified for native
observation. The exact-source replay reproduced selection, accounting and
exclusions; its adjacent `.analysis.json` retains hashes and coverage details.
Alignment recovered useful discovery coverage, not evidence of profitability.

The current venue collector uses a separate discovery WebSocket with six
confirmed [`logsSubscribe`](https://solana.com/docs/rpc/websocket/logssubscribe)
subscriptions: AMM-v4, CPMM, CLMM, DAMM-v2, DLMM and Orca. It screens any
three/four-leg order across these existing models, including repeated venues.
Complete runtime invocation stacks collapse nested same-program event CPIs
rather than counting them as extra legs. A mention alone is insufficient.
A complete candidate stack executing Token-2022 is excluded before receipt
quota or acquisition HTTP. Account mentions and program-log text do not trigger
this check; signed-receipt and native admission rules remain unchanged.
Failed, truncated or ambiguous traces do not select receipts. The first four
fresh matching identities in each 40-second window are selected before their
receipt accounting. An independent serial HTTP worker drains a FIFO bounded
by twelve selected identities total; the log reader does not await those
requests. Three windows and two-second response-header pacing remain fixed.
With no qualified route yet, the fourth selection immediately triggers
unsubscribe, not the fourth receipt response. After acknowledgements, queued
work may finish before the original next-window boundary; resume on the same
socket only if no route qualified. There is no reconnect. After an acknowledged
unsubscribe, the same program/filter may reuse its ID; active-ID collisions
and cross-program reuse remain fatal. Setup/inactive
notifications cannot select or poison deduplication; gaps are not complete
market coverage. The first qualifying window determines the catalog, and all
selected same-window work finishes before it freezes. If qualification arrives
after later-window identities were queued, those entries are explicitly
censored before receipt acquisition. The socket closes before native warmup.
Both sockets share the existing 16 MiB / 40,000-message ceilings; all native
financial and freshness guards remain unchanged.

The signed receipt must reproduce the selected slot and log digest and pass
the existing instruction, payer-ownership and accounting gates. An all-zero
`Signature.default()` is not a transaction identity: count it as unsigned and
discard it **before deduplication**, without a receipt request. Conflicting
notifications for an actual signature still stop the capture.

The first invocation run, `supported-venue-invocations-20260917T101213Z`,
stopped after 8.19 seconds and 811 log notifications because two different
payloads used that all-zero signature. It made only the genesis identity HTTP
request: no receipt requests, admitted routes or native simulations. The
unsigned-identity guard was added afterward and verified on loopback; replay
of the retained prefix excludes both placeholders without a conflict. That
replay is not a completed public capture or evidence about the unobserved
remainder. The adjacent `.analysis.json` preserves the failed run, exact
source replay and post-fix coverage limits.

### Ordinary-SPL Raydium CLMM extension (2026-09-17)

The initial extension admitted one additional family:
**Raydium CLMM → Orca → Orca → Raydium AMM v4**. That six-family capture retained
two discovery subscriptions and the existing financial, freshness, request and
WebSocket limits. The later order expansion is documented below; neither
change enables CLMM trading in the bot.

`simulate_clmm_cycle.py` follows the
[pinned Raydium source](https://github.com/raydium-io/raydium-clmm/tree/ed7c84a54ced59c55981780546adb0b4583dcf85).
It validates pool/config/vault identities, mint ownership and decimals,
pool-bound observation accounts, and initialized tick arrays. Coverage is
bounded to three initialized arrays per direction, without padding a missing
array or inventing a price. Token-2022 mints/vaults and permissioned seed-index
pools remain excluded; the deployed `swap_v2` performs the arithmetic.

```bash
uv run learning-examples/token-lifecycles/verify_clmm_cycle.py
uv run .state/paper-trading/record_supported_venue_opportunities_20260917.py --self-check
```

The first public topology control,
`clmm-native-control-20260917T162619Z`, exposed an invalid observation-PDA
assumption before any simulation. Existing pools can use non-PDA observations;
`swap_v2` binds them through `pool.observation_key`. The fix preserves owner,
discriminator and pool-backpointer checks, with a failing-before/passing-after
offline regression.

The corrected control, `clmm-native-control-20260917T163237Z`, used a
1,000,000-lamport input and seven HTTP requests. Native CLMM, Orca two-hop and
AMM-v4 instructions all completed before the closing profit guard rejected:
**1,038 bytes, 180,113 CU, 740.220 ms**, two slots after the first prefix.
`net_lamports` remains null; this was unsigned simulation, not a fill or a
realized loss. Both controls audited the public wallet unchanged. Their
retained receipt supplies topology only, not a fresh opportunity sample.

The separate fresh run, `supported-venue-clmm-20260917T163421Z`, hit the
unchanged 16 MiB stop condition with its last retained log at **+29.447 s**.
It retained 6,923 log notifications: 193 duplicates, one unsigned placeholder,
2,982 failed transactions and 3,747 successful identities outside the six
families. There were no selections, receipt requests or native observations;
the sole HTTP request checked genesis. Two over-budget frames totaling 3,289
bytes were not retained, and the byte counter exceeded the trigger by 1,320
bytes. This timestamp is not an exact process exit time or complete coverage
of the scheduled 120 seconds.

All three runs have adjacent immutable `.source.zip`, `.lock.json` and
`.analysis.json` artifacts under `.state/paper-trading/`. Their stored offline
replays verify exact source, hash chains, native packets/results where present,
and the discovery exclusions. Native support is verified; prospective
opportunity frequency and profitability remain unmeasured. No signing,
submission, retries, funding or increased caps occurred.

### Broader existing-venue orders (2026-09-18)

Paper admission now accepts any three/four-leg order across the six modeled
venues, but still requires signed receipts with `maxSupportedTransactionVersion: 0`,
ordinary SPL, distinct mints/pools and a fully funded sequential closed WSOL
cycle. Receipt decoding still recognizes the existing single-pool swap forms;
this does not add decoding of Orca two-hop receipts. Native construction can
merge adjacent Orca pairs anywhere in a route: only a leading pair may use
the bounded exact-output bridge in the closing packet; later pairs consume
their entire input. The order matrix checked all **1,512** program sequences
and quote boundaries, not 1,512 executable pools or market observations.

The single bounded run, `supported-venue-orders-20260918T010328Z`, selected
four receipts and froze discovery at **+20.569 s**. Two receipts had unknown
token-extension accounting and one was not inventory-flat. One ordinary-SPL
route qualified: **Orca → Orca → Orca → Raydium CLMM**, `7839fdac12d0da07`.
Warmup validated four pools, five lookup tables, 54 snapshot accounts and
43 watched accounts. The first event snapshot requested `minContextSlot`
**447938149** and received HTTP 200 / **-32016** (minimum context slot not
reached). The capture stopped before any native simulation: **12 HTTP RPC
requests, zero priced outcomes, zero signed/submitted transactions**. No retry
or post-stop wallet audit was performed. The retained error has a code, byte
count and hash, not the provider message or its actual context slot.

This run also exposed a readiness-record hash defect: integer subscription
IDs of different widths sort differently after JSON turns them into string
keys. **The original capture fails strict hash-chain replay at record 66.**
Its adjacent `.analysis.json` reconstructs the exact archived writer preimage,
verifies all other hashes and links, and reproduces receipt admission, warmup
and event-trigger coalescing. That forensic result is not clean strict-chain
acceptance. The emitter now stringifies keys before hashing; a mixed-width
9/10-ID loopback regression fails before the fix and passes afterward.
All 24 then-existing loopback cases passed, including quota pause/resume and
fatal unsubscribe acknowledgements. Public pause/resume was not exercised:
this run found a route in its first window. Original artifacts remain unchanged;
the separate post-fix capture below is not a repair of this tape.

### Nonblocking discovery acquisition (2026-09-18)

`supported-venue-orders-fixed-20260918T021758Z` passes normal strict replay:
**7,333 records and 17 frozen source files**, without a forensic hash exception.
Its four selected receipts yielded three Token-2022 accounting exclusions and
one non-flat inventory. The unchanged **16 MiB** WebSocket ceiling stopped the
run before discovery finished: nine HTTP RPC requests, zero admitted routes and
zero native simulations, signing or submission. No RPC failed or was retried.
The adjacent `.analysis.json` embeds the offline strict replay and exact hashes.

This capture isolated a reader bottleneck. Serial head/receipt HTTP calls
prevented discovery-log reads; quota filled at **+12.159 s**, but unsubscribe
started only at **+16.289 s**. Before any unsubscribe acknowledgement arrived,
**6,582 inactive notifications / 15,233,292 bytes** consumed the remaining budget.
Their latest slot was **447952173**, already behind the stream head **447952191**
at quota selection. These are raw notification counts, not unique trades or
negative economic observations. The last retained log at **+18.637 s** is not
an exact process-exit timestamp.

The collector now separates its bounded log reader from the serial receipt
worker. A loopback server that withholds the first receipt until quota
unsubscription completes fails against the frozen blocking collector and passes
afterward. The initial **32** cases in `verify_public_capture_failure_20260917.py` passed,
including genuine signed-receipt qualification after pause, later-window
censorship, fatal HTTP/stream errors, mixed-width readiness hashes and worker
cleanup. The actual CLI's freeze-only policy comparison leaves every
non-descriptive field unchanged except the collector source hash; the complete
native policy is identical. Proof is retained in the adjacent
`.drain-fix.analysis.json`.

That initial change had only loopback/offline evidence. The following capture
was its first public check. The earlier stopped run had no post-stop wallet
audit or native economic observation.

### Nonblocking public capture and subscription reuse (2026-09-18)

`supported-venue-nonblocking-20260918T052244Z` passes normal strict replay:
**1,200 records and 17 frozen sources**. Its first unsubscribe request was
recorded **22.7 microseconds** after the fourth selection, and all six
unsubscribe acknowledgements arrived in about **341 ms**, before the first
receipt response. Total WebSocket traffic was **2,230,183 bytes / 1,142 frames**,
below the unchanged ceilings. Nine HTTP requests acquired four receipts in FIFO
order: three were excluded for Token-2022/unknown accounting and one for
non-flat inventory. None is a fresh native economic observation.

At **+40.306 s**, the resumed AMM-v4 subscription returned **229390**, the same
ID whose unsubscribe had already been acknowledged. The frozen collector
incorrectly required IDs to be new across the entire socket lifetime and
stopped with `log_subscription_identity`. Zero routes reached native simulation;
there was no signing, submission, retry or post-stop wallet audit.

The corrected guard permits acknowledged, inactive ID reuse only for the same
program/filter. It still rejects active-ID collisions and cross-program reuse.
The loopback reuse case fails against the frozen source and passes after the
fix, including a conflicting setup notification that must not poison later
deduplication. All **35** lifecycle/failure cases and both receipt self-checks
pass. The actual CLI's freeze-only comparison changes only the collector source
hash; every other policy field, including the full native policy, is identical.

The adjacent `.analysis.json` embeds the strict public replay.
`.subscription-reuse-fix.analysis.json` retains the offline before/after proof.
That repair initially had only offline validation. The completed public capture
below verifies same-program ID reuse; fresh native economics remain unmeasured.

### Completed acquisition after subscription repair (2026-09-18)

`supported-venue-reuse-fixed-20260918T072745Z` completed normally: **four receipts
in each of three fixed windows**, **25 HTTP requests**, and no retries.
Normal strict replay verifies **1,544 records and 17 frozen sources**. All six
subscription IDs were reused successfully in both subsequent windows
(**12 acknowledgements**). Both quota pauses completed before their window's
first receipt response; first-unsubscribe request records followed selection by
about **24 and 30 microseconds**, with pauses recorded after **448 and 411 ms**.
Acquisition ended at **+96.567 s**. Retained WebSocket traffic was
**2,423,757 bytes / 1,413 frames**, with no budget-crossing unretained frames.

All twelve receipts remained ineligible under the unchanged admission rules:
**ten Token-2022 accounting exclusions and two non-flat inventories**. No route
was admitted, no native observation window opened, and no transaction was
simulated, signed or submitted. Wallet accounting reports `baseline_unavailable`,
not an unchanged-balance audit. These are coverage exclusions, not measured
trading losses or evidence of profitability.

The adjacent `.analysis.json` embeds the strict replay and hashes. This run
validates the collector's pause/resume and ID-reuse behavior, not native
execution or economic edge. No code, admission rules or safety caps were
changed in that continuation, which included only one public run.

### Token-program screening and fresh route (2026-09-18)

The collector now excludes executed Token-2022 calls from complete supported
log stacks before selection: receipt accounting already rejects that program.
Frozen before/after comparison on **34 signed receipts** screens **20 already
ineligible supported-family receipts**, retaining the admissible CLMM control
and all four original positive log controls. The new loopback regression fails
against the old collector and passes after the filter: rejected identities
cannot consume receipt quota, while program-log text mentioning Token-2022
remains harmless. **All 36 loopback cases**, both self-checks and scoped Ruff
checks pass; malformed stacks still retain their original refusal.

One newly locked capture, `supported-venue-token-screen-20260918T080626Z`,
filtered **19 unique log identities** before acquisition and made **eight
receipt requests**, selected `[4, 4, 0]` across the fixed windows. Five signed
receipts had non-flat inventories, one had an unreconciled native transfer
trace, and one request returned unsupported-version code `-32015`. The remaining
signed receipt admitted fresh route **`4b5cae11cc092f70`**, three AMM-v4 swaps
forming a funded closed-WSOL cycle. Native warmup attested its three pools,
ordinary-SPL mints/vaults and two lookup tables; twelve accounts were subscribed.
No retained positive control was backfilled into this cohort.

The observation window opened, but its first `event_snapshot` failed with
**RPC `-32016`**, requesting **`minContextSlot: 448024806`** for a slot
`448024805` trigger. The failure arrived **0.343 s after window opening**, before
the decision deadline. No economic evaluation, simulation, signing or submission
followed. The wallet matched across the two warmup reads; the fail-fast policy
correctly made no additional RPC for a post-failure balance audit.

Strict frozen-source replay verifies **3,832 records / 17 source files**,
**20 HTTP requests**, and **7,328,887 WebSocket bytes / 3,717 frames**.
The process exited **2** after about **58.6 s**, with zero restarts or retries.
Only discovery screening changed; native admission, financial, freshness and
resource limits stayed identical. The adjacent `.analysis.json` retains the
source comparison, regression proof, actual CLI policy comparison and executable
capture replay. This advances acquisition and warmup coverage, not profitability
evidence; no second public run followed in this continuation.

### Approved one-slot paper snapshot lag (2026-09-18)

The two event-driven paper collectors now freeze `max_head_lag_slots: 1`.
Their first snapshot requests `max(trigger_slot, stream_head_slot - 1)`;
snapshots never precede the trigger. Both the returned snapshot and the native
result must remain within one slot of the latest observed head, whose receipt
must be no older than one second at each check. A stale head or excess lag
censors the observation instead of recording a return or closing an episode.

This is an explicitly approved freshness-policy expansion, not a redundant-check
fix: the existing two-slot limits bound **forward drift** from the trigger or
first prefix, not lag behind the stream. Those limits, the two-second event
deadline, matching account/simulation slots, financial/resource caps and zero
retries remain unchanged. Both actual CLI policy producers were compared with
their frozen predecessors; only the lag field and collector source hashes
changed. The verifier's freshness cases run alongside its 36 loopback cases;
the one-slot case fails under the old floor. Mocked boundary checks are
not price or execution evidence.

One approved-policy capture, `supported-venue-one-slot-20260918T110342Z`,
completed normally after approximately **1m46s** without restart. It acquired
**12 signed receipts**, selected `[4, 4, 4]`: seven had non-flat inventories,
three had unsupported token accounting, one had an owner transfer outside a
recognized swap, and one failed swap-owner-flow accounting. **17** Token-2022
log identities were excluded before acquisition. No route qualified, so there
was **no live exercise of the changed snapshot boundary**, no native simulation,
and no signing or submission. Wallet audit remains `baseline_unavailable`,
not an unchanged-balance claim. Strict replay verifies **3,789 records / 17
source files**, **25 HTTP requests** and **3,658 WebSocket frames / 13,459,770
bytes**. The adjacent `.analysis.json` retains replay and verification evidence.
That continuation ended after this single public capture.

### Timing and notification cost controls (2026-09-18)

The collectors now censor `trigger_slot_interval_empty` **before** snapshot
acquisition when `head - max_head_lag_slots > trigger + max_trigger_age_slots`.
No bank can satisfy both bounds then. A three-slot head/trigger gap still
permits the exact `trigger + 2` bank; a four-slot gap cannot. The original
two-second event clock, first-trigger timestamp, post-snapshot and post-native
checks remain mandatory. The head can advance while the RPC is paced.

Native account subscriptions request `base64+zstd`, published as
`account_notification_encoding` in both CLI manifests. This is lossless
notification encoding, not a change to the native HTTP snapshots. The collector
uses notification slots as triggers and retains the opaque payloads, so no
runtime decompression dependency is needed. It still requests zero-length
`dataSlice`, but must not rely on the public provider honoring that request:
the retained repeat run received 2,202 nonempty account payloads.

`native-timing-remediation-20260918T135135Z.analysis.json` retains the diagnosis,
actual policy comparisons, and executable offline proofs. Per-frame compression
round-tripped all 2,202 payloads, estimating native notification traffic of
1,914,443 rather than 14,140,449 bytes for that exact trace. This is not a future
bandwidth or latency guarantee. A separate bounded public check matched the
same slots and complete account data for a Clock account and an Orca pool under
both encodings; small payloads can grow slightly.

The verifier now covers ten freshness boundaries, including the last eligible
bank and rejection before an impossible request. The new check fails against
the old frozen collector. All 36 loopback cases still pass. Only notification
encoding and collector source hashes changed in the actual CLI policy
comparisons; financial, freshness, pacing, resource and retry limits did not.
These checks prove control behavior, not trading returns.

One bounded capture, `supported-venue-timing-20260918T140511Z`, then admitted an
AMM-v4 → Orca → AMM-v4 route and completed the full 300-second observation window.
It retained **3,213 records**, **178 HTTP requests** and **2,503 WebSocket frames /
3,427,050 bytes**. There were **42 accepted fresh snapshots** and **86 unsigned
simulation attempts**, but **all 92 final observations were censored**:
27 `trigger_bank_age`, 23 `quote_slot_age`, 21 `event_deadline_expired`,
10 `snapshot_head_lag`, 10 `trigger_slot_interval_empty`, and one
`trigger_result_age`. Intermediate native results are not eligible economics.

The **83 complete simulation replies** contained 71 quote-only results, ten
profit-guard rejections and two Orca token-transfer insufficient-funds errors.
All eleven complete guarded replies exceeded the quote-age limit; their raw
outcomes are diagnostics, not accepted returns or paid fees. All 23 quote-age
refusals also exceeded the trigger-age limit.

The final processed-bank audit matched the starting payer balance of
**562,213,411 lamports** and still-absent route ATAs at slot **448106682**. The
collector signed and submitted nothing, opened no policy episode and observed
no live ROI. The adjacent `.analysis.json` retains separate executable
stream/admission, native packet and native-control replays against the **17
frozen source files**, plus the ten-boundary regression. The compression and
timing changes enabled native-path observation; they did not establish a return
or relax any freshness bound. That continuation ended after that single capture.

### Snapshot-seeded constant-product first leg (2026-09-18)

A fully attested initial snapshot now seeds an AMM-v4 or CPMM first leg through
the existing `Pool.quote` integer arithmetic. The next native packet still
executes that first swap and every subsequent prefix. It must consume the
intermediate inventory exactly; the closing packet still proves ATA closure,
fees, tip and the payer profit floor. Orca/CLMM/DAMM/DLMM math remains native.
This changes the source of one quantity, not the acceptance standard.

Both collectors publish
`first_leg_quote_policy: single_use_attested_constant_product_snapshot` inside
their native policy. The seed is consumed once: every event supplies its fresh
snapshot, while subsequent sizes in a standalone sweep retain native-prefix
discovery. Quote age starts at the **snapshot slot**, not the first later
simulation. The native deadline cannot exceed **snapshot request time + two
seconds**, and the collector's original first-trigger deadline still applies.

On `supported-venue-timing-20260918T140511Z`, the existing AMM arithmetic matched
all **38** same-slot native first-leg quantities exactly, including the earlier
event-snapshot quantities. A construction-only smoke exercised the actual
collector on all **42** accepted banks and reproduced the **38** recorded
follow-on packets byte-for-byte. No earlier native response or economic outcome
is inferred from that counterfactual. With zero hypothetical network/processing
delay and the retained head stream, only **6/42** two-call schedules were not
already excluded by the timing bounds, compared with **0/42** three-call
schedules. This is a feasibility limit for that trace, not a latency guarantee.

Seven new offline cases cover seeded native closure, the exact two-slot edge,
older/regressed banks, expired/future snapshot clocks and dust refusal. The
regression fails before the change. Existing native checks, all 36 collector
loopback cases and ten head-freshness cases still pass. Both actual CLI policy
comparisons preserve every other policy field; only the new quote-source field
and affected source hashes differ. Synthetic replies prove control behavior,
not profitability.

The single seeded capture, `supported-venue-seeded-20260918T154554Z`, admitted a
different four-leg route: **AMM-v4 → AMM-v4 → AMM-v4 → Orca**. It exercised the
new path on **15 fresh snapshots**, starting each native sequence at `prefix_2`.
There were **19 unsigned simulation attempts** and no closing guard packet.
All **35 completed observations** were censored: 13 `quote_slot_age`, seven
`snapshot_head_lag`, seven `trigger_slot_interval_empty`, five `trigger_bank_age`
and three `event_deadline_expired`. The 36th decision has no completed result.

The run stopped after **193.949 seconds of observation** on
`local_ServerDisconnectedError` during its final snapshot request. It retained
**3,281 records / 17 source files**, **59 HTTP requests** and **3,016 WebSocket
frames / 4,692,972 bytes**. One head-notification gap measured 1.199 seconds.
The failure policy made **no further RPC or retry**, so the final wallet audit
is **unavailable**, not unchanged. Nothing was signed or submitted, no policy
episode opened and ROI remains unknown. The adjacent `.analysis.json` retains
the diagnosis, immutable old/new regression, policy comparisons, packet smoke
and separate discovery/native replays. Different routes and traffic prevent a
controlled performance comparison with the preceding capture.

All **11 saved proof parts** re-execute offline against seven hash-pinned inputs.
The independent wire audit verifies all 19 packets and 17 complete native
replies, all `quote_only`. The unmodified frozen collector and native candidate
reproduce all 36 decisions, 15 snapshots, four stages and 35 final observations
at both bounding clock assignments, including the final escaping transport
failure. Those bounds are not measurements of unlogged internal timestamps.
The proof bundle is
`.state/paper-trading/supported-venue-seeded-20260918T154554Z.analysis.json`
(SHA-256 `87bd0020d72711a3827d091a998aa4905158e70d61b317a42ea39f30798f41c0`).

### Leading constant-product quote prefix (2026-09-18)

Snapshot seeding now covers **consecutive leading AMM-v4/CPMM legs**, stopping
before the first other venue or the closing leg. Even an all-constant-product
route must execute its closing native guarded simulation. The single-use seed,
snapshot-origin age, trigger deadline, dependency and inventory checks are
unchanged. Both collectors replace the earlier `first_leg_quote_policy` field
with `snapshot_quote_policy: single_use_attested_constant_product_prefix`.
No other financial, freshness, acquisition or resource limit changes.

On the preceding seeded capture, chaining the existing quote helper matches
**all 17 complete native quantities**: 14 two-leg and three three-leg replies.
It also reconstructs **all 19 recorded prefix packets** byte-for-byte. An
offline construction smoke through the new collector on its 15 accepted banks
exposes a different limit: each full guarded packet is **1,416 bytes**, exceeding
the unchanged **1,232-byte** wire cap. All 15 are refused before a native request.
This uses a synthetic construction clock; no earlier response, fill or economic
outcome is inferred. The new four-CPMM regression verifies three local quantities,
native closure and refusal to reuse a consumed seed; it fails with the old
first-leg-only implementation. The existing seven snapshot safety cases,
native checks, 36 collector cases and ten head-freshness cases still pass.

The single new capture, `supported-venue-leading-20260918T191831Z`, completed
its **300-second** observation window. Two routes were admitted; the
**CLMM → DLMM → DLMM** route warmed, while the **Orca → CLMM → CLMM** route
was refused with `orca_pool_identity`. Neither starts with a constant-product
pool, so this live cohort **does not measure the new local-prefix optimization**.
It reached 18 fresh snapshots and attempted 37 unsigned native simulations.
All 71 final observations remain censored: 18 `trigger_bank_age`, 16
`snapshot_head_lag`, 16 `event_deadline_expired`, nine
`trigger_slot_interval_empty`, four `quote_slot_age`, and two each of
`trigger_expired_before_dispatch`, `dynamic_dependencies_outside_snapshot`,
`prefix_inventory_not_consumed` and `trigger_result_age`.

The retained stream has **3,379 records / 17 frozen source files**, **108 HTTP
requests** and **2,908 WebSocket frames / 4,457,038 bytes**. The final
processed-bank audit at slot **448176947** matches the initial payer balance
of **562,213,411 lamports** and absent route ATAs, including the unavailable
route's wallet accounts. Nothing was signed or submitted, no policy episode
opened and ROI remains unknown. There was no retry or second capture.

Of 42 constructed native request records, 37 were dispatched and 32 have
complete responses. The independent native parser classifies those as 24
`quote_only`, five `native_failure`, one `guard_rejected` and two
`prefix_inventory_not_consumed` validation refusals. Only two complete responses
are for closing guarded packets; neither succeeds. These raw diagnostics are
not freshness-qualified observations or paid trades.

All **11 saved proof parts** re-execute offline in separate fresh interpreters
against seven pinned input files. The frozen collector and native candidate
reproduce all **71 decisions, 18 snapshots, 26 stages, 71 immediate results and
42 native request packets** at both retained clock bounds. These executions
reproduce the tape; they do not measure unrecorded internal timing or establish
every possible schedule between the anchors. Missing replies remain unknown.
The old/new regression also proves the local-prefix change fails before the
change and passes afterward, independently of this capture's non-CP first leg.

Archive:
[`supported-venue-leading-20260918T191831Z.analysis.json`](../../.state/paper-trading/supported-venue-leading-20260918T191831Z.analysis.json),
SHA-256
`68e063b9c52d40437e29452e9e19dcca4d72e6d82f305fd826de79022cac3d4f`.

### Paired state read and first native quote (2026-09-20)

For routes not starting with a locally quoted constant-product prefix, the
event collector now batches its initial account read with the first unsigned
native simulation. Warm pool references construct the packet, not a trusted
quote: the returned bank must attest its dependencies and reproduce the packet
exactly before its native result is used. State and simulation slots must match.
The response is single-use, and cannot extend the original trigger deadline.
Leading AMM-v4/CPMM seeding and all later native accounting remain unchanged.

Both event collectors freeze
`event_snapshot_policy: paired_state_first_native_quote_unless_constant_product_seed`.
The two-second deadline, two-slot trigger/quote age, one-slot head lag, exact
inventory consumption, fees, rent, payer profit floor and zero-retry policy
remain enforced. No new venue quote arithmetic or fallback was added.

`verify_public_triangle.py` adds nine paired-response cases, including changed
packets, bank mismatches, expired/future clocks, fees, inventory, later freshness
and refusal to reuse a consumed response. Both real collector classes pass a
loopback smoke: two paired HTTP requests reach the fixture's guarded result;
a mismatched first bank is censored after one request. These are synthetic
control checks, not profitable mainnet executions. The existing native,
36 collector, ten head-lag, 24 pool-HTTP and seven pool-supplement cases pass.
The new frozen code also reproduces all 51 retained first-prefix packets and
quantities from `chainstack-window-260920-083524`, preserving its six inventory
refusals. That replay does not invent earlier prices or retime any outcome.

A final offline regression preserves the pre-existing constant-product seed
when a smaller standalone size is screened out, so the next eligible size does
not acquire a redundant native prefix. The final native controls, collector
smoke and 51-packet compatibility proof pass from the separate
`.verification.source.zip` archive. This correction received no cloud execution;
the original capture archive remains unchanged.

The single London capture, `chainstack-paired-260920-101349`, stopped after
**43.591 seconds of discovery** on the existing shared **16 MiB Geyser budget**.
It retained 2,113 frames totaling 16,785,702 bytes; the final 10,983-byte frame
crossed the threshold by 8,486 bytes and was retained but not dispatched.
Of 1,926 transaction notifications, seven identities were selected. Five
receipts were fetched and rejected: two for `owner_transfer_outside_recognized_swap`,
two for `receipt_accounting_not_qualified`, one for `swap_owner_flow_count`.
Two selections remained unfetched at the stop; they are not accounting failures.

There were **11 HTTP requests, zero admitted routes and zero native simulations**.
This cohort therefore does **not** validate the paired request on Chainstack,
measure a latency improvement or establish economic edge. No request followed
the stop, including a wallet audit: the final balance is unavailable, not
verified unchanged. Nothing was signed or submitted. The cumulative budget is
**261/400 requests, with 139 remaining**; there was no second capture.

The exact-source replay recomputes selection, every fetched receipt exclusion,
request pacing and the byte-budget stop. A scan of the raw tape and 2,124
decoded payloads found no configured service-secret matches. The temporary
cloud job, service account, secret, local provider projection and adapters were
removed; the owner's ignored `ENVFILE` remains mode 0600. Cloud logs retain
their existing retention policy.

Artifacts:
[`capture and replay`](../../.state/paper-trading/chainstack-paired-260920-101349.analysis.json)
and
[`offline verification`](../../.state/paper-trading/chainstack-paired-260920-101349.verification.json).

### Failed transaction metadata and bounded continuation (2026-09-20)

Offline screening rejected a `failed=false` Geyser filter: it would retain all
seven successful selections in the preceding tape but suppress the existing
failed-first signature-conflict guard. The six venue filters, sampling,
freshness, fees, request pacing and zero-retry policy were therefore unchanged.

One London continuation, `chainstack-paired-260920-223934`, used a cap of
**139 HTTP requests** and stopped after **1.978584908 seconds of discovery** on
`log_notification_shape`. A failed transaction at slot **448869296** carried
`log_messages_none=true` with an empty log list; the decoder rejected it before
counting its failed-transaction exclusion. This is a decoder boundary defect,
not a Chainstack transport or minimum-context-slot failure.

The capture used **two HTTP requests**, retained **1,180 Geyser updates /
9,833,313 bytes**, and selected two identities without fetching either receipt.
There were **zero routes, zero native simulations and no economic observation**.
No request followed the stop, including a final balance audit; wallet balance
is unavailable, not verified unchanged. Nothing was signed or submitted.
The cumulative budget is **263/400 requests, with 137 remaining**.

The narrow offline correction accepts missing logs only for an explicitly
failed transaction with an empty log list. Such a notification remains
ineligible; successful missing logs, contradictory log markers and conflicting
signature metadata still stop discovery.
[Agave's transaction metadata](https://raw.githubusercontent.com/anza-xyz/agave/master/transaction-status-client-types/src/lib.rs)
defines `log_messages` as optional. The new regression fails before the fix and
passes afterward. All **1,166 previously valid notifications** decode
identically; five discovery controls exercise the retained failure, subsequent
selection and the refusal boundaries. Native and transport controls also pass.
These are offline checks, not a resumed capture or Chainstack native validation.

The original source, lock and tape remain unchanged, and their exact replay
reproduces the stop. The correction and runnable checks are pinned separately
in `.verification.source.zip`; there was **no second cloud capture**. The
temporary cloud job, service account, secret, local provider projection and
adapters were removed. The raw tape and 1,182 decoded payloads had no configured
service-secret matches. The owner's ignored `ENVFILE` remains mode 0600;
cloud logs retain their existing retention policy.

Artifacts:
[`capture and replay`](../../.state/paper-trading/chainstack-paired-260920-223934.analysis.json)
and
[`offline correction and verification`](../../.state/paper-trading/chainstack-paired-260920-223934.verification.json).

### Approved 64 MiB capture allowance (2026-09-21)

The preceding `chainstack-paired-260920-231215` capture reached the local
16 MiB cumulative delivered-protobuf ceiling after **40.675272008 seconds**.
Its ten HTTP responses were all 200. Discovery requests were already paced
at least two seconds apart; after its first four selections, the transaction
subscription paused for **35.099916042 seconds**, receiving only 96 bytes of
discovery keepalives during the paused state. The first burst and unsubscribe
drain had already consumed **14,578,757 bytes**. This ceiling does not refill
while waiting, so extra backoff or a local read delay would not replenish it.
Seven identities were selected, four receipts excluded and three left
unfetched. That capture left **127 of the original 400 HTTP requests**.

The user then approved **one 64 MiB capture with unchanged sampling**.
Only the total Geyser byte ceiling changed: six simultaneous venue filters,
three fixed 40-second discovery windows, first-four selection, 40,000-message
and 1 MiB-message ceilings, 256 MiB evidence-tape ceiling, fees, freshness,
zero retries and no signing/submission remain enforced. Offline transport
checks accept delivery beyond 16 MiB and exactly at 64 MiB, then refuse and
retain the crossing frame across the shared head/discovery counter.
The original locks and captures remain immutable.

That single London capture, `chainstack-cap64-260921-011504`, admitted and
warmed one **Raydium CLMM → CLMM → CLMM** route. Four receipt requests yielded
one accounting exclusion, two unavailable receipts and the qualified route.
The retained stream used **4,243,407 bytes**, below even the previous ceiling:
this cohort does not establish that raising the ceiling caused its admission.
Discovery ended after **40.001283819 seconds**; the observation phase retained
**78.229210934 seconds** through its last timestamped event before stopping.

The stop was **`http_request_limit`**, not provider throttling.
There are 128 RPC journal records: **126 attempted HTTP requests** and two
interrupted before dispatch. Of **114 dispatched simulation batches**, 16
were refused with correlated `-32016` minimum-context-slot errors and did not
establish native execution; 98 returned complete simulation replies.
All attempted HTTP requests received status 200. The original 400-request
allowance is now **399 used, one reserved request remaining**.

All **96 immediate outcomes are censored**: 57
`dynamic_dependencies_outside_snapshot`, 16
`rpc_min_context_slot_not_reached`, nine `trigger_bank_age`, eight
`quote_slot_age`, two each of `event_deadline_expired` and
`prefix_inventory_not_consumed`, and one each of
`state_simulation_slot_mismatch` and `trigger_slot_interval_empty`.
There is no qualified profitable observation or policy episode. One pending
account-update trigger remains on the terminal record; it is unevaluated paper
work, not an open funded position. No retry or second capture was launched.

The archived collector and native candidate reproduce all **96 decisions,
116 constructed request packets, 12 snapshots, 21 stages and 96 outcomes**
at both retained lower and upper timing anchors. This checks the recorded
boundaries, not every possible unrecorded schedule. Separate native decoding
classifies the 98 complete replies as **27 quote-only, 64 native failures,
five rejected profit guards and two inventory-validation refusals**. The
eight complete closing-packet replies contain no successful closure. These
body-level diagnostics do not turn censored observations into economic returns.

Nothing was signed or submitted. No audit followed the failure, so final wallet
balance is unavailable, not verified unchanged. The cloud job, service account,
secret and local provider projection were removed; the owner's `ENVFILE` remains
mode 0600. The raw tape and 2,175 decoded payloads had no configured service-secret
matches. Further acquisition requires an explicit new HTTP allowance; a
continuation does not reset the cumulative meter.

Artifacts:
[`pacing diagnosis`](../../.state/paper-trading/chainstack-paired-260920-231215.pacing-analysis.json),
[`capture evidence`](../../.state/paper-trading/chainstack-cap64-260921-011504.analysis.json)
and
[`byte-boundary verification`](../../.state/paper-trading/chainstack-cap64-260921-011504.verification.json).

### CLMM reference-plan refresh (2026-09-21)

Offline reconstruction of all **57 dependency exclusions** in the capture above
found the same first-pool tick-array transition. Its required first array moved
from start tick `-21900` to `-21840`. The one missing snapshot account belonged
to the opposite, unused direction, but that does **not** make the simulated
packet valid: all 57 native replies returned **6024
`InvalidFirstTickArrayAccount`**, and every correctly rebuilt packet differed
from the one actually simulated. Neither dependency nor packet-equality checks
can be removed to recover these observations.

The shared event collector now uses decoded, freshness-checked snapshot
metadata as planning hints for the **next distinct trigger**. The current result
still fails the original checks; it is never retried or accepted using a rebuilt,
unsimulated packet. Cached lookup-table contents refresh only after same-bank hydration.
Each later response must independently pass the unchanged dependency,
same-bank, packet-equality, deadline, inventory and profit-guard checks.

Next-trigger account reads contain the current dependencies plus the original
watched cohort, bounded by the existing 100-account request limit. Historical
arrays do not accumulate. Subscriptions and trigger sampling stay fixed: in this
cohort, **36 watched accounts**, **48 read accounts instead of 47**, and a
734-byte first-quote packet. This is not adaptive coverage of newly selected
array accounts; the original watch-cohort coverage limitation remains.

The [executable dependency proof](../../.state/paper-trading/chainstack-cap64-260921-011504.dependency-proof.json)
reconstructs the frozen source/tape, reproduces all 57 failures, and checks the
later packet built by both the archived and current collector. All **57/57**
current planning transitions pass, versus **0/57** before the fix. Later replies
are deliberately forced to time out: these are packet-construction and
censoring checks, **not new native execution or profitability evidence**.
The original 96 censored outcomes and all sealed capture artifacts are unchanged.

Run from the repository root, without provider or wallet access:

```bash
uv run --offline --no-sync python -I -B -c \
  'import json,sys; from pathlib import Path; exec(json.loads(Path(sys.argv[1]).read_bytes())["runner_source"])' \
  .state/paper-trading/chainstack-cap64-260921-011504.dependency-proof.json
```

CLMM identity/ABI checks, collector self-checks, native accounting/freshness
controls and scoped Ruff checks also passed. This continuation made **zero
external requests**. Only **one reserved HTTP request** remains; further
acquisition requires an explicit new HTTP allowance.

### Approved supplemental pool lookup tables (2026-09-18)

Both event collectors now freeze `pool_lookup_policy`. After signed-receipt
discovery freezes the structural catalog, and before warmup, each admitted route
with AMM-v4, CPMM or Raydium CLMM pools makes at most **one** anonymous GET to
Raydium's existing `/pools/key/ids` endpoint. Only that route's pool IDs are sent.
Returned IDs must match the requested pools and programs; metadata prices and
vaults are not execution inputs. Receipt lookup tables retain their order;
pool-table hints are appended with ordered deduplication. `route_admitted` stays
unchanged and `route_lookup_tables` records both sources and the effective list.

Hints are not attestations. Every supplemental table enters the existing
same-bank account reads, owner/active-table validation and native packet
rebuild-equality checks. There is no historical-table cache, table-order
optimizer, route backfill or fallback to an unverified table. Null, absent,
empty and default-key hints are explicitly recorded as unavailable; malformed
identities or HTTP failures stop the capture without retry. A table that fails
on-chain validation cannot make its route ready.

The GET uses the shared HTTP budget, response-header pacing, body/tape bounds
and reserved audit capacity, with a maximum ten-second request deadline.
Authenticated, cookie-bearing, secret-configured or environment-authenticated
sessions are refused. Only this fixed public metadata response may retain
public token-logo URLs; private/native RPC redaction is unchanged. These reads
remain `rpc` journal events with explicit `request.method: GET`, no JSON-RPC
error codes, and HTTP—not native-simulation—accounting. Offline decoding uses
`decode_wire(record, scope=policy)` for these records.
The public request middleware also prevents aiohttp's implicit GET retry after
a disconnect. A real loopback regression observes two wire attempts under the
superseded draft and exactly one under the final source, with the same terminal
transport failure. Neither follows redirects or retries an HTTP error.

The offline verifier passes **36 existing lifecycle + 10 freshness + 24
metadata HTTP + 7 enrichment cases**. A retained-data smoke runs the new
collector's enrichment and guarded construction on all **15** previously
oversized banks: **1,416 → 1,146 bytes**, leaving **86 bytes** under the unchanged
1,232-byte cap. Its packets exactly match the earlier independently decoded
instruction/global-privilege equivalence proof. The three real tables were
attested at slot **448188098**, later than those market banks: this is explicitly
counterfactual serialization, not fresh native execution, faster quotes or ROI.

Only `pool_lookup_policy` and source hashes change in either collector policy;
all financial, freshness and resource limits remain fixed. The base/supported
policies and source archives are sealed under `pool-tables-20260918T215947Z`.
These are **freeze-only artifacts**: no additional observation capture was
started, signed or submitted. Proofs and exact verification output:
[`pool-tables-20260918T215947Z.analysis.json`](../../.state/paper-trading/pool-tables-20260918T215947Z.analysis.json).

### Public-RPC smoke evidence (2026-09-15)

Three separate bounded executions are retained, each with its exact
`.source.zip` beside the JSONL. They are not independent opportunity samples:

- `public-triangle-20260915-boundary-refusal.jsonl`: three HTTP requests, no
  simulations. The actual DRb8/USDC pool's spacing of 32896 exposed the old
  three-distinct-arrays restriction. The shared boundary fix above was reproduced
  offline before repair; the verifier now uses a full-range second pool.
- `public-triangle-20260915-native-smoke.jsonl`: seven HTTP requests and three
  unsigned native simulations for a 1,000,000-lamport input. The final packet
  reached its profit guard at slot 447249809: **890 bytes, 171,779 CU, 964.127 ms**
  through validation, two slots after the first prefix. Native quantities were
  373 DRb8 raw and 97,452 USDC raw. The guard rejected the cycle. Its diagnostic
  pre-guard balance was 112,775 lamports below the starting balance after modeled
  network fee and tip; the atomic result rolled back and `net_lamports` remains
  null. This is neither a fill nor a realized wallet loss.
- `public-triangle-20260915-size-sweep.jsonl`: thirteen HTTP requests and nine
  native simulations across the predeclared 100,000 / 1,000,000 / 10,000,000
  lamport sizes. All three candidates exceeded the two-slot quote-age bound.
  Their native diagnostics are retained but are **ineligible**, not accepted
  economic negatives or zero-return observations.

Every closing wallet audit was unchanged. No transaction was signed or submitted,
no bot was run, and no live configuration, risk limit or durable trading state
was changed. This establishes the native probe path and its refusal behavior,
not a positive-ROI strategy or a reason to relax the freshness bound.

### DAMM v2 route and evidence (2026-09-15)

The route is fixed from raw transaction **seq 2229, slot 446878901** in
`empirical-0913233451_solana_full.jsonl`. Selection uses the earliest retained,
fully decoded, known-flat ordinary-SPL three-leg cycle at or below the
10,000,000-lamport cap, with DAMM and already supported venues. Earlier seq 1657
has four legs and needs DLMM; excluding it is a scope decision, not an economic
failure. Full selection provenance, receipt verification, protocol identity,
tape hashes and bounded-run summaries are in
`public-damm-20260915-provenance.json`.

The historical input/output were 1,000,000 / 1,146,321 WSOL raw units. After its
7,393-lamport fee, the known payer ownership-union delta was **+138,928 lamports**;
that is one historical transaction, not portfolio PnL or our current quote.
The new builder does not copy its router, zero slippage floor or unusual
program-account placeholders. It emits canonical native `swap2` accounts from
the [pinned DAMM v2 source](https://github.com/MeteoraAg/damm-v2/tree/a85c926607433f23f0ea60f4ca7b1ae92f4156cb).
Vault identity, global authority, activation and ordinary-SPL ownership are
checked locally; swap math, dynamic fees and compounding execute natively.
The Instructions sysvar is supplied for rate-limited pools. Token-2022 is
unsupported in this probe. The source pin is not a deployed-binary attestation.

Two bounded public-RPC executions retain their six exact source files in the
adjacent `.source.zip` archives:

- `public-damm-20260915-native-smoke.jsonl`: **seven HTTP requests, three unsigned
  native simulations**, 1,000,000-lamport input. The final packet reached the
  profit guard at slot 447318971: **849 bytes, 103,537 CU, 1,016.063 ms** through
  validation, two slots after its first prefix. The guard rejected it. Its
  pre-guard diagnostic balance was **308,691 lamports below** the starting
  balance, including the unchanged 65,000-lamport fee and 10,000-lamport tip.
- `public-damm-20260915-size-sweep.jsonl`: **thirteen HTTP requests, nine unsigned
  native simulations** for 100,000 / 1,000,000 / 10,000,000 lamports. The first
  and third candidates reached the guard within freshness bounds and were
  rejected; their pre-guard diagnostics were **−96,405 / −3,944,353 lamports**.
  The middle candidate exceeded the two-slot quote-age bound and is **unpriced**,
  regardless of its retained native logs.

All four candidates retain null returns: three guard rejections and one
freshness refusal. Failed atomic simulations roll back; these diagnostics are
not fills or realized wallet losses. Both closing public-wallet audits were
unchanged. Nothing was signed or submitted, and no live configuration, risk
limit or durable trading state was modified. This adds native venue coverage,
not a profitable candidate or evidence of repeatable net $100/day.

At DAMM closeout, cleanup only relocated lint comments. The then-current six
source ASTs matched both captured archives; those archives remain unchanged.

### DLMM four-leg route and evidence (2026-09-15)

This covers the previously excluded **seq 1657, slot 446878617** in
`empirical-0913233451_solana_full.jsonl`; it does not replace the earlier DAMM
experiment. The historical path used 2,142,930 WSOL raw, returned 2,152,593,
paid a 5,000-lamport fee and a 1,400-lamport external System transfer, and ended
flat in its three non-SOL assets. Its known payer ownership-union net was
**+3,263 lamports**. Holding those historical outputs fixed but applying our
unchanged 65,000-lamport fee and 10,000-lamport tip gives **−65,337 lamports**,
not a current quote or an executable return.

`simulate_dlmm_cycle.py` follows the
[pinned DLMM SDK/IDL](https://github.com/MeteoraAg/dlmm-sdk/tree/576919e3e4368e542c402f000b4264724f7f23ec).
It emits canonical native `swap2`, including Memo and the empty remaining-slices
vector. Pool, vault, oracle, mint and array identities are checked locally.
The bitmap extension is fetched with the initial pool state; an explicit null
is distinct from an omitted dependency. Signed internal and extension bitmap
indexes select at most three initialized arrays per direction, without Orca-style
padding. This is a bounded coverage policy, not a protocol limit or a guarantee
that an amount can fill. Native execution handles prices, fees, legacy bin
versions and limit-order liquidity. Activation is checked against the same
bank's Clock. Token-2022 remains unsupported; the source pin is not a deployed
binary attestation.

The unchanged two-second/two-slot bound also applies to all four legs. A third
prefix remains unpriced; only the fourth packet can close all four ATAs and
reach the profit guard. Initial hydration retains the fresh dependency plan,
without treating Raydium's transient local quote caches as pool identities.
Offline checks cover signed bitmap transitions, both wire directions, present
and absent extensions, activation, missing dependencies, the four-leg boundary
and the existing cancellation/audit/failure matrix. An independent 2,448-case
flat-list comparison matched bitmap traversal. The pinned upstream binary
fixture independently checks pool/vault/oracle and supplied array identities,
but omits its bitmap-selected positive array 1: full hydration **correctly
refuses it**. It is not represented as a complete native fixture execution.

Two bounded anonymous public-RPC runs retain their **seven exact source files**
in adjacent `.source.zip` archives:

- `public-dlmm-20260915-native-smoke.jsonl`: **7 HTTP requests, 3 unsigned native
  prefixes**, historical-size input 2,142,930. The third prefix exceeded the
  two-slot age limit; no final packet was attempted.
- `public-dlmm-20260915-size-sweep.jsonl`: **14 HTTP requests, 10 unsigned native
  simulations**, inputs 100,000 / 1,000,000 / 10,000,000. The first two reached
  all four swaps and four ATA closes, then failed the final balance guard at
  slots 447332994 / 447332999. Both packets were **1,118 bytes**, consuming
  **170,883 / 170,296 CU**. They were also five / four slots after their first
  prefixes, so both candidates are **freshness refusals**, not eligible economic
  negatives. Their reverted, stale pre-guard diagnostics were −75,096 /
  −77,797 lamports including modeled fee and tip. The largest input was refused
  after its second prefix left **97 JUP raw units** unconsumed.

All four candidates retain null returns: three freshness refusals and one
inventory refusal, no unattempted sizes. Across **21 HTTP requests / 13 native
simulations**, nothing was signed or submitted. Both closing audits found the
public wallet unchanged at **562,213,411 lamports**, with all four route ATAs
absent. Limits, live configuration and durable trading state were untouched.
Raw receipt hashes, protocol/fixture qualifications, tape/source hashes and
per-packet diagnostics are preserved in `public-dlmm-20260915-provenance.json`.
This closes a native coverage gap, not the evidence gap for profitable execution
or repeatable net $100/day.

### Freshness diagnosis and retained-route discovery (2026-09-15)

`public-cycle-discovery-20260915-provenance.json` preserves the offline methods,
full retained-population screen, raw identities, timing analysis and new native
packet audits. Earlier tapes, locks and holdout reports remain unchanged.

Across the earlier **37 simulations / 12 candidates**, median admission wait was
**188.6 ms**, request-to-headers **112.6 ms**, and response-end-to-accepted-stage
**2.7 ms**. The DLMM third-prefix body intervals were **612–645 ms**; replaying
each captured DLMM body's JSON/secret/response checks 25 times took median
**0.9–2.2 ms** per body locally. RPC `end_ns` excludes subsequent compression,
tape serialization/flush and native checks. The timings do not isolate server
from network delay, and overlapping local intervals must not be added together.
All **ten completed guarded packets** were still negative after adding back
only their 65,000-lamport network fee and 10,000-lamport tip, holding captured
outputs fixed. This does not retime a quote or change stale eligibility.
Removing safety checks or optimizing a few local milliseconds is not supported
as a path to profit by these observations.

Both retained Solana tapes were streamed completely: **16,625 + 17,266 receipts**.
Of 2,652 known, flat, reconciled receipts without external System SOL funding,
206 had aggregate owned WSOL debits at most 10,000,000; six had historical gross
WSOL flow at least 76,000. Four matched supported, distinct-pool 2–4-leg cycles
whose intermediate transfers exactly funded the next spend. Two were already
probed; two became new fixed routes. The other two cost-covered records remain
excluded for an unsupported venue or a nonmatching swap/owner-flow shape.
This is discovery on a censored retained population, not a market-wide no-go
or another heldout profitability result.

The new reference traders had unchanged intermediate holdings, including
**78,684.285122 USDC** in the Orca case—not necessarily dust. Replaying the
recorded transfer arithmetic from zero intermediate balances established that
neither selected route needed those holdings. The fresh native probes still
require all route ATAs absent and exact inventory closure.

The catalog adds `--route orca-amm` and `--route dlmm-damm` using existing
adapters only. `public-cycle-discovery-20260915-lock.json` froze one attempt at
each historical input, in replication sequence order, with every existing
financial, pacing, freshness and no-retry limit unchanged:

| Route | Replication seq | Input lamports | New observed outcome |
| --- | ---: | ---: | --- |
| Orca → Orca → Raydium AMMv4 | 4053 | 9,779,771 | Second Orca leg: native SPL insufficient funds; no third leg |
| DLMM → DAMM v2 → DAMM v2 | 5912 | 2,870,073 | All swaps and three closes reached the rejecting profit guard; quote age 3 slots |

The Orca first swap's bytes and resolved accounts were identical across the
two prefixes. Its emitted acquisition fell from **952,877 to 952,840 USDC raw**
between slots 447345247 and 447345248, while the second input remained 952,877.
The **37-unit shortfall** is decoded from the
[upstream `Traded` event layout](https://github.com/orca-so/whirlpools/blob/main/programs/whirlpool/src/events.rs)
and corroborates the native insufficient-funds error. Pre-existing inventory
must not subsidize it. The second route's final packet was **1,184 bytes /
113,091 CU**; its stale, reverted pre-guard diagnostic was **−218,871 lamports**
including fee/tip, or **−143,871** with just those two costs added back.
Neither candidate has an accepted return; these are not paid losses.

The `public-cycle-discovery-20260915-{orca-amm,dlmm-damm}.jsonl` tapes and their
seven-file `.source.zip` archives retain **13 HTTP requests / five unsigned
simulations**, below the locked 14/six limits. Both closing audits found
**562,213,411 lamports** and all three route ATAs absent. Nothing was signed,
submitted, retried or changed in live configuration or durable trading state.
Scoped Ruff, the existing public/atomic/paired self-checks, frozen-catalog screen
replay and independent packet/archive audits passed. No temporary analyzer
files or scratch directories were created. Profitable executable opportunity
frequency and repeatable net $100/day remain unestablished.

### Learning applied: native amount propagation and bounded acquisition (2026-09-15)

The previous 37-USDC-raw failure was not missing transaction atomicity: both
swaps were already in one packet, but the second amount came from a different
simulation. Winning receipts R4053, R5912 and F2229 construct sequential CPIs
with each intermediate spend exactly matching its acquisition. Their outer
payloads do not contain those later amounts as ordinary little-endian u64s.
This supports runtime amount propagation, not an identified private-router
algorithm or permission to replay another actor's nonce, inventory or authority.

Two changes now apply to both Orca-leading routes:

1. `simulate_orca_cycle.two_hop_swap` uses the
   [canonical native handler](https://github.com/orca-so/whirlpools/blob/main/programs/whirlpool/src/instructions/two_hop_swap.rs)
   to compute its internal intermediate amount, removing the separate first
   quote and its quantity mismatch.
2. The guarded packet reuses the existing exact-output-pair budget policy:
   acquire exactly the bridge quantity that the final fixed-input leg needs,
   for no more than the original funded SOL. This removes surplus/shortage at
   that boundary without a generic router, dust tolerance or extra capital.
   Worse prices may still reject the maximum-input bound or final profit guard.

`public-orca-two-hop-20260915-lock.json` fixed one attempt at 9,779,771 lamports.
Its native smoke used **six HTTP requests / two unsigned simulations**:
the two-hop quote emitted **947,074 USDC raw out and exactly 947,074 in**,
leaving balances `[0, 0, 1616143002]`. The final packet requested exactly
1,616,143,002 bridge units with a 9,779,771-lamport maximum input; Orca rejected
it with **6037 `AmountInAboveMaximum`** before Raydium or account closes ran.
This was a **fresh native refusal**, after **509.795 ms / two slots**, not a
completed profitable cycle. The input cap was not raised and no retry followed.

Scoped Ruff and all three existing public/atomic/paired checks passed. The
regression now checks both two-hop modes, exact guarded output/cost bounds,
zero residual inventory, reduced request accounting and existing failure/audit
transitions. The real local-VM control still proves refunding all funded WSOL
and rent cannot mint profit. The closing public audit retained **562,213,411
lamports**, all three ATAs absent, zero signing/submission and unchanged live
configuration. Exact runtime sources and decoded native proof are retained
beside the smoke tape and in `public-orca-two-hop-20260915-provenance.json`.
Execution consistency improved; a current profitable opportunity and repeatable
net $100/day remain unproved.

### Learning applied: reject economically impossible snapshots (2026-09-15)

Offline analysis of the two-hop smoke, earlier Orca-AMM discovery, and both
triangle tapes found **20 complete account snapshots / seven distinct price
states**, all with `P < 1`. Even ignoring every venue fee and price impact,
none could generate positive gross profit at any permitted spend. Increasing
the acquisition cap would not repair those observed states.

The new screen was exercised through the actual CLI against loopback replay of
those retained account responses: **six candidates across four runs**, each
run completing in **four read-only HTTP requests**, with **zero native
simulation calls**. All candidate net returns remain null. These tapes
originally contained 16 simulations across different implementation versions;
that historical count is not a measured latency improvement or a new native run.

The existing offline check now defends inverse prices, owed input fees,
capped-spend refunds, fractional-lamport flooring, the +1,000 equality boundary,
expired snapshots and zero-simulation rejection with a closing wallet audit.
The native positive-result path remains required when a snapshot passes.
`public-orca-spot-screen-20260915-provenance.json` retains the exact ratios,
hash-checked inputs, runnable replay proof and source identities. No new public
RPC observation, signing, submission, fee/cap increase or live-state change was
needed. Profitable execution and repeatable net $100/day remain unproved.

### Cost screens and a native-USDC forward study (2026-09-16)

The positive-daily-return mission remains open. The running token paper learner
is independent of this research; its losses, cash and unknown inventory are not
reset or funded by counterfactual results.

At paper event cutoff 35,383, 546 complete paired cohorts remained negative even
after optimistically returning all 2.1m lamports of modeled rent: mean losses
were 546,329 / 474,014 / 709,607 lamports for the 10s / 30s / 60s exits.
None of 15 initial-liquidity/exit combinations cleared the training uncertainty
screen. Ten delayed-entry momentum/reversal combinations also failed. These
are chronological posthoc rejection screens, not prospective holdouts.
The exact methods and inputs are retained under `.state/paper-trading/roi-*`.

Anonymous Hyperliquid funding and Deribit/Kraken dated-futures screens did not
establish a small-capital, complete-cost daily edge. A gross futures premium is
not profit: the short needs separate collateral; delivery pays cash PnL rather
than consuming the spot holding; spot liquidation, settlement-index mismatch
and withdrawal costs remain. Ordinary fees, not VIP rates, were used.

`record_supply_returns.py` now observes a different, frozen hypothesis: a
50-USDC direct supply position in Kamino's existing Main Market USDC reserve
`D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59`, without borrowing or rewards.
It signs and submits nothing. Its only transaction RPC is an unsigned,
`sigVerify: false` simulation of transfer-free `RefreshReservesBatch`, using the
public readiness-wallet address as simulation payer. No key or wallet is loaded.
This executes native accrual at the finalized bank clock rather than pretending
stale stored interest or a displayed APY is an executable entry.

The observer freezes floored shares and ceiled debit, retains all three native
fee exclusions, uses the reserve's collateral denominator even when standalone
SPL burns reduce mint supply, and checks both native oracle validity masks.
Fixed shares/debit are overlaid on later reserve marks; unchanged external
activity/rates remain a disclosed small-position approximation. Withdrawal
liquidity/caps are screened conservatively without crediting hypothetical
deposit relief. Unavailable exits have no net-return figure, not a zero return.

Current anonymous fee queries returned 5,000 lamports per one-signer
deposit/setup or redeem/close message and **1,488,440 lamports** for a new
165-byte account's rent exemption. The older 2,039,280 figure in documentation
does not match this node's current rent quote. These are fee/rent queries,
not proof that deposit or redemption executed. The study retains 10k/30k/85k
lamport round-trip fee sensitivities, refundable collateral-account rent, USDC
price changes and native-SOL capital/fee FX. It assumes an existing funded USDC
account. Acquisition/off-ramp, actual execution/retries and future exit remain
unpriced; bad debt, depeg and upgrade risk remain real.

```sh
uv run learning-examples/token-lifecycles/record_supply_returns.py --self-check
# One current no-transfer simulation; output must not already exist.
uv run learning-examples/token-lifecycles/record_supply_returns.py \
  --once --out .state/paper-trading/native-usdc-snapshot.jsonl
# Frozen seven-day observation, one request every five minutes.
uv run learning-examples/token-lifecycles/record_supply_returns.py \
  --out .state/paper-trading/native-usdc-forward.jsonl
```

The exclusive, fsynced JSONL contains its exact source/definition, raw responses,
fixed entry, marks and coverage failures. A seven-day run allows two extra
sampling intervals to reach the chain-clock boundary; it fails if no usable
mark/target is obtained. Layout, authority, oracle or reward changes fail closed.
Transport gaps are retained and prevent complete-coverage claims. It never
marks all-in profit proven or changes the existing paper portfolio. Six
historical API exchange-rate windows suggested only about 0.0052–0.0055 USDC
gross per day per 50 USDC, before fees/FX; that is neither $100/day capacity
nor a forward result. Sources: [native accounting](https://github.com/Kamino-Finance/klend/blob/a08760976f51a3a58c4a0c6ea27b4a0e565bca79/programs/klend/src/state/reserve.rs),
[direct supply](https://kamino.com/docs/products/borrow/supplying.md),
[fees and rates](https://kamino.com/docs/products/borrow/fees.md).

The existing native swap helpers also measured two cold SOL→50-USDC→SOL
round trips, without signing or submission. The older retained triangle's
stablecoin pool first failed the 50-USDC liquidity prerequisite, before any
simulation. A liquid Raydium AMM cycle then consumed 63,517 CU and lost
2,545,086 simulated lamports; a lower-fee Orca cycle consumed 81,257 CU and
lost 412,371. Each included a 5,000-lamport simulated fee and recovered both
new token-account rents. Public wallet reads remained **562,213,411 lamports**
with both accounts absent. These are immediate conversion-cost observations,
not lending executions, future exits, guaranteed fee budgets or real fills.

At each pool's own observed spot, the losses were about 0.250752 and
0.040499 USDC. Compared with the six historical API windows' 0.0053625-USDC
mean daily gross, even the cheaper route takes about **7.55 days just to cover
conversion**, or about **9.02 days** with an 85k-lamport total-fee sensitivity.
That calculation assumes unchanged rates/prices and does not establish a
profitable week from the readiness wallet. The seven-day warm-USDC benchmark
is not a cold-wallet profit claim or a reason to deploy funds. Full raw probes,
failed prerequisites, source, reviews and arithmetic are retained in
`.state/paper-trading/roi-native-supply-20260916T181619Z.json`.

**Study stopped incomplete on 2026-09-17 at 03:24:27 UTC.** Its 114th native
simulation succeeded, but the returned SOL reserve had oracle-validity flags
`0`, versus the required `63`. Its saved SOL price was **220 seconds old**
against the reserve's **120-second** maximum. The observer refreshes USDC
interest without price updates and reads SOL separately for fee/rent FX; it
does not refresh the SOL oracle. Native refresh success therefore did not make
this valuation usable. The existing guard correctly stopped the run.
The journal retains 113 valid marks spanning **33,676 chain seconds**
(9.3544 hours); the last **0.002744 USDC** nominal gross accrual is neither a
terminal exit nor paid profit. Neither the 24-hour nor seven-day target was
reached. The frozen source, entry and
`.state/paper-trading/native-usdc-forward-20260916T175810Z.jsonl` remain
unchanged. The failed observation is not discarded or restarted with a new
entry, and the independent paper/carry observers are unaffected.

### Native carry execution and a frozen counterfactual (2026-09-17)

The SOL-collateral / short-SOL-perpetual candidate now executes its complete
Velocity lifecycle in one **unsigned** native simulation: acquire 0.5 USDT,
deposit 0.5 SOL plus that buffer, fully short 0.56 SOL, settle funding while
open, fully close, settle PnL, withdraw and delete the User. No transaction was
signed or submitted. UserStats and the returned-USDT account retain rent;
the cash-cost calculation includes it rather than assuming it is recoverable.

The original **15-bps-from-oracle** admission remains rejected. Separately
registered **15-bps-from-native-maker-quote** diagnostics are not passes of that
rule. All three fixed timing samples were retained: cold cash costs were
0.645154 / 0.489312 / 0.489467 USDT. The expensive cycle used makers on both
legs; the cheaper cycles entered through the AMM and exited through a maker.
This is execution evidence, not proof of an available or repeatable edge.

`record_carry_returns.py` records a distinct, preregistered counterfactual.
It freezes the first qualifying scheduled native observation, preserves the
entry short and scaled spot balances, then samples hourly for seven real
chain days. It accounts for signed funding, actual native close quotes,
SOL/USDT basis and FX, retained rent, and separate exit/failure fee reserves.
Unsettled positive PnL is not paid cash. A depleted USDT buffer does not become
an automatic sale of SOL collateral: unavailable liquidation value stays null,
with the signed indicative claim/liability reported separately.
Fresh native round trips are not simulations of a held account: the
counterfactual's funding receipt, margin health and future withdrawal
availability remain unproved.

```sh
uv run learning-examples/token-lifecycles/record_carry_returns.py --self-check
```

Observation requires explicit definition, probe and input fingerprints.
New journals are exclusive; every raw observation is fsynced before valuation.
HTTP 401/403/429, wallet-context changes and deployment changes stop collection.
Missed observations are retained, not recreated. A trustworthy seven-day
clock ends the run even when its terminal valuation is unavailable.

The first prospective cycle at **2026-09-17 02:49:08 UTC** cost **0.492058 USDT**
and returned **0.361425 USDT of principal**, not funding income. It exposed a
one-micro-USDT accounting error: internal PnL transfers use floored deposit
scaling; the extra scaled unit applies only when funds leave the protocol.
The corrected recorder recovered that same retained first observation without
another quote or entry selection. Its original failure, source versions,
request counts and schedule remain in the append-only journal. Holding time
starts at the actual short-fill timestamp, not the preceding account-read time.
`--recover-startup` is restricted to this fully observed startup failure;
it cannot reset an existing entry or recover a partial/unknown native outcome.

The local definition and journal are
`.state/paper-trading/carry-quote-forward-definition-20260917T022525Z.json` and
`.state/paper-trading/carry-quote-forward-20260917T022525Z.jsonl`.
The 24-hour and seven-day boundaries are **September 18 and September 24,
02:49:08 UTC**, evaluated at the first scheduled trustworthy observation on
or after each boundary. The earlier paper and USDC studies remain unchanged.
Neither paid funding, a profitable held-position exit nor $100/day capacity is
proved. Rounding reference: the pinned
[SDK spot-balance implementation](https://unpkg.com/@velocity-exchange/sdk@0.20.0/lib/node/math/spotBalance.js).

**UTC schedule correction (2026-09-21).** An offline replay reproduced all
49 retained carry marks and found 27 observation attempts started 941–945 seconds
after their frozen UTC schedule, beyond the 180-second tolerance. The recorder
had checked lateness only against monotonic time. It now schedules against the
original UTC anchor, rejects early clock reversals, and bounds sleeps and total
runtime with the unchanged monotonic deadline. Missed observations are not
retried or shifted to a new schedule.

```sh
uv run learning-examples/token-lifecycles/verify_carry_schedule.py
```

The regression exercises the real recorder loop without network access or a
native child: late observations are skipped, the next UTC slot stays fixed,
backward clock changes cannot extend runtime, and the hard wall-time limit holds.
The old definition, journal, entry and negative/unavailable valuations are
unchanged. Its last mark is still only 50.26 hours after entry, not a completed
seven-day study. No observer was restarted. Full offline mission evidence and
replay commands are in `.state/paper-trading/mission-evaluation-20260921.json`;
neither the paper learner nor these carry/cycle observations establish repeatable
positive daily net return.

### Three-capture six-venue batch (2026-09-21)

An authorized batch of three bounded London Chainstack captures used the
unchanged frozen protocol with a 400-request per-capture cap: **399 / 91 / 66**
HTTP requests, 267 native simulations, **321 immediate decisions** (312
censored, 5 screened below the profit floor, 4 native program failures),
**zero policy episodes and zero qualified positive native results**. Captures 2
and 3 completed and audited the public wallet unchanged at 0.562213411 SOL;
capture 1 stopped at its request cap. Six other-actor closed cycles were
observed in sampled receipts (118 to 315,714 lamports); they are historical
signed receipts, not our executable returns. Cumulative budget: 1,081 of 1,726
requests. Temporary cloud job, secret, service account and the local
service-only projection were deleted afterward. Evidence, replay sources and
seal: `.state/paper-trading/chainstack-batch-26092106380.{analysis,seal,verification}.json`
with `-r1..r3` tapes. Nothing was signed or submitted; no repeatable profit
is proved.

### Venue cycle census and decision-latency diagnosis (2026-09-21)

`record_venue_cycle_census.py` is a Geyser-only recorder (zero HTTP, no signing)
that accounts delivered confirmed transactions referencing its configured programs
with `verify_solana_actor_receipts.account_receipt`. The venue profile includes
Orca Whirlpool, PumpSwap, Raydium AMM v4/CPMM/CLMM and Meteora DLMM.
This is **not the same scope as the six-venue native cycle study**: it includes
PumpSwap rather than DAMM-v2. Keep both historical scopes intact.

A 120-second London smoke retained 72,719 compact receipts (573 MB wire),
including 400 plausible closed cycles by 73 payers. Those cycles sum to
461,771 lamports; captured failed-transaction fees for the same payers sum to
31,968,457 lamports. The difference, **−31,506,686 lamports**, covers those
categories only, not full portfolio PnL or our executable returns.
The replay identifies 45 positive and 28 nonpositive cycle actors after those
captured fees; these are post-hoc outcomes, not a forward selection rule.
Historical tip totals can include reverted transfers. A transfer to a Jito tip
account does not prove bundle membership, nor does a low priority fee identify
the sender's transport.

The running census schedules twelve 120-second windows every two hours with
cycle-only retention and an 8 GiB aggregate wire ceiling
(`venue-census-260921-1121.*`, execution `venue-census-260921-0802-jcc58`).
This is intermittent program-filtered sampling, not full-day market coverage.
The frozen collector can still schedule later windows after a failed window;
do not assume hitting the wire ceiling immediately ends the Cloud Run execution.

**Accounting correction (2026-09-22):** cycle-only retention omits non-cycle
failed receipts, so per-actor failed fees and fee-adjusted winner rankings are
unknown, not zero. `--analyze` now returns null for those fields, retains all
cycle actors, labels recomputed totals as retained-receipt totals, and exposes
recorded cumulative summaries separately. Recorded summaries include omitted
receipts but cannot be fully replay-verified from a cycle-only tape.
`verify_census_accounting.py` checks that a positive cycle followed by a larger
failed fee is a loss with full retention and has unknown net with partial
retention. Historical tapes, archives and analysis artifacts are not rewritten;
the corrected full-smoke replay is
`venue-census-260921-0802-smoke.accounting-correction-20260922.json`.

**Backrun disposition:** the old `smoke.backrun.json` labels describe only
receive-order/same-slot/venue overlap. Its 113/400 candidates are not proven
backruns. A streaming audit retained in
`venue-census-260921-0802-smoke.raw-coverage-20260922.json` verified raw payloads
for all 400 cycles and none of the 72,319 non-cycle receipts. All raw cycles
carry nonzero transaction indexes, and four within-slot receive-order
transitions reverse index order. Antecedent block positions, pool identities
and swap traces cannot be recovered from their compact rows. Causal backrun
research needs those antecedent payloads plus aligned market-state evidence;
no new acquisition or execution is authorized by this offline audit.

**Route completeness (2026-09-22):** a follow-up decode of the retained smoke
cycles (`venue-census-260921-0802-smoke.pool-evidence-20260922.json`,
`...damm-legs-20260922.json`) identified 651 classified venue swaps across 229
pool addresses. Exactly 100 of the 400 cycles share a pool with another payer
in the same slot; 63 follow another payer by provider transaction index. These
are overlap measurements, not backrun or profit claims.

The venue map is incomplete for routes: 218 cycles contain only one classified
venue swap, and those transactions also invoke programs outside the six-venue
set — most often Jupiter (131), BisonFi (112), the DFlow Aggregator DF1ow4…
(78), Meteora DAMM v2 (33) and TesseraV (27); counts overlap because a
transaction can invoke several.
DAMM v2 legs are now decoded offline with official layouts: 42 swap legs across
20 pools, 33 of them forming two-leg routes with exactly one classified venue
swap (PumpSwap 24, Meteora DLMM 7, Raydium CLMM 2). Anchor discriminators are
name-hashes: DAMM v2 `swap` shares `f8c69e91e17587c8` with DLMM `swap`, so
program identity, not discriminator alone, separates venues. Co-presence
describes route shape; token-level chaining is not established.

External legs are now decoded from the retained cycles
(`...external-legs-20260922.json`): BisonFi 128 swap legs in 125 cycles and
TesseraV 69 legs in 69 cycles — both entirely inner CPIs, all five BisonFi
pairs and five TesseraV markets identified, with the pmm-sim documented
Tessera market `FLckHL…` carrying 56 legs. DFlow Aggregator swap/swap2
instructions appear in 87 cycles (its `Vec<Action>` route payload is not yet
decoded — that Borsh walker is the remaining prerequisite). BisonFi `swap2`
appears on chain as 19 bytes/9 accounts versus the third-party shank IDL's 18
bytes, so only pair identity (account 1) is read for tag 7. These layouts are
third-party extractions for closed-source programs, not official
attestations. Label conflict recorded: Jupiter labels `ojh19ojaK…` "Scorch",
while Solana Foundation's slot200 registry labels it "Arb bots" and
`SCoRcH8c2d…` "Scorch"; neither is treated as verified. Per-venue cycle counts
remain a floor, not the population.

**Census completion and pacing outcome (2026-09-22):** the twelve-window
census finished at 09:22 UTC: ten complete windows, five thousand seven
hundred eighty-three plausible closed cycles, aggregate cycle gain
4,158,458,831 lamports (p50 8,347, p90 159,675, max 1.44 SOL), 3,685
Jito-tipped cycles carrying 511,040,035 lamports of tips against 526,201,573
lamports of their cycle gain. Windows 10–11 hit the 8 GiB wire ceiling
(1,040,257 transactions, 121 requests total) and terminal `window_failures`;
the collector's exit 2 is the designed censor path. Failed fees per actor
remain unknown under cycle-only retention. Tape
`venue-census-260921-1121.jsonl` (sha256 `c8addef8…`) was reassembled from
execution-log `evidence_chunk` lines and verified against the in-band
`evidence_complete` hash; retrieval provenance and analysis in
`census-retrieval-20260922T1015.json`.

The approved 100 ms pacing capture ran 09:24–09:26 UTC and finished clean:
**25 of 400 HTTP requests used, 0 signed, 0 submitted.** Of 699 log
notifications, 543 had no supported invocation family and 80 were failed
transactions; 12 matching families produced 12 receipt requests, all excluded
before qualification (9 receipt accounting, 2 swap owner-flow count, 1 owner
transfer outside a recognized swap); the observation phase then reported
`no_current_supported_routes`. **This is the fourth consecutive capture with
no qualified positive native result** (three-capture batch + pacing). No
further captures proceed without a new user decision.

**Full-tape decode (2026-09-22):** the raw payloads retained for the first 50
cycles per window (536 of 5,783 cycles) decode with zero classification gaps
(`venue-census-260921-1121.full-decode-20260922.json`): 870 classified venue
swaps across 382 pools, 150 cycles sharing a pool with another payer in the
same slot and 93 following another payer by provider transaction index. The
busiest pool is Orca Whirlpool `83v8iPy…` with 42 participating cycles by 20
payers. External legs persist at census scale: BisonFi 121 cycles (top pair
`8FnX3xo…`, 105 legs), DAMM v2 53 cycles across 45 pools, TesseraV 50 cycles
(`FLckHL…` 43 legs), DFlow 61 cycles. Aggregate per-venue population statements
remain floors: 5,247 of 5,783 retained cycles are compact-only and cannot be
pool-decoded.

**Population mining and strategy hypotheses (2026-09-22):** mining all 5,783
census cycles (`census-population-mining-20260922.json`) shows an extremely
concentrated, persistent winner population: 166 of 185 cycle payers are
net-positive, the top 10 hold 89.8% of positive gain, and 93 payers post ≥5
positive cycles across ≥3 windows. Untipped cycles carry 3.63 of the 4.28 SOL
of positive gain (p50 31,654 lamports vs 4,791 for tipped cycles); tipped
cycles' aggregate tips equal 97.1% of their aggregate gain, so tips only pay
at whale scale. Route shapes in the raw subset: 1 recognized leg plus
aggregator/Prop-AMM legs lead on per-cycle gain (255 positive, p50 17,131
lamports) but require closed-source pricing; **2-leg/2-pool cycles across the
six venues + DAMM are the largest fully-simulable cohort** (154 positive, p50
10,379; DAMM+PumpSwap and DLMM+Orca pairings included) — while the frozen
protocol's 3/4-leg families targeted the thinnest segment (p50 ≈ 2,500).
Zero same-pool multi-leg cycles were observed.

Decision-ready, not executed: (P1) extend the modeled family set to 2-leg
cross-pool cycles — the native simulators already build them; (P2) evaluate
untipped or market-tier priority-fee submission for small sizes (untipped
winners are the majority in both dominant shapes). Both are frozen-protocol
changes and, with any further capture, require explicit user approval. The
aggregator/Prop-AMM leg stays out of scope: no public pricing model exists for
the closed-source programs.

**P1+P2 capture (2026-09-22, user-approved):** the pipeline was extended to
2-program families and untipped economics (three leg-count literals, the tip
constant and the freeze tuple; the guarded tip transfer became conditional
while closes and the wallet guard stayed). All offline gates pass: the full
verify suite, both collector self-checks — including two retained DLMM↔DAMM
DFlow controls that now pass the log screen but still fail receipt
qualification — and the frozen `check_policy` pre-flight on the rebuilt
package ([validation](…p1p2-package-validation-20260922.json)).

Capture outcome (`chainstack-p1p2-260922-1520-vtfpp`,
[evidence](…p1p2-260922-1520.evidence-20260922.json)): **the first route
admission in five captures.** Twelve log families matched — the widened screen
let four 2-leg families through — eleven receipts failed the unchanged
flow/accounting gates, and one 3-leg route (CPMM → DAMM v2 → Whirlpool) was
admitted, warmed and driven into live observation with 25 native simulations
at three sizes. Every one of the 34 live triggers was censored (13 RPC bank
behind Geyser `-32016`, 10 quote age, 7 event-deadline, 4 bank/interval age),
so no guarded final simulation completed and no episode opened. The economic
gate remains blocked by live latency, not qualification: 85 of 400 HTTP used,
zero signing, zero submission.

**Win attempt at renewed 100 ms pacing (2026-09-22, user directive):** the
identical frozen package ran with capture-specific 100 ms response-header
pacing ([renewal](…p1p2-win-260922-2000.renewal.json)). The observation
**completed end to end for the first time**: 149 live decisions, 254 native
simulations, completed-decision lag p50 **456 ms** inside the 2 s deadline —
the latency blocker is closed. A 4-leg WSOL → JUPY → USDC → 9BB6NF → WSOL
route (Whirlpool ×3 + DLMM) was admitted, and its 46 completed guarded final
simulations were **all rejected by the wallet guard**: the live cycle priced
below the +1000 lamport floor at every trigger. Seven further simulations
failed on-chain (Whirlpool 6037 ×5). Censors persisted only as bank-lag
`-32016` (93), which censors the current observation without retry. Budget:
266 of 400 HTTP used; 1,457 of 1,726 total; 269 remain; zero signing, zero
submission ([evidence](…p1p2-win-260922-2000.evidence-20260922.json)).

**Final approved window (2026-09-22):** the remaining 269-request window
(`chainstack-final-260922-2040-2bzzf`) landed in a quiet stretch: 21 HTTP
requests, 8 receipts all excluded on the unchanged flow/accounting gates, one
3-leg route admitted but never warmed (0 simulations). Budget after five
captures and this window: **1,499 of 1,726 used; 248 remain**. A TypeSafe
judgment over the sealed state (user-authorized) ranked a **multi-route window
protocol change** first (0.51) with unchanged windows near-worthless (0.04)
and budget sufficiency **weak (1.06/4)** — the binding constraint is the
receipt qualification rate (1 of 8–12 per window), not the catalog cap of 4.
Consecutive single-route windows are luck-dominated against a bursty,
top-10-concentrated market; sampling more routes per window requires a
protocol change and a user decision
([evidence](…chainstack-final-260922-2040.evidence-20260922.json)).

**Sixth window (2026-09-22):** `chainstack-win2-260922-2200-jhnss` stalled at
57 s on the designed no-reconnect Geyser censor (`geyser_real_update_timeout`)
with 9 HTTP requests spent, 4 receipts (2 unavailable, 2 accounting
exclusions), no catalog route and no simulations. Budget: **1,508 of 1,726
used; 218 remain**. Two consecutive windows have now produced a quiet stretch
and a transport stall — sampling single routes against a bursty market is
luck-dominated, as the TypeSafe judgment scored (1.06/4). Everything within
standing approvals has been fired; the remaining 218 requests are inside the
standing allowance but each further window and any multi-route protocol
change is a user decision
([evidence](…chainstack-win2-260922-2200.evidence-20260922.json)).

**P3 multi-route window (2026-09-23, budget increase authorized):** discovery
extended to five 40s rounds and the catalog cap widened to 6; observation
needed no change (already route-agnostic). The window sampled **20 receipts
and admitted zero** (17 accounting, 2 owner-flow, 1 flow-count exclusions);
observation reported unavailable. Campaign-wide receipt qualification is
**1 of 59** (3.4%). Combined with the win window's 46 guarded re-evaluations
pricing below floor, the structural finding is: cycle profits decay inside
the originating slot — the census shows winners landing in the same slot as
their trigger (150 same-pool-same-slot cycles, 93 later-index wins), while
the paper-capture's re-entry arrives one to two slots behind through bank lag.
Capturing same-slot displacement requires signed submission, which the
frozen simulation-only protocol prohibits. Budget: 1,549 of 2,408 used; 859
remain; zero signing, zero submission
([evidence](…chainstack-p3-260923-0015.evidence-20260923.json)).

**Displacement decay (2026-09-23):** the retained census raw cycles measure
profit decay by arrival position inside a block. Same pool and slot, cycle
versus cycle: **first arrival p50 69,536 lamports (97.4% positive) versus
later arrivals p50 15,279 (84.1% positive, 16% losses) — a 4.5x decay within
one block**, over 78 contested pool-slots. Next-slot arrival (the paper
capture's 1–2-slot bank-lag reality) is strictly worse than later-in-slot.
Same-slot submission is the structural requirement for the measured edge; the
bot's extreme_fast_mode path (Geyser trigger → zero-RPC submit) is the
existing infrastructure designed for it, and the bounded live-readiness test
is the AGENTS.md-gated way to measure the bot's real trigger→submit latency
([evidence](…displacement-decay-20260923.json)).

**Bounded live-readiness attempt (2026-09-23, user-authorized):** the full
AGENTS.md gate sequence ran — status clean, preflight `ready: true`,
`--authorize-live`, one-shot config. The session **failed safely at startup**:
`validate_wallet` raised `ExecutionBlocked` because the signer derived from
`.state/wallets/live-readiness.secrets` is `41c4pmfs…` while the configured
expected wallet is the designated `9MFfWXdT…`. No signing, no submission, no
funds moved; `enabled` restored to false; durable status unchanged (11
submissions, 0 unresolved). Remediation is owner-only: reconcile the secrets
file so its keypair derives the designated wallet
([evidence](…live-readiness-attempt-20260923.json)).

Second attempt same day failed identically (`41c4pmfs…` signer) and an
authorized search found **no keypair for the funded `9MFfWXdT…` wallet
anywhere on this machine**: `.env` (one key field), `ENVFILE`, the
live-readiness secrets, `~/.config/solana/id.json` and the empty `.env~` /
`.env.example` placeholders all derive `41c4pmfs…` (0 lamports), while
`9MFfWXdT…` holds 0.562 SOL and the 11-submission ledger. Owner must supply
the `9MFfWXdT…` keypair into the secrets file, or fund `41c4pmfs…` and
explicitly re-designate it as the live-readiness wallet.

**Bounded live-readiness session completed (2026-09-23, user-authorized):**
after fixing the hub-environment key override (`SOLANA_PRIVATE_KEY` exported
from the supervisor shell takes precedence over dotenv in
`config_loader.py:285`; a wrapper unsets it before exec), the one-shot session
ran live: Geyser listening, CreateEvent parsing, entry-gate flow. Timeline:
two gate skips on timeouts (buyers=0), then **"/" accepted after 410 ms**
(buyers=1, curve 0.151 SOL); a buy of 250k tokens costing 0.007150 SOL was
submitted and **failed on-chain with 6002** (slippage bound exceeded — price
moved past the bound between quote and submission). The bot shut down cleanly
(max_attempts 1). This is the displacement-decay mechanism observed live:
the bot's gate decision is same-slot fast (410 ms), but the fill lost the
price race. Cost: 33,000 lamports of fees; tokens received: none; ledger
reconciled (11 → 12 submissions); `enabled` restored false; wallet balance
verified ([evidence](…live-readiness-session-20260923.evidence.json)).

**Submit-side slot lag quantified (2026-09-23):** ledger evidence from the
completed session gives the exact race numbers. The buyer whose event satisfied
the gate landed in slot **449587833**; our buy landed in slot **449587842** —
**9 slots (about 3.6 s) behind the displacement source**. Gate decision was
fast (410 ms); the lag accumulated in the gate's designed wait (2 slots for
buyer confirmation) and the HTTP-RPC submit path (~1.2–1.5 s from intent to
landing). On a 0.151 SOL curve, nine slots of trading moved price past the
30% bound → revert 6002. The fix is submit-path infrastructure — pre-computed
bounds with TPU-direct or Jito-bundle submission — not gate tuning
([evidence](…live-session-slot-lag-20260923.json)).

**TPU-direct submit path built and live-verified (2026-09-23):** new
`src/core/tpu.py` + client wiring. Measured first: **0 of 3,780 mainnet
nodes publish the legacy UDP `tpu` port; 3,345 publish `tpuQuic`** — so the
channel is QUIC (ALPN `solana-tpu`, CERT_NONE for self-signed validator
certs), one unidirectional stream per transaction with the 4-byte
little-endian length header. Wired into `build_and_send_transaction`: the
signed wire is pushed over QUIC (+ legacy UDP fallback) *before* the
rate-limited HTTP RPC send; never fatal; dedup by signature. Verified:
leader TPUs resolve live from `getSlotLeaders` + `getClusterNodes`
(e.g. `(64.130.45.196, 9007, quic)`); a QUIC handshake + stream + framed
packet completed in **0.835 s including the TLS handshake** — the reuse path
is faster. `aioquic 1.3.0` added. The follow-up live session ran 12m32s with
zero gate-accepted coins (all `window_expired`/`not_mayhem`), so the
channel's real-landing benefit is wired but untested by a landing; one-shot
protocol honored, `enabled` restored false, ledger unchanged (12
submissions, balance verified)
([evidence](…tpu-submit-path-20260923.evidence.json)).

**Bound freshness fix (2026-09-23):** at `buyers_present` accept,
`token_info` now takes `real_token_reserves` and a new optional
`real_sol_reserves` from the trigger buyer's TradeEvent, and
`_event_pool_state` propagates them so the exact-out fee quote prices
`max_sol_cost` from the curve **at accept time** rather than the stale
CreateEvent. Displacement math on the live revert: a single ~0.1 SOL
front-run buy trips the 30% bound on that 0.151 SOL curve — the failure was
bound staleness, not latency alone. With QUIC-TPU landing ~1 slot after the
trigger, the bound now absorbs the first wave instead of reverting.
106 lifecycle tests pass, lint clean
([evidence](…reserve-freshness-20260923.json)).

**Cycle executor phase 1 (2026-09-23, user-selected):** the six-venue quote
core is ported into `src/core/cycles/` — `core.py` (primitives, program IDs,
discriminator) and `pool.py` (Pool dataclass, decode_pool for AMM v4 + CPMM,
hydrate_pool with vault attestation and fee-rate extraction, constant-product
quote). Smoke test: quote 0.01 SOL → 19,752,964 USDC raw on a synthetic pool.
40 curve/fee tests pass, lint clean. Phase 2 (live cycle discovery) next.

**End-to-end audit (2026-09-23):** the full fresh-reserves + QUIC-TPU buy
path was audited gate by gate and verified against every repo check:
extreme_fast_zero_rpc 9/9, pumpportal_buy_path 5/5, v2_account_layout,
tx_status_checks 13/13, lifecycle 106, submission safety 99, ledger/config
safety 129. Two latent issues found and fixed: a dropped
`_signature_intents` attribute in SolanaClient (concurrent-submission guard
lost during TPU wiring) and a pre-existing `urlsplit(...).netloc`
userinfo leak in `record_creation_account_marks.py:843` (now `.hostname`).
Stage timing decomposition: CreateEvent → gate accept (410 ms observed) →
zero-RPC build (~0.1 s) → QUIC-TPU send (0.05–0.1 s reuse, 0.835 s fresh)
→ landing +1 slot — **~0.5–0.9 s end to end**, inside the same-slot window
the census pays 69,536 lamports p50 for first arrivals
([evidence](…e2e-audit-20260923.json)).

**Lending census completion (2026-09-22):** all six 600-second windows
completed cleanly (`lending-census-260922-0315-mdbxx`): 10,589
lending-program transactions (8,412 successful), **zero recognized
liquidations** — inconclusive for MarginFi because the frozen detector missed
its receivership instructions; Kamino/Velocity coverage stands. Cycle
activity is thin: 18 closed cycles, 6 positive, gain 60,725,572 lamports
concentrated in one payer (top-5 share 100%), 16,967,041 lamports of
cycle-actor failed fees, 5,947,658 tips. Lending-venue cycle density is two
orders of magnitude below the six-venue DEX market
([evidence](…lending-census-260922-0315.evidence-20260922.json)).

**Cycle executor built (2026-09-23, user-selected):** phases 2+3 completed in
parallel — `src/core/cycles/discovery.py` (391L: event-driven CycleDiscovery
over TradeFlowHub per-mint queues, CycleCandidate detection with 2-leg
constant-product quotes from TradeEvent reserves, profit gate, dedup by
legs+amount, contained ingest) and `src/core/cycles/executor.py` (481L:
CycleExecutor with require_submission gate, wallet match, budget validation,
TransactionLedger risk-session envelope, TpuSubmitter.send_quic first with
HTTP RPC fallback, CycleResult with receipt-derived signer SOL delta). Both
self-checks pass offline; 205 lifecycle/submission tests pass; lint clean.
Phase 4 (bounded live validation) is the remaining gate
([evidence](…cycle-executor-build-20260923.json)).

**Lending coverage correction (2026-09-22):** the separately frozen lending
capture (`lending-census-260922-0315.*`) samples Kamino, MarginFi and Velocity
for six 600-second windows at four-hour intervals. Its first window recorded
1,592 transactions, 1,366 successful and 226 failed, with no recognized
liquidation instructions. The captured detector used an invalid MarginFi name,
`lending_account_start_liquidation`; the actual receivership instruction is
`start_liquidation`. Missed non-cycle receipts were not retained, so their
liquidation status cannot be corrected retrospectively from this tape.

The working-tree detector now restricts discriminator matches to their protocol,
recognizes MarginFi `start_liquidation`/`end_liquidation`, and includes Velocity
`liquidate_spot_with_swap_end`. Its self-check uses explicit official wire bytes
and rejects cross-program matches. The current cloud execution is unchanged.
Sources: [Kamino entrypoints](https://github.com/Kamino-Finance/klend/blob/master/programs/klend/src/lib.rs),
[MarginFi discriminator constants](https://github.com/0dotxyz/marginfi-v2/blob/35b5c66aa6897c43e7199bd6c598134041e89f99/type-crate/src/constants.rs),
[MarginFi 6.4.2 published IDL](https://unpkg.com/@mrgnlabs/marginfi-client-v2@6.4.2/dist/idl/marginfi_0.1.8.json),
and [Velocity 0.25.0 published IDL](https://unpkg.com/@velocity-exchange/sdk@0.25.0/lib/node/idl/velocity.json).
Velocity's [public program source](https://github.com/velocity-exchange/velocity-v1/blob/9c91b022fa0948b3a2a8ddb15f4560796fbc35bf/programs/velocity/src/lib.rs)
declares the captured program ID, but this is not deployed-binary attestation.

Liquidation counters count transactions containing a recognized instruction,
not liquidation actions or profits. A failed transaction may never reach its
declared instruction; its fee payer is not necessarily the liquidator.
Successful invocation alone does not establish a nonzero transfer or profit.
The venue and lending captures share the venue-census service account and
provider secret; retain those resources until **both** executions finish.

Replaying the three batch tapes shows RPC round trips of ~20-40 ms but decisions
of ~1.4 s p50 / ~2.0 s p90 against the 2 s deadline: each decision needs up to
three serial calls behind the frozen 500 ms response-header pacing, and the
batched snapshot+simulate call itself takes ~300 ms. Time-based censors are
therefore mostly self-inflicted pacing; bank-readiness censors are the RPC bank
lagging the Geyser trigger. Details and replay source:
`chainstack-batch-26092106380.latency-diagnosis.json`. Changing the pacing is a
frozen-protocol change and was not made without approval.

**Cycle runner session (2026-09-23):** the six-venue cycle runner ran 10m
with both pools hydrated from live RPC (AMM v4 reserves measured, trade_rate
2500). Zero candidates emitted: the discovery class evaluates per-mint
queues from TradeFlowHub, which only decodes **pump.fun TradeEvents** — the
pool mints (AMM vaults) don't produce those events. **This is the remaining
wiring gap:** the six-venue trade stream needs its own Geyser subscription
on the venue pool vault accounts (the paper engine's GeyserTradeStream
pattern), feeding CycleDiscovery from that stream instead of the token
listener's hub. No submission, no fees, ledger unchanged
([evidence](…cycle-session-20260923.evidence.json)).

The cycle executor wiring fix is straightforward: create a
`GeyserCycleStream` that subscribes to the venue pool **vault accounts**
(the `dependencies()` return values), decode swap instructions from their
program logs (AMM v4 and CPMM swapBaseIn/swapBaseOut discriminators), and
feed `CycleDiscovery.ingest()` from that stream. The existing
`GeyserTradeStream` pattern (subscribe bonding curve → decode TradeEvent →
yield) maps directly. The paper engine already has this: `GeyserCapture` in
the census tools subscribes to program-wide streams and decodes all six
venue swap forms.

**Fresh-reserves session (2026-09-23):** 11m0s, 600 s token budget expired
with all gate evaluations returning `not_mayhem` (no qualifying coin); zero
submissions, zero fees, ledger unchanged. **Important disclosure:** the cycle
executor (`src/core/cycles/discovery.py` + `executor.py`) is built and
offline-verified but **not yet wired into `bot_runner.py`** — the
live-readiness config runs pump_fun sniping. Wiring it in needs its own
runner integration. This session exercised the pump_fun path with fresh
reserves + QUIC-TPU (the two fixes from this session)
([evidence](…fresh-reserves-session-20260923.json)).

**Same-mint pool discovery fix (2026-09-23):** tracing the discovery math
exposed a false positive: the runner registered census top pools (trading
different tokens) against every new coin, so the cycle-first direction
showed a false 4.34 SOL margin (comparing prices across different assets).
Fixed: `discover_pools_for_mint()` in `pool.py` uses the Raydium API list
endpoint (the mint-specific endpoint returns 500) filtered client-side for
AMM v4/CPMM pools trading the target mint/SOL. The runner's
`on_new_token()` now calls `discover_pools_for_mint()` per new coin and
registers same-mint pools via `update_pool()`. Verified: Fartcoin
`9BB6NFEc…` resolves to AMM v4 pool `Bzc9NZfMqkXR6fz1`. A valid cycle
requires the pool to trade the same mint as the curve — which happens when
a pump.fun coin graduates to AMM. Runner test: 8 tracked mints in 10 s,
0 same-mint pools (most fresh coins haven't graduated yet)
([evidence](…same-mint-discovery-20260923.json)).

**Cycle runner live session with listener integration (2026-09-23):** the
proper runner — `src/cycles/runner.py` with UniversalGeyserListener +
TradeFlowHub + CycleDiscovery + CycleExecutor — tracked **551 new pump.fun
coins** in 10 minutes, with 2 AMM v4 pools hydrated from live RPC. Zero
cycle candidates: the profit gate correctly rejected all (AMM v4 2500 bps
trade rate + profit floor on a 0.01 SOL buy against a fresh curve). The
mechanism is proven end to end (listener → discovery → candidate →
executor); the zero outcome is parameter tuning, not wiring failure.
One-shot honored, `enabled` restored false, ledger unchanged
([evidence](…cycle-runner-session-20260923.evidence.json)).

**False positive cycle discovery found (2026-09-23):** tracing the discovery
math exposed a critical design flaw: the runner registers census top pools
(e.g. AMM v4 `FCEnSx…` trading SOL/B5WTLaRw) against every new coin, but
those pools trade **different tokens**. The cycle-first direction shows a
4.34 SOL "margin" purely because the pool prices a different asset. A valid
cycle requires the pool to trade the **same mint** as the curve — which is
what the census captured (coins that graduated from curve to AMM). The fix
requires discovering the same-mint pool dynamically per coin (via
`getProgramAccounts` or Raydium API), not a fixed pool list
([finding](…cycle-false-positive-20260923.json)).
