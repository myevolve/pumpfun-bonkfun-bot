"""Offline UTC/progress checks using synthetic supervisor input, never a real child.

Run: python -B learning-examples/token-lifecycles/verify_keeper_health.py
"""

# ruff: noqa: S101, S603, PLR2004 - fixed local diagnostics and exact clock boundaries
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import keep_whaleride_shadow as shadow


def verify_utc() -> None:
    """An ISO UTC timestamp must retain its epoch under any local timezone."""
    program = (
        "import calendar,json,sys,time; "
        f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r}); "
        "import keep_whaleride_shadow as keeper; time.tzset(); "
        "values=['2026-10-31T12:00:00Z','2026-11-02T12:00:00Z']; "
        "print(json.dumps([keeper._parse_ts(value) - "
        "calendar.timegm(time.strptime(value,'%Y-%m-%dT%H:%M:%SZ')) for value in values]))"
    )
    for zone in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", program],
            env={"TZ": zone, "PATH": os.defpath},
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert json.loads(result.stdout) == [0.0, 0.0], f"UTC parsing depends on {zone}"


def verify_progress() -> None:
    """A single quiet poll is healthy; 901 seconds without slot progress is not."""
    clock = {"now": 0.0, "slot": 0}
    ticks = iter(((30.0, 1), (930.0, 2), (960.0, 2), (1831.0, 2)))
    handlers: dict[int, object] = {}
    stops: list[float] = []

    class SyntheticChild:
        process = SimpleNamespace(pid=123456, poll=lambda: None)

        def wait_console(self, _seconds: float) -> None:
            clock["now"], clock["slot"] = next(ticks)

        def stop(self) -> None:
            stops.append(clock["now"])
            assert clock["now"] == 1831.0, "restart fired before 900s of unchanged slot"
            handlers[shadow.signal.SIGTERM](shadow.signal.SIGTERM, None)

    child = SyntheticChild()
    with tempfile.TemporaryDirectory(prefix="verify-keeper-health-") as temporary:
        directory = Path(temporary)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(shadow, "SHADOW_DIR", directory),
            patch.object(shadow, "SHADOW_PATH", directory / "shadow.jsonl"),
            patch.object(shadow, "KEEPER_LOG", directory / "keeper.log"),
            patch.object(shadow, "PID_FILE", directory / "collector.pid"),
            patch.object(shadow, "GAVE_UP", directory / "keeper.gave-up"),
            patch.object(shadow, "spawn", return_value=child),
            patch.object(
                shadow, "watch_state", side_effect=lambda: (clock["now"], clock["slot"])
            ),
            patch.object(shadow.time, "time", side_effect=lambda: clock["now"]),
            patch.object(
                shadow.signal,
                "signal",
                side_effect=lambda number, handler: handlers.__setitem__(
                    number, handler
                ),
            ),
        ):
            assert shadow.run() == 0
        events = [
            json.loads(line.split(" ", 1)[1])
            for line in (directory / "keeper.log").read_text().splitlines()
        ]
        assert [event["reason"] for event in events if event["event"] == "restart"] == [
            "slot_frozen"
        ]
        assert stops == [1831.0]


def main() -> None:
    """Collect both independent regressions so the pre-fix failure is explicit."""
    failures = []
    for label, check in (("UTC", verify_utc), ("slot progress", verify_progress)):
        try:
            check()
        except AssertionError as error:
            failures.append(f"{label}: {error}")
    if failures:
        raise AssertionError("; ".join(failures))
    print(
        "keeper health: OK (timezone-invariant UTC, progress reset, sustained 900s stall; no real child)"
    )


if __name__ == "__main__":
    main()
