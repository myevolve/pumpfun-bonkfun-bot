"""Offline: head deadlines, reorgs, priced native results and fatal RPC boundaries.

Only in-memory provider responses are used. No credentials, network or signing.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import evaluate_evm_capital as capital
import observe_evm_meme_cycles as observer
from evaluate_evm_capital import (
    RPC,
    BoundReached,
    RPCError,
    RPCReadError,
    require,
    uint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Boundary values deliberately exercise the actual observer and RPC implementations.
# ruff: noqa: PLR2004
ADDRESSES = ["0x" + f"{n:040x}" for n in range(1, 7)]
BASE, CALLER, EXECUTOR, TOKEN, ENTRY, EXIT = ADDRESSES
ROUTE = {"token": TOKEN, "entry": ENTRY, "exit": EXIT}
SELECTORS = {observer.RUN: "00000000", observer.HYBRID: "00000001"}
SCOPE = {
    "chain": "robinhood",
    "selection_id": "offline",
    "duration_seconds": 60,
    "request_limit": 30,
    "request_interval_seconds": 0.3,
    "max_notifications": 100,
    "max_latency_ms": 1000,
    "max_head_age_seconds": 10,
    "wrapped_native": BASE,
    "caller": CALLER,
    "executor": EXECUTOR,
    "minimum_profit_raw": "1",
    "gas_limit": 300000,
    "gas_price_raw": "2",
    "max_fee_raw": "600000",
    "runtime_sha256": "0" * 64,
    "amounts_raw": ["1000000"],
}


def raw_head(number: int, identity: int, parent: int) -> dict:
    """Provide a deterministic, syntactically valid public block header."""
    return {
        "number": hex(number),
        "hash": "0x" + f"{identity:064x}",
        "parentHash": "0x" + f"{parent:064x}",
        "timestamp": hex(1000),
        "baseFeePerGas": "0x1",
    }


async def verify_heads() -> None:
    """Drive the real subscription through backlog and a replacement branch."""
    queue = asyncio.Queue()
    consumed = asyncio.Event()
    clock = {"mono": 100.0, "wall": 1000.0}
    fake_time = SimpleNamespace(
        monotonic=lambda: clock["mono"], time=lambda: clock["wall"]
    )

    async def receive_json(**_kwargs: object) -> dict:
        message = await queue.get()
        consumed.set()
        return message

    @contextlib.asynccontextmanager
    async def ws_connect(
        *_args: object, **_kwargs: object
    ) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(send_json=AsyncMock(), receive_json=receive_json)

    async def deliver(number: int, identity: int, parent: int) -> None:
        consumed.clear()
        queue.put_nowait(
            {
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {
                    "subscription": "opaque",
                    "result": raw_head(number, identity, parent),
                },
            }
        )
        await consumed.wait()

    queue.put_nowait({"jsonrpc": "2.0", "id": 1, "result": "opaque"})
    with patch.object(observer, "time", fake_time):
        heads = observer.Heads(SCOPE, 200, lambda *_a, **_k: None)
        stream = asyncio.create_task(
            heads.listen(SimpleNamespace(ws_connect=ws_connect), "unused")
        )
        try:
            await consumed.wait()
            await deliver(100, 100, 99)
            clock["mono"] = 102
            await deliver(101, 101, 100)
            require(
                heads.pending["received_monotonic"] == 102, "exact_head_receipt_lost"
            )
            pending_expired = not heads.freshness(heads.pending)["fresh"]
            await heads.take()
            clock["mono"] = 102.1
            await deliver(102, 102, 101)
            first = await heads.take()
            await deliver(102, 202, 101)
            await heads.take()
            require(not heads.freshness(first)["fresh"], "replacement_not_rejected")
            await deliver(104, 204, 203)
            require(
                pending_expired and not heads.freshness(first)["fresh"],
                "pending_deadline_or_sticky_reorg_failed",
            )
        finally:
            stream.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream


async def verify_native(mode: str) -> None:  # noqa: C901 - one native execution and fee-control scenario
    """Late fee diagnostics cannot erase immediate success or accept a later fork."""
    clock = {"mono": 100.0}
    fake_time = SimpleNamespace(monotonic=lambda: clock["mono"], time=lambda: 1000.0)
    with patch.object(observer, "time", fake_time):
        heads = observer.Heads(SCOPE, 200, lambda *_a, **_k: None)
        h = observer.header(raw_head(100, 100, 99))
        h.update(received_at=1000.0, received_monotonic=100.0)
        heads.latest = h

        async def call(method: str, params: list) -> str | dict:
            if method == "eth_call":
                clock["mono"] = 100.1
                if mode == "rejected":
                    raise RPCError(method, 3, "0x")
                return "0x" + uint(100000).hex()
            if method == "eth_getBlockByNumber":
                return raw_head(100, 100, 99)
            require(method == "eth_estimateGas", "unexpected_native_method")
            clock["mono"] = 102
            if mode == "reorg":
                heads.latest = {"hash": "replacement", "number": 100}
            if mode == "unpriced":
                raise RPCError(method, -32602, None)
            if params[2][EXECUTOR]["code"] == "0x60006000fd":
                raise RPCError(method, 3, "0x")
            return hex(30000)

        row = await observer.evaluate(
            SimpleNamespace(call=call),
            SCOPE,
            "0x00",
            SELECTORS,
            ROUTE,
            1000000,
            {"blockHash": h["hash"], "requireCanonical": True},
            heads,
            h,
            lambda *_a, **_k: None,
            0,
        )
        if mode == "priced":
            require(
                row["net_raw"] == "40000" and row["opportunity_eligible"],
                "priced_payout_not_eligible",
            )
            require(not row["freshness"]["fresh"], "late_fee_diagnostic_not_exercised")
        elif mode == "unpriced":
            require(
                row["net_raw"] is None and not row["opportunity_eligible"],
                "missing_fee_became_profit",
            )
        elif mode == "reorg":
            require(not row["opportunity_eligible"], "post_execution_reorg_accepted")
        else:
            require(
                row.get("definitive_native_rejection")
                and not row["opportunity_eligible"],
                "native_revert_misclassified",
            )


async def verify_rpc() -> None:  # noqa: C901, PLR0915 - one bounded transport check
    """Correlate reversed batches, charge every method and stop on HTTP failure."""
    requests = []
    response_status = 200
    response_payload = None

    @contextlib.asynccontextmanager
    async def post(_endpoint: str, **kwargs: object) -> AsyncIterator[SimpleNamespace]:
        request = kwargs["json"]
        requests.append(request)
        rows = (
            [
                {"jsonrpc": "2.0", "id": item["id"], "result": hex(index + 10)}
                for index, item in enumerate(request)
            ]
            if isinstance(request, list)
            else []
        )
        content = asyncio.StreamReader()
        content.feed_data(
            json.dumps(
                list(reversed(rows))
                if isinstance(request, list)
                else {"jsonrpc": "2.0", "id": request["id"], "result": response_payload}
            ).encode()
        )
        content.feed_eof()
        yield SimpleNamespace(status=response_status, content=content)

    rpc = RPC(
        SimpleNamespace(post=post), "unused", time.monotonic() + 60, request_limit=3
    )
    values = await rpc.batch(
        [
            ("eth_chainId", []),
            ("eth_gasPrice", []),
            ("eth_getBalance", [CALLER, "latest"]),
        ]
    )
    require(values == ["0xa", "0xb", "0xc"], "batch_responses_misattributed")
    try:
        await rpc.call("eth_chainId", [])
    except BoundReached:
        require(len(requests) == 1, "exhausted_batch_budget_started_http")
    else:
        raise AssertionError("batch_budget_not_charged")

    # A full native receipt batch can exceed the small quote-reader default.
    response_payload = [
        {
            "transactionHash": "0x" + "ab" * 32,
            "logs": [{"data": "0x" + "aa" * (1024 * 1024)}],
        }
    ]
    body_size = len(
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": response_payload}).encode()
    )
    for limit, accepted in (
        (None, False),
        (4 * 1024 * 1024, True),
        (body_size - 1, False),
    ):
        options = {} if limit is None else {"response_limit": limit}
        bounded_rpc = RPC(
            SimpleNamespace(post=post),
            "unused",
            time.monotonic() + 60,
            request_limit=1,
            **options,
        )
        try:
            receipts = await bounded_rpc.call(
                "eth_getBlockReceipts", ["0x" + "cd" * 32]
            )
        except RPCReadError as exc:
            require(
                not accepted and str(exc) == "rpc_response_too_large",
                "wrong_response_boundary_failure",
            )
        else:
            require(
                accepted and receipts[0]["transactionHash"] == "0x" + "ab" * 32,
                "response_limit_or_complete_body_not_preserved",
            )
    for invalid_limit in (0, True, 16 * 1024 * 1024 + 1):
        try:
            RPC(None, "unused", time.monotonic() + 60, response_limit=invalid_limit)
        except ValueError as exc:
            require(str(exc) == "invalid_response_limit", "wrong_response_bound_error")
        else:
            raise AssertionError("invalid_response_limit_accepted")
    response_payload = None

    # Work between pacing and dispatch must not steal spacing from the next call.
    clock = SimpleNamespace(now=100.0)
    starts = []

    async def advance(seconds: float) -> None:
        clock.now += seconds

    @contextlib.asynccontextmanager
    async def delayed_post(
        endpoint: str, **kwargs: object
    ) -> AsyncIterator[SimpleNamespace]:
        if not starts:
            clock.now += 0.1
        starts.append(clock.now)
        async with post(endpoint, **kwargs) as response:
            yield response

    fake_asyncio = SimpleNamespace(
        Lock=asyncio.Lock,
        sleep=advance,
        IncompleteReadError=asyncio.IncompleteReadError,
    )
    with (
        patch.object(capital, "time", SimpleNamespace(monotonic=lambda: clock.now)),
        patch.object(capital, "asyncio", fake_asyncio),
    ):
        paced = RPC(
            SimpleNamespace(post=delayed_post),
            "unused",
            110.0,
            request_limit=2,
            request_interval=0.35,
        )
        await paced.call("eth_chainId", [])
        await paced.call("eth_chainId", [])
    require(starts[1] - starts[0] >= 0.349, "dispatch_setup_stole_request_spacing")

    response_status = 429
    requests.clear()
    events = []
    h = observer.header(raw_head(100, 100, 99))
    h.update(received_at=time.time(), received_monotonic=time.monotonic())
    fake_heads = SimpleNamespace(
        listen=AsyncMock(),
        take=AsyncMock(side_effect=[h, h, None]),
        pending=None,
        stop_reason="stream_unavailable",
        notifications=2,
        coalesced=0,
        missed=0,
        freshness=lambda _h: {
            "fresh": True,
            "stream_live": True,
            "stream_branch_valid": True,
        },
    )
    with (
        patch.object(observer.aiohttp, "ClientSession") as session,
        patch.object(observer, "Heads", return_value=fake_heads),
        patch.object(observer, "attest", AsyncMock(return_value=[ROUTE])),
        patch.object(
            observer,
            "emit",
            lambda _out, event, **fields: events.append({"event": event, **fields}),
        ),
    ):
        session.return_value.__aenter__.return_value = SimpleNamespace(post=post)
        status = await observer.run(
            SCOPE,
            {"rpc_url": "unused", "wss_url": "unused"},
            "0x00",
            SELECTORS,
            "0" * 64,
            {},
            io.StringIO(),
        )
    require(
        status != 0 and len(requests) == 1,
        "fatal_transport_retried_or_reported_success",
    )
    require(
        events[-1]["reason"] == "observation_failed", "fatal_transport_not_terminal"
    )


async def verify_episodes(mode: str) -> None:
    """Unknown coverage cannot split episodes or certify an observed boundary."""
    outcomes = [100, None, 100, -100, None, 100] if mode == "gaps" else [100, 100]
    frames = []
    for index in range(len(outcomes)):
        h = observer.header(raw_head(100 + index, 100 + index, 99 + index))
        h.update(received_at=time.time(), received_monotonic=time.monotonic())
        frames.append(h)
    events = []
    heads = SimpleNamespace(
        listen=AsyncMock(),
        take=AsyncMock(side_effect=[*frames, None]),
        pending=None,
        stop_reason=None,
        notifications=len(frames),
        coalesced=0,
        missed=0,
        freshness=lambda _h: {
            "fresh": True,
            "stream_live": heads.stop_reason is None,
            "stream_branch_valid": True,
        },
    )
    next_outcome = iter(enumerate(outcomes))

    async def evaluate(*_args: object) -> dict:
        index, net = next(next_outcome)
        stream_live = mode != "stream_lost" or index == 0
        if not stream_live:
            heads.stop_reason = "stream_unavailable"
        return {
            "opportunity_eligible": net is not None and net > 0 and stream_live,
            "net_raw": None if net is None else str(net),
            "amount_raw": "1000000",
            "canonical_after_execution": True,
            "immediate_freshness": {"fresh": True},
            "freshness": {"stream_live": stream_live, "stream_branch_valid": True},
        }

    with (
        patch.object(observer, "Heads", return_value=heads),
        patch.object(observer, "attest", AsyncMock(return_value=[ROUTE])),
        patch.object(
            observer,
            "execution_context",
            AsyncMock(
                return_value=({"base_fee_raw": 1, "caller_balance_raw": "1000000"}, 0)
            ),
        ),
        patch.object(observer, "evaluate", evaluate),
        patch.object(
            observer,
            "emit",
            lambda _out, event, **fields: events.append(
                json.loads(json.dumps({"event": event, **fields}))
            ),
        ),
    ):
        status = await observer.run(
            SCOPE,
            {"rpc_url": "unused", "wss_url": "unused"},
            "0x00",
            SELECTORS,
            "0" * 64,
            {},
            io.StringIO(),
        )
    require(status != 0, "truncated_stream_reported_success")
    observed = [
        row["episode"] for row in events if row["event"] == "capital_episode_observed"
    ]
    closed = [row for row in events if row["event"] == "capital_episode_closed"]
    if mode == "gaps":
        require(
            [row["id"] for row in observed] == [1, 1, 2],
            "gap_invented_independent_episode",
        )
        require(observed[-1]["left_censored"], "unknown_pre_entry_coverage_lost")
        require(
            [row["right_censored"] for row in closed] == [False, True],
            "episode_closure_evidence_lost",
        )
    else:
        require(
            len(closed) == 1 and closed[0]["right_censored"],
            "dead_stream_closed_episode_as_negative",
        )


async def verify_v4_admission() -> None:
    """A mismatched PoolKey or wrapped-native key cannot become a priced route."""
    manager, state_view = observer.V4_DEPLOYMENTS["robinhood"]
    pool = {
        "family": "canonical_v4",
        "pool_id": "0x" + "11" * 32,
        "manager": manager,
        "state_view": state_view,
        "currency0": observer.ZERO,
        "currency1": TOKEN,
        "fee": 3000,
        "tick_spacing": 60,
        "hooks": observer.ZERO,
    }
    scope = {**SCOPE, "markets": [{"token": TOKEN, "pools": [pool]}]}
    observer.admit_v4(scope, TOKEN, pool)
    for changed in (
        {"currency0": BASE},
        {"currency1": BASE},
        {"hooks": ENTRY},
        {"fee": 0x800000},
        {"tick_spacing": 0},
        {"manager": observer.V4_DEPLOYMENTS["polygon"][0]},
    ):
        try:
            observer.admit_v4(scope, TOKEN, {**pool, **changed})
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported_v4_key_admitted")
    polygon = {
        **pool,
        "manager": observer.V4_DEPLOYMENTS["polygon"][0],
        "state_view": observer.V4_DEPLOYMENTS["polygon"][1],
    }
    observer.admit_v4({**scope, "chain": "polygon"}, TOKEN, polygon)

    async def contract(_to: str, data: str, _bank: dict) -> str:
        if data == "0xc45a0155":
            return "0x" + uint(int(observer.NETWORKS["robinhood"]["factory"], 16)).hex()
        if data == "0x4aa4a4fc":
            return "0x" + uint(int(BASE, 16)).hex()
        require(data == "0x313ce567", "unexpected_attestation_read")
        return "0x" + uint(18).hex()

    async def call(method: str, _params: list) -> str:
        if method == "eth_chainId":
            return hex(4663)
        require(method == "eth_call", "unexpected_attestation_method")
        return "0x" + "22" * 32  # A different key, never admitted.

    events = []
    rpc = SimpleNamespace(
        call=call,
        contract=contract,
        code=AsyncMock(),
        block=AsyncMock(return_value=(raw_head(100, 100, 99), {"blockHash": "bank"})),
    )
    routes = await observer.attest(
        rpc, scope, lambda event, **fields: events.append({"event": event, **fields})
    )
    require(
        not routes
        and any(row.get("reason") == "v4_pool_key_hash_mismatch" for row in events),
        "mismatched_pool_key_became_executable",
    )


async def verify_fair_coverage() -> None:
    """Expired banks rotate route/size attempts without certifying missing coverage."""
    routes = [ROUTE, {**ROUTE, "entry": EXIT, "exit": ENTRY}]
    scope = {**SCOPE, "amounts_raw": ["1000000", "2000000"]}
    frames = [observer.header(raw_head(100 + n, 100 + n, 99 + n)) for n in range(4)]
    attempted = set()
    visited, events = [], []
    heads = SimpleNamespace(
        listen=AsyncMock(),
        take=AsyncMock(side_effect=[*frames, None]),
        pending=None,
        stop_reason=None,
        notifications=4,
        coalesced=0,
        missed=0,
        freshness=lambda h: {"fresh": h["hash"] not in attempted},
    )

    async def evaluate(
        _rpc: object,
        _s: dict,
        _runtime: str,
        _selectors: dict,
        route: dict,
        amount: int,
        _bank: dict,
        _heads: object,
        h: dict,
        _record: object,
        _nonce: int,
    ) -> dict:
        attempted.add(h["hash"])
        visited.append((route["entry"], amount))
        return {"opportunity_eligible": False, "net_raw": None}

    with (
        patch.object(observer, "Heads", return_value=heads),
        patch.object(observer, "attest", AsyncMock(return_value=routes)),
        patch.object(
            observer,
            "execution_context",
            AsyncMock(
                return_value=({"base_fee_raw": 1, "caller_balance_raw": "1000000"}, 0)
            ),
        ),
        patch.object(observer, "evaluate", evaluate),
        patch.object(
            observer,
            "emit",
            lambda _out, event, **fields: events.append({"event": event, **fields}),
        ),
    ):
        await observer.run(
            scope,
            {"rpc_url": "unused", "wss_url": "unused"},
            "0x00",
            SELECTORS,
            "0" * 64,
            {},
            io.StringIO(),
        )
    require(
        visited
        == [(ENTRY, 1000000), (ENTRY, 2000000), (EXIT, 1000000), (EXIT, 2000000)],
        "later_routes_or_sizes_starved",
    )
    rows = [row for row in events if row["event"] == "head_evaluated"]
    require(
        len(rows) == 4
        and all(
            row["attempted"] == 1
            and row["unattempted"] == 3
            and not row["coverage_complete"]
            for row in rows
        ),
        "rotation_extended_deadline_or_invented_coverage",
    )


async def verify() -> None:
    """Run consumer-visible boundaries without any remote or funded operation."""
    await verify_heads()
    for mode in ("priced", "unpriced", "reorg", "rejected"):
        await verify_native(mode)
    await verify_rpc()
    await verify_v4_admission()
    await verify_fair_coverage()
    for mode in ("gaps", "stream_lost"):
        await verify_episodes(mode)
    print(
        "PASS: pending deadlines, sticky reorgs, exact fee net and fatal RPC boundaries; no network or signing"
    )


if __name__ == "__main__":
    asyncio.run(verify())
