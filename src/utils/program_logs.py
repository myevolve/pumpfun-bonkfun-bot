"""Attribute Solana log messages to completed runtime invocation frames."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

# Invocation arity and one explicit stack machine follow the runtime wire grammar.
# ruff: noqa: PLR2004

# Anchor ``emit_cpi!``: a program invokes itself with this prefix + event bytes.
EVENT_CPI_PREFIX = bytes.fromhex("e445a52e51cb9a1d")


def attribute_program_logs(  # noqa: C901, PLR0912 - validate the whole invocation stack
    logs: list[str],
) -> list[tuple[int, str, str, bool]]:
    """Return (log index, emitting program, full message, committed) records.

    Validate the whole stack before returning anything. A caught CPI failure
    invalidates its descendants, not successful siblings. A top-level failure
    invalidates the entire transaction, including earlier successful instructions.
    Callers must still require successful transaction metadata from their provider.
    """
    stack: list[tuple[str, int]] = []
    entries: list[tuple[int, str, str, bool]] = []
    transaction_failed = False
    for index, log in enumerate(logs):
        if log.startswith(("Program data: ", "Program log: ")):
            if not stack:
                raise ValueError("unbound_program_message")
            entries.append((index, stack[-1][0], log, True))
            continue
        parts = log.split()
        if len(parts) < 3 or parts[0] != "Program":
            continue
        if parts[2] == "invoke":
            if len(parts) != 4 or parts[3] != f"[{len(stack) + 1}]":
                raise ValueError("ambiguous_invocation_depth")
            stack.append((parts[1], len(entries)))
        elif parts[2] in {"success", "failed:"}:
            if not stack or stack[-1][0] != parts[1]:
                raise ValueError("unmatched_program_completion")
            if parts[2] == "success" and len(parts) != 3:
                raise ValueError("ambiguous_program_completion")
            _, start = stack.pop()
            if parts[2] == "failed:":
                for offset in range(start, len(entries)):
                    i, program, message, _ = entries[offset]
                    entries[offset] = (i, program, message, False)
                transaction_failed |= not stack
    if stack:
        raise ValueError("truncated_program_stack")
    if transaction_failed:
        return [(i, program, message, False) for i, program, message, _ in entries]
    return entries


def attribute_events(
    logs: list[str], instructions: Sequence[Any] = ()
) -> list[tuple[int, str, str, bool]]:
    """``attribute_program_logs``, recovering a truncated stack from event-CPI.

    Solana caps a transaction's logs at 10 KB; big bundles and pump.fun's
    migrate_v2 overflow it and lose their trailing ``Program data:`` lines.
    Anchor repeats each event as a self-invocation whose instruction data is
    never truncated. Pass only a successful transaction's normalized
    instructions: a failed CPI fails the whole transaction, so every recovered
    event committed, and only the program itself can sign its event authority.
    Returned indices are instruction positions, not log indices.
    """
    try:
        return attribute_program_logs(logs)
    except ValueError as exc:
        if str(exc) != "truncated_program_stack":
            raise
        recovered = [
            (
                position,
                instruction.program_id,
                "Program data: " + base64.b64encode(instruction.data[8:]).decode(),
                True,
            )
            for position, instruction in enumerate(instructions)
            if instruction.inner_index is not None
            and instruction.data is not None
            and instruction.data[:8] == EVENT_CPI_PREFIX
        ]
        if not recovered:
            raise
        return recovered
