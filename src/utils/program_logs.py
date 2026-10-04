"""Attribute Solana log messages to completed runtime invocation frames."""

from __future__ import annotations

# Invocation arity and one explicit stack machine follow the runtime wire grammar.
# ruff: noqa: PLR2004


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
