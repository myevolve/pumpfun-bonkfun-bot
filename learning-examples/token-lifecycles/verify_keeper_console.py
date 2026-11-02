"""Offline assertions: temporary files and local Python emitters only."""

# ruff: noqa: S101, PLR2004, PLR0915, SLF001, TRY003 - isolated resource-boundary scenarios

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from capture_child_console import (
    CapturedChild,
    ConsoleCaptureError,
    _ConsoleHandler,
    _safe_message,
    start_child,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable


def emit(directory: Path, code: str, **limits: int) -> CapturedChild:
    return start_child(
        [sys.executable, "-B", "-u", "-c", code],
        cwd=directory,
        console_path=directory / "console.jsonl",
        pid_path=directory / "child.pid",
        **limits,
    )


def rows(directory: Path) -> list[dict[str, str]]:
    return [
        json.loads(line)
        for path in directory.glob("console.jsonl*")
        for line in path.read_text().splitlines()
    ]


def finish(child: CapturedChild) -> None:
    try:
        child.process.wait(timeout=5)
    finally:
        child.stop()
    assert child.process.poll() is not None
    assert not child.reader.is_alive()
    assert child.process.stdout.closed and child.process.stderr.closed
    assert child.handler.stream is None


def expect_failure(action: Callable[[], object]) -> None:
    try:
        action()
    except ConsoleCaptureError as exc:
        assert "SYNTHETIC" not in str(exc)
        assert exc.__cause__ is None
    else:
        raise AssertionError("console failure was hidden")


def verify_continuations(directory: Path) -> None:
    directory.mkdir()
    child = emit(
        directory,
        (
            "import sys,time\n"
            "sys.stdout.write('GEYSER_API_'); sys.stdout.flush(); time.sleep(0.03)\n"
            "sys.stdout.write('TOKEN=\\n'); sys.stdout.flush(); time.sleep(0.03)\n"
            "print('SYNTHETIC_CONTINUED_SECRET\\n\\nSYNTHETIC_AFTER_BLANK')\n"
            "print('Traceback: independent safe stderr', file=sys.stderr)\n"
        ),
    )
    finish(child)
    text = (directory / "console.jsonl").read_text()
    assert "SYNTHETIC" not in text
    assert "Traceback: independent safe stderr" in text
    assert "sensitive console continuation" in text
    urls = directory / "urls"
    urls.mkdir()
    child = emit(
        urls,
        (
            "print('endpoint=https://SYNTHETIC_USER:SYNTHETIC_PASSWORD@rpc.invalid/SYNTHETIC_PATH')\n"
            "print('SYNTHETIC_URL_CONTINUATION')\n"
        ),
    )
    finish(child)
    text = (urls / "console.jsonl").read_text()
    assert "REDACTED_URL" in text and "SYNTHETIC" not in text


def verify_scanning() -> None:
    # Ordinary permitted identifiers must not cause overlapping regex retries.
    ordinary = ("ordinary-" * 1024).encode()
    began = time.monotonic()
    assert _safe_message(ordinary) == ordinary.decode()
    assert time.monotonic() - began < 1.5


def verify_slow_cleanup(directory: Path) -> None:
    directory.mkdir()
    child = emit(
        directory,
        (
            "import pathlib,time,sys\n"
            "while not pathlib.Path('go').exists(): time.sleep(0.01)\n"
            "sys.stdout.write('line\\n' * 600); sys.stdout.flush()\n"
        ),
    )
    entered = threading.Event()
    real_handle = child.handler.handle

    def slow_handle(record: logging.LogRecord) -> bool:
        entered.set()
        time.sleep(0.01)
        return real_handle(record)

    with patch.object(child.handler, "handle", slow_handle):
        (directory / "go").touch()
        assert entered.wait(5)
        began = time.monotonic()
        expect_failure(lambda: child.stop(drain_timeout=0.01))
        assert time.monotonic() - began < 1
    assert child._closed and not child.reader.is_alive()
    assert child.process.stdout.closed and child.process.stderr.closed
    assert child.handler.stream is None
    expect_failure(child.stop)

    blocked = directory / "blocked"
    blocked.mkdir()
    child = emit(
        blocked,
        (
            "import pathlib,time\n"
            "while not pathlib.Path('go').exists(): time.sleep(0.01)\n"
            "print('one line', flush=True)\n"
        ),
    )
    entered, release = threading.Event(), threading.Event()
    real_handle = child.handler.handle

    def blocked_handle(record: logging.LogRecord) -> bool:
        entered.set()
        release.wait(5)
        return real_handle(record)

    with patch.object(child.handler, "handle", blocked_handle):
        (blocked / "go").touch()
        assert entered.wait(5)
        try:
            expect_failure(lambda: child.stop(drain_timeout=0.01))
            assert child.reader.is_alive() and not child._closed
            assert child.handler.stream is not None
        finally:
            release.set()
        expect_failure(lambda: child.stop(drain_timeout=2))
    assert child._closed and not child.reader.is_alive()
    assert child.handler.stream is None
    assert child.process.stdout.closed and child.process.stderr.closed


def verify_private_destinations(directory: Path) -> None:
    directory.mkdir()
    for name in ("console.jsonl", "console.jsonl.1", "child.pid"):
        case = directory / name.replace(".", "-")
        case.mkdir()
        target = case / "untouched"
        target.write_text("SYNTHETIC_UNTOUCHED_TARGET")
        (case / name).symlink_to(target)
        with patch("capture_child_console.subprocess.Popen") as popen:
            expect_failure(
                lambda selected=case: emit(selected, "print('never spawned')")
            )
        popen.assert_not_called()
        assert target.read_text() == "SYNTHETIC_UNTOUCHED_TARGET"
    # Swap immediately at the open boundary: a precheck alone would follow this.
    real_open = os.open
    for name in ("console.jsonl", "child.pid"):
        case = directory / ("raced-" + name.replace(".", "-"))
        case.mkdir()
        target = case / "untouched"
        target.write_text("SYNTHETIC_UNTOUCHED_TARGET")

        def raced_open(
            path: str | os.PathLike[str],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
            _case: Path = case,
            _name: str = name,
            _target: Path = target,
        ) -> int:
            if (
                path == _name
                and dir_fd is not None
                and not (_case / _name).is_symlink()
            ):
                (_case / _name).symlink_to(_target)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch("capture_child_console.os.open", raced_open):
            with patch("capture_child_console.subprocess.Popen") as popen:
                expect_failure(
                    lambda selected=case: emit(selected, "print('never spawned')")
                )
        popen.assert_not_called()
        assert target.read_text() == "SYNTHETIC_UNTOUCHED_TARGET"
    unsafe = directory / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with patch("capture_child_console.subprocess.Popen") as popen:
        expect_failure(lambda: emit(unsafe, "print('never spawned')"))
    popen.assert_not_called()
    fifo = directory / "fifo"
    fifo.mkdir()
    os.mkfifo(fifo / "console.jsonl")
    with patch("capture_child_console.subprocess.Popen") as popen:
        expect_failure(lambda: emit(fifo, "print('never spawned')"))
    popen.assert_not_called()


def verify(directory: Path) -> None:
    verify_scanning()
    verify_continuations(directory / "continuations")
    verify_slow_cleanup(directory / "slow-cleanup")
    verify_private_destinations(directory / "private-destinations")
    basic = directory / "basic"
    basic.mkdir()
    child = emit(
        basic,
        "import sys; print('startup stdout'); print('Traceback: startup stderr', file=sys.stderr)",
    )
    finish(child)
    captured = rows(basic)
    assert {row["logger"] for row in captured} == {
        "keeper.console.stdout",
        "keeper.console.stderr",
    }
    assert {row["message"] for row in captured} == {
        "startup stdout",
        "Traceback: startup stderr",
    }
    for row in captured:
        assert row["timestamp"].endswith("Z")
        assert (
            datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).tzinfo
            == UTC
        )
        assert row["event_id"] == str(child.process.pid)
    assert (basic / "child.pid").read_text() == str(child.process.pid)
    assert (basic / "child.pid").stat().st_mode & 0o777 == 0o600
    assert (basic / "console.jsonl").stat().st_mode & 0o777 == 0o600

    redaction = directory / "redaction"
    redaction.mkdir()
    secrets = [
        'SOLANA_PRIVATE_KEY="SYNTHETIC_PRIVATE_VALUE with spaces"',
        "GEYSER_API_TOKEN=SYNTHETIC_PREFIX_TOKEN",
        "service.token = SYNTHETIC_DOTTED_TOKEN",
        "Authorization: Bearer SYNTHETIC_BEARER",
        "url=https://SYNTHETIC_USER:SYNTHETIC_PASSWORD@rpc.invalid/SYNTHETIC_PATH?api-key=SYNTHETIC_QUERY",
        "ws=wss://rpc.invalid/opaque/SYNTHETIC_OPAQUE_PATH",
        "binary\x00SYNTHETIC_CONTROL",
    ]
    child = emit(
        redaction,
        "import sys; "
        + f"[print(line) for line in {secrets!r}]; sys.stdout.buffer.write(b'bad\\xffSYNTHETIC_UTF8\\n')",
    )
    finish(child)
    text = (redaction / "console.jsonl").read_text()
    assert "SYNTHETIC" not in text
    assert len(rows(redaction)) == len(secrets) + 1
    assert "CENSORED" in text

    rotation = directory / "rotation"
    rotation.mkdir()
    child = emit(
        rotation,
        "for i in range(400): print(str(i) + ':' + 'x' * 100)",
        max_bytes=2048,
        backup_count=3,
        record_limit=1024,
    )
    finish(child)
    files = list(rotation.glob("console.jsonl*"))
    assert {path.name for path in files} == {
        "console.jsonl",
        "console.jsonl.1",
        "console.jsonl.2",
        "console.jsonl.3",
    }
    assert all(path.stat().st_size <= 2048 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4 * 2048
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert any(row["message"].startswith("399:") for row in rows(rotation))

    oversized = directory / "oversized"
    oversized.mkdir()
    child = emit(
        oversized,
        "import sys; sys.stdout.write('SYNTHETIC_PREFIX' + 'x' * 200000 + 'SYNTHETIC_SUFFIX\\nnormal after oversize\\n'); sys.stderr.write('last without newline')",
        record_limit=1024,
    )
    finish(child)
    messages = [row["message"] for row in rows(oversized)]
    assert messages.count("[CENSORED: oversized console line]") == 1
    assert (
        "normal after oversize" not in messages and "last without newline" in messages
    )
    assert "SYNTHETIC" not in json.dumps(messages)
    assert all(
        len(line.encode()) + 1 <= 1024
        for line in (oversized / "console.jsonl").read_text().splitlines()
    )

    no_newline = directory / "no-newline"
    no_newline.mkdir()
    child = emit(
        no_newline,
        "import sys,time; sys.stdout.write('SYNTHETIC_START' + 'x' * 200000); sys.stdout.flush(); time.sleep(30)",
        record_limit=1024,
    )
    deadline = time.monotonic() + 5
    while not rows(no_newline) and time.monotonic() < deadline:
        time.sleep(0.01)
    child.stop()
    assert [row["message"] for row in rows(no_newline)] == [
        "[CENSORED: oversized console line]"
    ]
    assert not child.reader.is_alive() and child.process.poll() is not None

    encoded = directory / "encoded"
    encoded.mkdir()
    child = emit(encoded, "print('\u00b5' * 400)", record_limit=1024)
    finish(child)
    assert [row["message"] for row in rows(encoded)] == [
        "[CENSORED: oversized encoded console record]"
    ]

    gated_emitter = (
        "import pathlib,time\n"
        "while not pathlib.Path('go').exists(): time.sleep(0.01)\n"
        "print('x' * 100, flush=True)\n"
        "time.sleep(30)\n"
    )
    for stage in ("write", "rotation"):
        failure = directory / f"failure-{stage}"
        failure.mkdir()
        child = emit(failure, gated_emitter, max_bytes=1024, record_limit=512)

        def fail(*_args: object, **_kwargs: object) -> None:
            raise OSError("SYNTHETIC_SECRET_IN_FAILURE")

        if stage == "write":
            patcher = patch.object(child.handler.stream, "write", fail)
        else:
            child.handler.maxBytes = 1
            patcher = patch.object(_ConsoleHandler, "doRollover", fail)
        with patcher:
            (failure / "go").touch()
            try:
                expect_failure(lambda captured=child: captured.wait_console(5))
            finally:
                expect_failure(child.stop)
        assert child.process.poll() is not None and not child.reader.is_alive()
        assert child.process.stdout.closed and child.process.stderr.closed
        assert child.handler.stream is None

    pid_failure = directory / "pid-failure"
    pid_failure.mkdir()
    owned = []
    real_popen = subprocess.Popen

    def record_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        owned.append(process)
        return process

    with patch("capture_child_console.subprocess.Popen", record_popen):
        expect_failure(
            lambda: start_child(
                [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                cwd=pid_failure,
                console_path=pid_failure / "console.jsonl",
                pid_path=pid_failure,
            )
        )
    assert not owned  # Unsafe PID destinations are refused before spawning.
    with patch("capture_child_console.subprocess.Popen", record_popen):
        with patch(
            "capture_child_console.os.ftruncate",
            side_effect=OSError("SYNTHETIC_PID_WRITE_FAILURE"),
        ):
            expect_failure(lambda: emit(pid_failure, "import time; time.sleep(30)"))
    assert len(owned) == 1 and owned[0].poll() is not None
    assert owned[0].stdout.closed and owned[0].stderr.closed

    thread_failure = directory / "thread-failure"
    thread_failure.mkdir()
    with patch("capture_child_console.subprocess.Popen", record_popen):
        with patch(
            "capture_child_console.threading.Thread.start",
            side_effect=RuntimeError("SYNTHETIC_THREAD_FAILURE"),
        ):
            expect_failure(lambda: emit(thread_failure, "import time; time.sleep(30)"))
    assert len(owned) == 2 and owned[-1].poll() is not None
    assert owned[-1].stdout.closed and owned[-1].stderr.closed

    spawn_failure = directory / "spawn-failure"
    spawn_failure.mkdir()
    expect_failure(
        lambda: start_child(
            [str(spawn_failure / "missing-executable")],
            cwd=spawn_failure,
            console_path=spawn_failure / "console.jsonl",
            pid_path=spawn_failure / "child.pid",
        )
    )

    reader_failure = directory / "reader-failure"
    reader_failure.mkdir()
    child = emit(reader_failure, gated_emitter)
    with patch("capture_child_console.os", wraps=os) as file_io:
        file_io.read.side_effect = OSError("SYNTHETIC_READ_FAILURE")
        (reader_failure / "go").touch()
        try:
            expect_failure(lambda: child.wait_console(5))
        finally:
            expect_failure(child.stop)
    assert child.process.poll() is not None and not child.reader.is_alive()
    assert child.process.stdout.closed and child.process.stderr.closed

    shutdown = directory / "shutdown"
    shutdown.mkdir()
    child = emit(
        shutdown,
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)",
    )
    deadline = time.monotonic() + 5
    while not rows(shutdown) and time.monotonic() < deadline:
        time.sleep(0.01)
    child.stop(term_timeout=0.05, kill_timeout=1)
    assert child.process.returncode == -9 and not child.reader.is_alive()

    if hasattr(os, "fork"):
        held_pipe = directory / "held-pipe"
        held_pipe.mkdir()
        # The synthetic descendant exits itself; only the owned direct child is reaped.
        child = emit(
            held_pipe,
            "import os,time; p=os.fork(); time.sleep(0.8) if p == 0 else None; os._exit(0)",
        )
        child.process.wait(timeout=5)
        began = time.monotonic()
        expect_failure(lambda: child.stop(drain_timeout=0.05))
        assert time.monotonic() - began < 0.7 and not child.reader.is_alive()
        assert child.process.stdout.closed and child.process.stderr.closed
        time.sleep(
            0.8
        )  # Allow only this synthetic descendant to exit without signaling it.

    assert not any(
        thread.name.startswith("keeper-console-") for thread in threading.enumerate()
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="verify-keeper-console-") as temporary:
        verify(Path(temporary))
    print("verify_keeper_console ok (local emitters, temporary files only)")


if __name__ == "__main__":
    main()
