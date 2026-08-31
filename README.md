<img width="1200" alt="Labs" src="https://user-images.githubusercontent.com/99700157/213291931-5a822628-5b8a-4768-980d-65f324985d32.png">

<p>
 <h3 align="center">Chainstack is the leading suite of services connecting developers with Web3 infrastructure</h3>
</p>

<p align="center">
  • <a target="_blank" href="https://chainstack.com/">Homepage</a> •
  <a target="_blank" href="https://chainstack.com/protocols/">Supported protocols</a> •
  <a target="_blank" href="https://chainstack.com/blog/">Chainstack blog</a> •
  <a target="_blank" href="https://docs.chainstack.com/quickstart/">Blockchain API reference</a> • <br> 
  • <a target="_blank" href="https://console.chainstack.com/user/account/create">Start for free</a> •
</p>

A Solana trading bot for **pump.fun** and **letsbonk.fun**. Its core feature is sniping new tokens: it watches for token creation, buys, and exits on a strategy you configure. `learning-examples/` includes offline verifiers, read-only listeners, RPC simulations, and live transaction scripts; do not treat that directory as uniformly safe to run.

For the full walkthrough, see [Solana: Creating a trading and sniping pump.fun bot](https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot). It explains the concepts well but lags behind the code, so treat this README and the checked-in sample configs as the source of truth for setup and safety policy.

> **Also by Chainstack** — if you prefer a terminal interface or want to give an AI agent trading capabilities:
> - [**pumpfun-cli**](https://github.com/chainstacklabs/pumpfun-cli) — CLI for trading, launching, and managing tokens on pump.fun; buy, sell, wallet management, and smart routing between the bonding curve and PumpSwap AMM.
> - [**pumpclaw**](https://github.com/chainstacklabs/pumpclaw) — agent skill that equips AI assistants (OpenClaw, Claude Code, Cursor, Codex) with the ability to operate pumpfun-cli.

---

**🚨 SCAM ALERT**: The Issues section is regularly targeted by scam bots that try to redirect you to an external site and drain your funds. A GitHub Action tags the common patterns, which is not 100% accurate. Deleted comments in issues are scam bots after your private keys — genuine outside devs are welcome and appreciated.

**⚠️ NOT FOR PRODUCTION**: This code is for learning purposes only. We assume no responsibility for the code or its usage. Modify it for your needs and learn from it — the examples, issues, and PRs contain valuable insights.

**Execution is fail-closed by default.** The sample bots are disabled and set
`execution.mode: "dry_run"`. Dry-run never authorizes transaction submission,
but it can still contact configured RPC/listener services and does not prove
that a later live transaction will succeed.
Live submission requires one explicitly selected config, `execution.mode:
"live"`, an exact `expected_wallet`, a unique `risk_session_id`, per-wire trade
and fee caps, cumulative session caps, and the separate `--authorize-live`
runtime acknowledgement. Bulk startup never authorizes live bots.

**Current live capability boundary:** pump.fun v2 buys start on the bonding
curve; sells route through the bonding curve while it is active and through the
canonical index-0 PumpSwap pool after migration. Both paths support only
SOL/WSOL- and USDC-paired coins. Before any live listener or queue processor
starts, the bot strictly decodes and attests the Pump FeeConfig account and the
PumpSwap fee state used by migrated exits. Normal quotes batch the curve, mint
where needed, and FeeConfig reads; verified CreateEvent fast-path quotes use
event reserves plus the continuously refreshed, attested fee snapshot. Unknown
quote assets, malformed or changed fee data, failed attestation, stale
observations, and expired attestation all fail closed before submission. There
is no hardcoded fee fallback. Dry-run initializes the same attested schedule so
it exercises the executable quote path while submission remains disabled.
LetsBonk execution remains limited to authoritative `blocks` or `geyser`
events, funding-state constant product pools, and WSOL.

---

## Getting started

### 1. Prerequisites

Install [uv](https://github.com/astral-sh/uv), a fast Python package manager. The project needs **Python 3.11+**; `uv` uses an existing install if it's new enough, otherwise it fetches one for you.

### 2. Clone and install

```bash
git clone https://github.com/chainstacklabs/pumpfun-bonkfun-bot.git
cd pumpfun-bonkfun-bot

uv sync                        # create .venv and install dependencies
source .venv/bin/activate      # Unix/macOS — Windows: .venv\Scripts\activate
uv pip install -e .            # install the bot as an editable package
```

### 3. Set your credentials

```bash
cp .env.example .env
```

Fill in `.env`. Use a dedicated, low-value wallet; never paste a seed phrase or
funded primary-wallet key. The key is still parsed to identify the wallet in
dry-run mode, but dry-run policy blocks transaction submission.

| Variable | Purpose |
|---|---|
| `SOLANA_NODE_RPC_ENDPOINT` | HTTPS RPC endpoint |
| `SOLANA_NODE_WSS_ENDPOINT` | WebSocket endpoint (for `logs` / `blocks` listeners) |
| `SOLANA_PRIVATE_KEY` | Base58 private key for a dedicated trading wallet; leave the template blank |
| `GEYSER_ENDPOINT`, `GEYSER_API_TOKEN`, `GEYSER_AUTH_TYPE` | Only for the `geyser` listener |

Public RPC nodes will not work for this workload — see [throughput](#throughput-and-rate-limits) below.

### 4. Configure a bot

Each YAML file in `bots/` is one bot instance. Every checked-in sample has
`enabled: false`, `execution.mode: "dry_run"`, destructive cleanup disabled,
and live-only policy grants disabled. Start from the listener you want, but keep
those settings until you have reviewed the resolved config and wallet identity:

| File | Listener | Ships with |
|---|---|---|
| `bot-sniper-1-geyser.yaml` | `geyser` — fastest, needs a Geyser endpoint | `pump_fun` |
| `bot-sniper-2-logs.yaml` | `logs` — `logsSubscribe`, supported everywhere | `pump_fun` |
| `bot-sniper-3-blocks.yaml` | `blocks` — `blockSubscribe`, not supported by every provider | `pump_fun` |
| `bot-sniper-4-pp.yaml` | `pumpportal` — third-party aggregator | `pump_fun` |

Set `platform: "pump_fun"` or `platform: "lets_bonk"`. pump.fun supports all
four listeners; letsbonk.fun supports `blocks` and `geyser`. PumpPortal's
LetsBonk payload lacks the authoritative pool/config/vault state required by
the executable path, so that pairing is rejected at startup rather than filled
with guessed accounts. The bot validates every pairing before startup.

`enabled: false` prevents startup. To exercise one sample without authorizing
fund movement, keep `execution.mode: "dry_run"`, set only that sample to
`enabled: true`, and select it explicitly:

```bash
uv run src/bot_runner.py --config bots/bot-sniper-2-logs.yaml
```

This command cannot authorize live submission: a config changed to `live`
fails unless the separate live acknowledgement flag is also present. Running
without `--config` scans all samples but still refuses live configs.

Logs land in `logs/{bot_name}_{timestamp}.log`.

## Configuration reference

The YAML files are commented inline. The sections that matter most:

- **`execution`** — defaults to `dry_run`. Live mode requires an exact
  `expected_wallet`; a unique `risk_session_id`; per-wire
  `max_trade_quote_raw` and `max_total_fee_lamports`; and durable cumulative
  `max_session_quote_raw` and `max_session_fee_lamports`. The quote session cap
  is applied independently to each quote mint because SOL lamports and USDC raw
  units are not comparable. Every signed wire reserves its worst-case quote
  debit and fee before network submission; the reservation survives restarts.
  Leave `allow_skip_preflight: false` unless transaction simulation has been
  deliberately waived after a separate risk review. `allow_force_burn` should
  remain false unless destructive cleanup has been separately reviewed.
- **`trade`** — `buy_amount` is the SOL amount per buy;
  `trade.quote_amounts` supplies whole-unit amounts for non-SOL quote mints.
  Slippage, `exit_strategy` (`time_based`, `tp_sl`, `manual`), and
  `extreme_fast_mode` control execution. Extreme-fast mode skips the
  bonding-curve price read and buys a fixed token amount instead. See
  [Extreme fast mode](#extreme-fast-mode-zero-rpc-buys) for its two provenance
  controls, `trust_create_event` and `curve_refresh_budget`.
- **`priority_fees`** — fixed or dynamic. Dynamic costs an extra RPC call, which slows the buy.
- **`filters`** — `listener_type`, `max_token_age`, name/creator matching, `marry_mode` (buy only, never sell), `yolo_mode` (trade continuously).
- **`retries`** — attempts and the wait windows around creation, buy, and the next token.
- **`cleanup`** — defaults to `disabled`. `on_fail`, `after_sell`, and `post_session` may submit account-management transactions in authorized live mode. `force_close_with_burn` irreversibly destroys remaining tokens and is blocked unless both the cleanup request and `execution.allow_force_burn` are true.
- **`node.max_rps`** — cap requests per second to match your provider's plan.

Positions are journaled per venue under
`.state/positions/<wallet>-<platform>.json`. Live transaction attempts and
cumulative risk reservations share one wallet-wide ledger at
`.state/transaction-ledgers/<wallet>.sqlite3`, so concurrent platform processes
cannot each consume the full session cap. These paths are relative to the
working directory. Back them up and do not delete, edit, or share them while a
position or transaction outcome is unresolved: they prevent unsafe
re-execution, enforce restart-stable budgets, and drive recovery.

Versions before the wallet-wide risk cap wrote
`.state/transaction-ledgers/<wallet>-pump_fun.sqlite3` and
`<wallet>-lets_bonk.sqlite3`. The new runtime, `--status`, preflight, and cleanup
tool fail closed while either legacy ledger exists; silently starting a new
ledger could replay an unresolved wire or reset cumulative risk. Before
upgrading, stop every bot, resolve all nonterminal submissions with the previous
build, close every process using the SQLite WAL, and archive the verified
terminal legacy ledgers. Never rename, copy, merge, or delete a live WAL merely
to bypass this guard.

## Live operations runbook

Use a dedicated low-value wallet. Never use a funded primary wallet and never
test a code change by starting a live bot.

1. Keep `enabled: false`. Set `execution.mode: "live"`, the exact public key in
   `expected_wallet`, a new descriptive `risk_session_id`, and all four per-wire
   and cumulative caps. Caps are integers in raw units. Ensure
   `max_trade_quote_raw` covers the configured amount plus buy slippage and
   `max_total_fee_lamports` covers the base fee plus the configured priority fee
   at the operation's compute-unit limit.
2. Inspect durable state without authorizing submission:

   ```bash
   CONFIG=bots/bot-sniper-1-geyser.yaml
   uv run src/bot_runner.py --config "$CONFIG" --status
   ```

   Resolve or explicitly account for every active position, pending exit, and
   unresolved buy. Do not delete either `.state` file to make the report clear.
3. Run the networked, non-submitting readiness check:

   ```bash
   uv run src/bot_runner.py --config "$CONFIG" --preflight
   ```

   Continue only when the JSON says `"ready": true`. This verifies the signer,
   exclusive state lock, RPC health, configured quote balances, native
   SOL/fee/rent headroom, remaining durable session budgets, and live fee
   attestation. It never authorizes or calls transaction submission.
4. Set `enabled: true`, rerun `--preflight`, then start exactly that config:

   ```bash
   uv run src/bot_runner.py --config "$CONFIG" --authorize-live
   ```

   `--authorize-live` is process-local; it is not stored in YAML. Starting
   without `--config` still refuses every live bot.
5. During and after the run, use `--status`. Stop the listener before manual
   intervention. To liquidate one journaled mint without starting a listener:

   ```bash
   MINT=<base58-mint>
   uv run src/bot_runner.py --config "$CONFIG" \
     --emergency-exit "$MINT" --authorize-live
   ```

   Exit code `0` means confirmed, `1` means failed before confirmation, and `2`
   means the exact signature is still unresolved. On `2`, do not submit a
   different sell; rerun the same command to reconcile the durable signature.

`risk_session_id` is the explicit reset boundary. Do not rotate it merely to
continue buying after a cap. Size fee headroom for buys, exits, and cleanup. If
the fee cap itself blocks liquidation, stop all listeners and create a
dedicated exit-only session before using `--emergency-exit`; never combine that
reset with new buys. A prepared wire that provably never reached the network
releases its reservation. Submitted, reverted, expired, and ambiguous wires
remain conservatively counted.

### Extreme fast mode: zero-RPC buys

With `extreme_fast_mode: true` the bot constructs a buy for a fixed token amount
(`extreme_fast_token_amount`) instead of fetching the curve price first. It does
not override execution policy: checked-in dry-run configs still cannot submit.

For pump.fun, zero-RPC preparation is limited to authoritative, correlated
**CreateEvent** observations. Normalized `geyser` and `blocks` transactions
retain `state_from_event` and `metadata_verified` only when the event matches
exactly one create instruction in the same successful transaction. Those
retained fields include the canonical creator, mayhem/cashback flags, quote
mint, and raw curve reserves. The fee-aware quote combines them with the
already-attested in-memory FeeConfig snapshot, so no trade-triggered RPC is
needed between detection and submission. A background poll keeps that snapshot
fresh; a stale or expired snapshot blocks the buy. This is an
offline-verifiable latency contract, not a fee or execution-policy bypass.

`logsSubscribe` is deliberately **not** a zero-RPC source. Its notification has
logs but no transaction instructions with which to correlate the CreateEvent,
so normalized parser dispatch clears `state_from_event`, `metadata_verified`,
and event-only quote state. A token detected by the `logs` listener must refresh
curve state before a buy even when its logs contain a valid CreateEvent.

The `pumpportal` listener also cannot take the zero-RPC path because its payload
carries none of the authoritative curve state. It performs one slot-consistent
batched read of the bonding curve, mint, and FeeConfig before buying. If the
required state is not readable within `trade.curve_refresh_budget` seconds
(default 2.0), the token is **skipped**: a buy built from guessed accounts
reverts on-chain with `NotAuthorized` (6000) or `ConstraintSeeds` (2006) and
still costs the fee. The same refresh-and-skip rule applies to incomplete,
uncorrelated, or downgraded event data.

`trade.trust_create_event: false` turns the zero-RPC path off and forces the
pre-buy read even for verified `geyser` and `blocks` CreateEvents. This is the
more conservative fallback if pump.fun changes what the CreateEvent carries; it
still cannot guarantee a live transaction will succeed.

Machine checks: `learning-examples/verify_pump_fee_schedule.py` (strict layout,
integer quote vectors, every-tier `get_fees` attestation, plus optional
read-only WSOL/USDC mainnet simulations with `--live`; that mode requires an
exported `SOLANA_NODE_RPC_ENDPOINT` and reads neither `.env` nor a private key),
`learning-examples/verify_extreme_fast_zero_rpc.py` (the zero-RPC contract per
listener), and `learning-examples/verify_pumpportal_buy_path.py` (the
refresh/skip path). None moves funds.

### Non-SOL quote assets

pump.fun v2 has verified metadata only for SOL/WSOL and USDC. Amounts are in
that mint's own whole units, so `usdc: 1.0` is one USDC and is **not**
comparable to `buy_amount`:

```yaml
trade:
  buy_amount: 0.0001    # SOL-paired coins
  quote_amounts:
    usdc: 1.0           # USDC-paired coins

filters:
  allowed_quote_mints: ["sol", "usdc"]
```

Use only the aliases `sol` / `wsol` / `usdc`. Although config parsing accepts a
raw base58 mint, execution rejects quotes without verified decimals and token
program metadata; a configured amount does not make an arbitrary quote asset
supported. A coin with no configured amount is skipped. USDC buys require USDC
plus SOL for fees and ATA rent. letsbonk.fun's current executable buy/sell path
supports WSOL quote only and refuses other quote mints.

## Learning examples

The examples are not one safety class. Offline `verify_*.py` scripts do not use
RPC or keys. Listener/decoder scripts may contact mainnet. `simulate_*.py`
scripts submit RPC simulations but no transactions; simulation can differ from
live execution. Files named `manual_buy*`, `manual_sell*`, `mint_and_buy*`, and
`cleanup_accounts.py` are live/account-mutating tools that can spend funds or
change account state. Never run them as a verification step.

| Path | What it covers |
|---|---|
| `listen-new-tokens/` | One listener per method (`logs`, `blocks`, `geyser`, `pumpportal`) plus `compare_listeners.py` to race them |
| `listen-migrations/` | Detect a token graduating from the bonding curve to PumpSwap, via the migration wrapper program or new pool accounts |
| `bonding-curve-progress/` | Curve state, progress polling, and a live watch for coins close to graduating — over WebSocket (`get_graduating_tokens.py`) or Geyser (`get_graduating_tokens_geyser.py`), both taking `--min-progress` |
| `pumpswap/` | Manual **live** buy/sell against the PumpSwap AMM, and pool discovery |
| `letsbonk-buy-sell/` | Manual **live** exact-in / exact-out buys and sells on letsbonk.fun |
| `copy-trading/` | Watch another wallet's transactions |
| `manual_buy.py`, `manual_sell.py`, `fetch_price.py` | Live pump.fun trade tools plus a read-only price path. `manual_buy.py --cu-optimized` adds a `SetLoadedAccountsDataSizeLimit` instruction |
| `mint_and_buy_v2.py` | **Live:** create a coin and buy it in one transaction |
| `decode_from_*.py`, `calculate_discriminator.py` | Decoding account data, transactions, and Anchor discriminators |
| `cleanup_accounts.py` | **Live/account-mutating:** close an empty ATA or unwrap WSOL through the authorized transaction ledger; it refuses token burns |

Stop every process using the wallet before running it; closing an ATA while a
bot is building a trade can invalidate either transaction. Each cleanup target
uses a durable generation, and success is reported only after the confirmed
transaction leaves the account absent.

The cleanup tool has no default mint and requires the same explicit live grant
as the bot:

```bash
uv run learning-examples/cleanup_accounts.py \
  --config bots/bot-sniper-1-geyser.yaml \
  --mint <MINT> \
  --authorize-live
```

Most other examples take the mint or curve address as the first argument, and
print usage if you leave it off. The `decode_from_*.py` scripts fall back to the
saved fixtures beside them (`raw_*.json`), which are recaptured from mainnet
rather than hand-edited—a stale fixture makes a working decoder look broken and
a broken one look fine.

Two offline verifiers and one RPC simulation are useful after a pump.fun
program upgrade:

```bash
uv run learning-examples/verify_v2_account_layout.py    # offline: account layouts, PDAs, encoding
uv run learning-examples/verify_tx_status_checks.py     # offline: examples check transaction meta.err
uv run learning-examples/simulate_v2_trades.py <MINT>   # RPC simulation only; not proof of live success
```

Related docs: [Listening to pump.fun migrations](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium) · [Sniping with only logsSubscribe](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe)

## Throughput and rate limits

Every node provider has its own limits — method availability, requests per second, plan-specific caps. Consult your provider's docs before running the bot, and don't expect public RPC nodes to hold up.

One case worth knowing about: `getProgramAccounts` over the whole pump.fun program is no longer served by anyone. That program owns more than 10 million accounts, so providers reject the request or time out no matter which filters you pass. Use a filtered subscription instead — `bonding-curve-progress/get_graduating_tokens.py` shows the pattern.

For Chainstack, the numbers you need are in the [throughput guidelines](https://docs.chainstack.com/docs/limits), kept up to date.

The bot rate-limits itself with a token bucket: `node.max_rps` in the YAML (25 by default) smooths the request rate while allowing short bursts, and 429s are retried with exponential backoff.

For faster execution, Chainstack offers [Solana Trader nodes](https://docs.chainstack.com/docs/trader-nodes) for transaction propagation and the [Yellowstone gRPC Geyser plugin](https://docs.chainstack.com/docs/yellowstone-grpc-geyser-plugin) for streaming updates.

## IDLs

The IDLs under [`idl/`](idl/) are vendored from [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) — currently upstream commit `9c82f61`. To refresh, copy `pump.json`, `pump_amm.json`, and `pump_fees.json` into `pump_fun_idl.json`, `pump_swap_idl.json`, and `pump_fees.json`, and note the upstream commit in your commit message. Don't hand-edit them.

The `buy_v2` / `sell_v2` account lists are complete in the IDL — that's the point of the v2 interface. The **legacy** `buy` / `sell` lists are not: the IDL omits PDAs the on-chain program requires. For anything outside v2, cross-check against a recent successful on-chain transaction before trusting the IDL.

[CLAUDE.md](CLAUDE.md) documents the protocol gotchas in detail — account layouts, quote-mint handling, fee recipients, and what the IDL gets wrong.

## Contributing

Maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md). Open an **Issue** for feedback or bugs.

Lint and format the files you changed (`uv sync` installs `ruff` for you):

```bash
uv run ruff check --fix path/to/changed.py
uv run ruff format path/to/changed.py
```

Running `ruff check` over the whole repo reports a large backlog of pre-existing
errors — that's a known baseline, so scope it to your own files.

Then test your change with a learning example rather than by running a bot with real funds.
