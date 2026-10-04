# Pump.fun Live Dynamic Fees Implementation Plan

> **Status: EXECUTED (2026-10-01).** Do not re-implement task-by-task. The plan's
> checkboxes were never ticked, but the work shipped: `src/platforms/pumpfun/fee_schedule.py`,
> `tests/test_pumpfun_fee_schedule.py`, `tests/test_pumpfun_curve_fees.py` and
> `learning-examples/verify_pump_fee_schedule.py` all exist and CI runs the
> verifier suite (`.github/workflows/ci.yml`). The boxes are historical
> tracking, not a work queue; see the agent guide for the current verifier
> commands.

**Goal:** Enable fail-closed live Pump.fun bonding-curve quotes and trades for WSOL and USDC using an attested dynamic fee schedule.

**Architecture:** A Pump-specific strict decoder and integer quote engine consumes immutable `FeeConfig` snapshots. Normal trades batch curve, mint, and fee state; verified CreateEvents use a periodically attested cache so fee lookup stays off the extreme-fast dependency path. Live startup and every changed fee config are checked against read-only `get_fees` simulation before use.

**Tech Stack:** Python 3.11+, asyncio, solders, solana-py, pytest, Ruff, Anchor/Borsh account layouts.

**Spec:** `docs/superpowers/specs/2026-08-31-pump-live-fees-design.md`

## Global Constraints

- Support exactly WSOL and USDC quote mints registered in `src/core/pubkeys.py`.
- Never read or print `.env` contents and never submit a funded transaction during verification.
- Use raw integer arithmetic only for executable quotes; floats remain presentation-only.
- Never fall back to hardcoded fees or legacy Pump Global fees.
- Preserve the verified CreateEvent zero-request dependency path.
- Keep imports rooted at `src/`.
- Run Ruff only on touched paths.

---

### Task 1: Strict fee account model and quote math

**Files:**
- Create: `src/platforms/pumpfun/fee_schedule.py`
- Create: `tests/test_pumpfun_fee_schedule.py`

**Interfaces:**
- Produces: `PumpFees`, `PumpFeeTier`, `PumpFeeConfig`, `PumpFeeSnapshot`, `PumpQuote`.
- Produces: `decode_fee_config_account(account: Account) -> PumpFeeConfig`.
- Produces: `quote_buy_exact_in(state: Mapping[str, object], spendable_raw: int, snapshot: PumpFeeSnapshot) -> PumpQuote`.
- Produces: `quote_buy_exact_out(state: Mapping[str, object], token_amount_raw: int, snapshot: PumpFeeSnapshot) -> PumpQuote`.
- Produces: `quote_sell_exact_in(state: Mapping[str, object], token_amount_raw: int, snapshot: PumpFeeSnapshot) -> PumpQuote`.

- [ ] **Step 1: Write decoder contract tests**

Create real account bytes with the current layout and assert owner/discriminator checks, zero-padding acceptance, nonzero unknown-tail rejection, vector truncation rejection, empty-tier rejection, strictly increasing thresholds, and BPS limits. The basic fixture shape is:

```python
raw = bytearray(FEE_CONFIG_DISCRIMINATOR)
raw += bytes([253])
raw += bytes(Pubkey.new_unique())
raw += struct.pack("<QQQ", 0, 95, 30)
raw += encode_tiers(regular)
raw += encode_tiers(stable)
raw += bytes(128)
account = Account(1, bytes(raw), PumpFunAddresses.FEE_PROGRAM, False, 0)
```

- [ ] **Step 2: Run decoder tests and confirm RED**

Run: `uv run pytest -q tests/test_pumpfun_fee_schedule.py -k 'decode or reject'`
Expected: collection fails because `platforms.pumpfun.fee_schedule` does not exist.

- [ ] **Step 3: Implement immutable models and strict decoder**

Use frozen slotted dataclasses and `struct.unpack_from`; check bounds before every vector. Hash the complete account data with SHA-256. Do not allocate based on an untrusted count until `count * 40 <= remaining_bytes` is proven.

- [ ] **Step 4: Run decoder tests and confirm GREEN**

Run: `uv run pytest -q tests/test_pumpfun_fee_schedule.py -k 'decode or reject'`
Expected: all selected tests pass.

- [ ] **Step 5: Write quote-math tests**

Cover regular versus stable tier selection, the exact threshold boundary, default creator suppression, separate fee rounding, the IDL buy correction when separately ceiled fees overshoot, real-token cap, impossible exact-output target, sell fee subtraction, and nonzero LP rejection. Each assertion uses hand-calculated raw integers.

- [ ] **Step 6: Run quote tests and confirm RED**

Run: `uv run pytest -q tests/test_pumpfun_fee_schedule.py -k 'quote or tier or creator'`
Expected: failures identify missing quote functions.

- [ ] **Step 7: Implement fee selection and integer quote functions**

Implement highest-threshold selection and `_ceil_div`. Require positive u64 trade inputs and validated curve fields. Return fee breakdown and config digest in every `PumpQuote`.

- [ ] **Step 8: Run the complete fee module tests**

Run: `uv run pytest -q tests/test_pumpfun_fee_schedule.py`
Expected: all tests pass.

### Task 2: Attested snapshot lifecycle

**Files:**
- Modify: `src/platforms/pumpfun/fee_schedule.py`
- Modify: `tests/test_pumpfun_fee_schedule.py`

**Interfaces:**
- Produces: `PumpFeeSchedule(client: SolanaClient, fee_config: Pubkey, config_program: Pubkey)`.
- Produces: `async start() -> None`, `async close() -> None`, `async accept_account(account: Account) -> PumpFeeSnapshot`, and `require_snapshot() -> PumpFeeSnapshot`.
- Internal contract: `get_fees` simulations return exactly three little-endian u64 values.

- [ ] **Step 1: Write lifecycle and attestation tests**

Use a deterministic fake client that returns real solders accounts and JSON-RPC simulation responses. Assert startup requires attestation, same-digest observations refresh age without re-attesting, changed bytes cannot promote before matching program results, mismatches invalidate the candidate, stale observations fail, expired attestations fail, and `close` cancels the poll task.

- [ ] **Step 2: Run lifecycle tests and confirm RED**

Run: `uv run pytest -q tests/test_pumpfun_fee_schedule.py -k 'snapshot or attest or refresh or close'`
Expected: failures identify the missing `PumpFeeSchedule` API.

- [ ] **Step 3: Implement simulation encoding and parsing**

Encode:

```python
data = (
    GET_FEES_DISCRIMINATOR
    + struct.pack("<?", True)
    + market_cap_raw.to_bytes(16, "little")
    + struct.pack("<Q", trade_size_raw)
    + struct.pack("<?", is_usdc)
)
```

Build a simulation-only instruction with read-only fee-config and Pump-program accounts. Parse only `Program return: pfee... <base64>` for the expected program and require 24 decoded bytes. Probe every regular/stable tier plus two trade sizes; differing trade-size results fail closed.

- [ ] **Step 4: Implement refresh, freshness, and locking**

Use one async lock for validation/promotion. Poll every 2 seconds, require observation age at most 10 seconds, re-attest after 60 seconds, and reject attestation age over 120 seconds. A transport failure preserves the last snapshot only inside those bounds.

- [ ] **Step 5: Run lifecycle tests and fee tests**

Run: `uv run pytest -q tests/test_pumpfun_fee_schedule.py`
Expected: all tests pass.

### Task 3: Slot-consistent Pump curve integration

**Files:**
- Modify: `src/interfaces/core.py`
- Modify: `src/platforms/pumpfun/curve_manager.py`
- Modify: `src/platforms/letsbonk/curve_manager.py`
- Modify: `src/trading/platform_aware.py`
- Create or modify: `tests/test_pumpfun_curve_fees.py`
- Modify: `tests/test_letsbonk_execution_safety.py`

**Interfaces:**
- Changes: both `CurveManager.calculate_*_amount_out` methods accept `pool_state: dict[str, Any] | None = None` as a keyword-only argument.
- Produces: Pump `prepare_live_execution()`, `close()`, and `calculate_buy_cost(...)`.
- Pool state carries an internal `_pump_fee_snapshot` value that never enters persistence.

- [ ] **Step 1: Write state-reuse and fee-aware curve tests**

Assert one provided state causes zero account reads, standalone Pump quote fetches curve plus fee config together, curve+mint refresh fetches exactly three ordered accounts, a changed config is accepted only after attestation, buy output is fee-adjusted, and sell output is fee-adjusted.

- [ ] **Step 2: Run integration tests and confirm RED**

Run: `uv run pytest -q tests/test_pumpfun_curve_fees.py tests/test_letsbonk_execution_safety.py`
Expected: missing optional-state API and pre-fee output assertions fail.

- [ ] **Step 3: Update the shared interface and both implementations**

LetsBonk uses the supplied state or performs its existing fetch. Pump validates curve, mint owner, and fee account from one batch, attaches the snapshot, and delegates arithmetic to `fee_schedule.py`. Remove pre-fee wording and avoid any cached mutable pool state.

- [ ] **Step 4: Pass refreshed state from buyer and seller**

Change normal calls to:

```python
await curve_manager.calculate_buy_amount_out(
    pool_address,
    quote_amount_raw,
    pool_state=pool_state,
)
```

and the equivalent sell call. Keep slippage application after fee-adjusted output.

- [ ] **Step 5: Run curve integration tests**

Run: `uv run pytest -q tests/test_pumpfun_curve_fees.py tests/test_letsbonk_execution_safety.py`
Expected: all selected tests pass.

### Task 4: Fee-aware verified-event fast path

**Files:**
- Modify: `src/interfaces/core.py`
- Modify: `src/platforms/pumpfun/event_parser.py`
- Modify: `src/trading/platform_aware.py`
- Modify: `src/trading/universal_trader.py`
- Modify: `tests/test_pumpfun_event_parser_safety.py`
- Modify: `tests/test_trading_lifecycle_hardening.py`
- Modify: `learning-examples/verify_extreme_fast_zero_rpc.py`

**Interfaces:**
- Adds `TokenInfo.virtual_token_reserves`, `real_token_reserves`, and `token_total_supply`.
- Verified Pump CreateEvents must supply all event quote inputs before `_can_skip_refresh` returns true.
- Pump exact-output fast quotes use `calculate_buy_cost` and the attested snapshot.

- [ ] **Step 1: Write event and recovery round-trip tests**

Assert correlated successful events retain all reserves, logs-only/instruction-only paths clear event-only reserves, malformed reserve fields prevent trusted state, and recovery JSON restores exact integers without float conversion.

- [ ] **Step 2: Run event tests and confirm RED**

Run: `uv run pytest -q tests/test_pumpfun_event_parser_safety.py tests/test_trading_lifecycle_hardening.py -k 'reserve or recovery or event'`
Expected: new field assertions fail.

- [ ] **Step 3: Add and persist authoritative event fields**

Populate fields directly from decoded CreateEvent values. Include them in `_serialize_token_info` and strict integer restoration. Clear them in every provenance downgrade path.

- [ ] **Step 4: Write fast-path fee and cap tests**

Assert a verified event performs no trade-triggered account read, uses the stable schedule for USDC, skips when the fixed token target exceeds the configured quote cap, and refuses a stale snapshot rather than refreshing synchronously.

- [ ] **Step 5: Run fast-path tests and confirm RED**

Run: `uv run pytest -q tests/test_trading_lifecycle_hardening.py && uv run learning-examples/verify_extreme_fast_zero_rpc.py`
Expected: fee-aware expectations fail before integration.

- [ ] **Step 6: Implement exact-output fast quoting**

For Pump v2, preserve the configured fixed token target, calculate its fee-aware required quote from event state, compare it to the existing max quote cap, and build only when it fits. Do not add a fee RPC inside `PlatformAwareBuyer.execute`.

- [ ] **Step 7: Run event and zero-RPC checks**

Run: `uv run pytest -q tests/test_pumpfun_event_parser_safety.py tests/test_trading_lifecycle_hardening.py && uv run learning-examples/verify_extreme_fast_zero_rpc.py`
Expected: tests and verifier pass.

### Task 5: Live lifecycle, capability gate, and operator docs

**Files:**
- Modify: `src/trading/universal_trader.py`
- Modify: `src/config_loader.py`
- Modify: `tests/test_config_validation.py`
- Modify: `tests/test_trading_lifecycle_hardening.py`
- Modify: `README.md`

**Interfaces:**
- Live Pump startup calls `curve_manager.prepare_live_execution()` before queue/listener tasks.
- Cleanup calls `curve_manager.close()` before `SolanaClient.close()`.
- Pump live configuration is accepted only after runtime attestation remains mandatory.

- [ ] **Step 1: Write startup, cleanup, and config tests**

Assert fee preparation precedes token processing, preparation failure starts no listener or processor, cleanup cancels the schedule before RPC close, live Pump config now validates, and the runtime quote guard no longer rejects an attested Pump manager.

- [ ] **Step 2: Run lifecycle tests and confirm RED**

Run: `uv run pytest -q tests/test_config_validation.py tests/test_trading_lifecycle_hardening.py -k 'pump or fee or close or startup'`
Expected: the old configuration/runtime gates or missing lifecycle calls fail.

- [ ] **Step 3: Wire lifecycle and remove obsolete hard gates**

Call the uniform curve-manager lifecycle methods. Delete `_require_executable_quote_contract` only after both normal and fast Pump quotes require fee snapshots. Do not add a bypass flag.

- [ ] **Step 4: Update README capability boundary**

Replace the statement that Pump live is unavailable with the actual guarantees: WSOL/USDC only, startup attestation required, normal batch versus event-cache flow, and failure behavior. Keep LetsBonk limitations unchanged.

- [ ] **Step 5: Run targeted lifecycle/config tests**

Run: `uv run pytest -q tests/test_config_validation.py tests/test_trading_lifecycle_hardening.py`
Expected: all selected tests pass.

### Task 6: Permanent offline verifier and release verification

**Files:**
- Create: `learning-examples/verify_pump_fee_schedule.py`
- Modify: `learning-examples/simulate_v2_trades.py`
- Modify: `AGENTS.md` only if the worktree contains the tracked regular file and the new commands need operator guidance; never overwrite the user's base-checkout type change.

**Interfaces:**
- Offline verifier checks FeeConfig layout, tier selection, quote vectors, and `get_fees` instruction encoding without network access.
- Mainnet simulator uses the bot's fee-aware quote engine and still only calls `simulateTransaction`.

- [ ] **Step 1: Write the offline verifier and run it RED**

Run: `uv run learning-examples/verify_pump_fee_schedule.py`
Expected: it exits nonzero until all required vectors and encodings are represented.

- [ ] **Step 2: Complete verifier vectors and simulation wiring**

Include WSOL and USDC fixture schedules, threshold-minus-one/threshold/threshold-plus-one cases, default/nondefault creator cases, and exact raw expected values. Update `simulate_v2_trades.py` to derive buy and sell limits from the same fee-aware state used by production.

- [ ] **Step 3: Run scoped formatting and lint**

Run: `uv run ruff check --fix src/interfaces/core.py src/platforms/pumpfun/fee_schedule.py src/platforms/pumpfun/curve_manager.py src/platforms/pumpfun/event_parser.py src/platforms/letsbonk/curve_manager.py src/trading/platform_aware.py src/trading/universal_trader.py src/config_loader.py tests/test_pumpfun_fee_schedule.py tests/test_pumpfun_curve_fees.py tests/test_pumpfun_event_parser_safety.py tests/test_trading_lifecycle_hardening.py tests/test_config_validation.py tests/test_letsbonk_execution_safety.py learning-examples/verify_pump_fee_schedule.py learning-examples/verify_extreme_fast_zero_rpc.py learning-examples/simulate_v2_trades.py` and then run `uv run ruff format` with the same explicit path list.
Then run `uv run ruff check --select E,F,I,S` with that same explicit path list.
Expected: no new diagnostics.

- [ ] **Step 4: Run the full local verification matrix**

Run:

```bash
uv run pytest -q
uv run learning-examples/verify_pump_fee_schedule.py
uv run learning-examples/verify_v2_account_layout.py
uv run learning-examples/verify_pumpportal_buy_path.py
uv run learning-examples/verify_extreme_fast_zero_rpc.py
uv run learning-examples/verify_tx_status_checks.py
uv run learning-examples/verify_tp_sl_exit_price.py
uv lock --check
uv run python -m compileall -q src tests learning-examples
```

Expected: every command exits zero.

- [ ] **Step 5: Run read-only mainnet simulations**

Run `uv run learning-examples/verify_pump_fee_schedule.py --live`. Its live mode discovers one active WSOL curve and one active USDC curve, attests `get_fees`, and invokes the same simulation helpers used by `simulate_v2_trades.py` for each. Verify both reach Pump without fee/slippage math errors. Never send a transaction.

- [ ] **Step 6: Review and promote**

Invoke the mandatory Python reviewer and a silent-failure review. Address findings test-first, rerun the complete matrix, commit the feature branch, merge it into canonical `main`, push, and require hosted CI success before deleting the worktree/branch.
