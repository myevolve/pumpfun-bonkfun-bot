"""Bound child console bytes; ambiguous continuations stay censored until EOF.

Each stream keeps at most RECORD_LIMIT pending bytes. Sensitive, malformed or
oversized input poisons that stream for its remaining lifetime: even blank lines
cannot prove a multiline credential ended. Other-stream diagnostics remain useful,
but unstructured console output is not an authenticated or exhaustive secret filter.
"""
# ruff: noqa: C901, PLR0912, PLR0913, TRY003, TRY300, TRY301, BLE001
# Cleanup barriers retain ownership and surface every failure; never retry unlogged.

from __future__ import annotations

import logging
import os
import re
import selectors
import stat
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from utils.logger import _redact, _StructuredFormatter  # noqa: E402

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 3
RECORD_LIMIT = 64 * 1024
READ_BYTES = 8192
_URL = re.compile(r"(?i)(?<![\w+.-])[a-z][a-z0-9+.-]*://[^\s<>\"']+")
_LEXEME = re.compile(r"[\w.-]+")
_SECRET_PARTS = (
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "mnemonic",
    "seed",
    "authorization",
)


class ConsoleCaptureError(RuntimeError):
    """A safe diagnostic: never attach raw child data or underlying errors."""


def _directory_fd(directory: Path) -> int:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ConsoleCaptureError(
                "console directory must be owned and not group/world writable"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise ConsoleCaptureError(
            "console destination must be an owned single-link regular file"
        )


def _private_open(path: Path) -> int:
    directory = _directory_fd(path.parent)
    try:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    try:
        _check_file(os.fstat(fd))
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_rotations(path: Path, count: int) -> None:
    directory = _directory_fd(path.parent)
    try:
        for name in (
            path.name,
            *(f"{path.name}.{index}" for index in range(1, count + 1)),
        ):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _check_file(info)
    finally:
        os.close(directory)


class _ConsoleHandler(RotatingFileHandler):
    # Standard rollover in a trusted owned directory; file opens never follow links.
    # Same-UID adversarial directory replacement is outside this protection boundary.
    def _open(self) -> TextIO:
        return os.fdopen(
            _private_open(Path(self.baseFilename)),
            "a",
            encoding=self.encoding,
            errors=self.errors,
        )

    def doRollover(self) -> None:  # noqa: N802 - stdlib override
        _check_rotations(Path(self.baseFilename), self.backupCount)
        super().doRollover()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802, ARG002 - stdlib hook
        # RotatingFileHandler otherwise reports an error and silently keeps running.
        raise ConsoleCaptureError("console write or rotation failed") from None


class _ConsoleFormatter(_StructuredFormatter):
    def __init__(self, record_limit: int) -> None:
        super().__init__()
        self.record_limit = record_limit

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if len(rendered.encode("utf-8")) + 1 <= self.record_limit:
            return rendered
        censored = logging.makeLogRecord(
            {
                **record.__dict__,
                "msg": "[CENSORED: oversized encoded console record]",
                "args": (),
            }
        )
        return super().format(censored)


def _sensitive_assignment(text: str) -> bool:
    # Consume each complete lexeme once, instead of retrying a greedy pattern at
    # every word boundary inside a long hyphen-separated ordinary identifier.
    for match in _LEXEME.finditer(text):
        label = match.group().lower()
        end = match.end()
        if label in ("bearer", "basic") and end < len(text) and text[end].isspace():
            return True
        if not any(part in label for part in _SECRET_PARTS):
            continue
        if end < len(text) and text[end] in "\"'":
            end += 1
        while end < len(text) and text[end].isspace():
            end += 1
        if end < len(text) and text[end] in ":=":
            return True
    return False


def _safe_message(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "[CENSORED: non-UTF-8 console line]"
    if any(not char.isprintable() and char != "\t" for char in text):
        return "[CENSORED: unsafe console control characters]"
    # Raw startup stderr is not structured: censor the whole assignment, including
    # prefixed environment labels and values containing spaces, not just one token.
    if _sensitive_assignment(text):
        return "[CENSORED: sensitive console assignment]"
    # Opaque URL paths and query parameters can be credentials too.
    return _redact(_URL.sub("[REDACTED_URL]", text))


def _reap(process: subprocess.Popen, term_timeout: float, kill_timeout: float) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=term_timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=kill_timeout)
    else:
        process.wait()


class CapturedChild:
    """Own one child, one reader, two pipes and one rotating handler."""

    def __init__(
        self, process: subprocess.Popen, handler: _ConsoleHandler, record_limit: int
    ) -> None:
        self.process = process
        self.handler = handler
        self.record_limit = record_limit
        self._cancel = threading.Event()
        self._failed = threading.Event()
        self._failure: str | None = None
        self._closed = False
        # ponytail: censor through EOF; resume only with verified structured framing.
        self._censored_streams: set[str] = set()
        self.reader = threading.Thread(
            target=self._read_console,
            name=f"keeper-console-{process.pid}",
            daemon=True,
        )

    def wait_console(self, seconds: float) -> None:
        if self._failed.wait(seconds):
            raise ConsoleCaptureError(
                self._failure or "console capture failed"
            ) from None

    def _write(self, stream: str, message: str) -> None:
        if self._cancel.is_set():
            return
        record = logging.LogRecord(
            f"keeper.console.{stream}", logging.INFO, "", 0, message, (), None
        )
        record.event_id = str(self.process.pid)
        self.handler.handle(record)

    def _line(self, stream: str, raw: bytes) -> None:
        if stream in self._censored_streams:
            message = "[CENSORED: sensitive console continuation]"
        else:
            message = _safe_message(raw)
            if message.startswith("[CENSORED:") or "[REDACTED_URL]" in message:
                self._censored_streams.add(stream)
        self._write(stream, message)

    def _consume(
        self, stream: str, chunk: bytes, pending: bytes, *, discarding: bool
    ) -> tuple[bytes, bool]:
        for index, part in enumerate(chunk.split(b"\n")):
            if self._cancel.is_set():
                break
            if index:
                if not discarding:
                    self._line(stream, pending.rstrip(b"\r"))
                pending, discarding = b"", False
            if discarding:
                continue
            if len(pending) + len(part) > self.record_limit:
                self._censored_streams.add(stream)
                self._write(stream, "[CENSORED: oversized console line]")
                pending, discarding = b"", True
            else:
                pending += part
        return pending, discarding

    def _read_console(self) -> None:
        streams = (("stdout", self.process.stdout), ("stderr", self.process.stderr))
        try:
            with selectors.DefaultSelector() as selector:
                for name, pipe in streams:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, (name, b"", False))
                while selector.get_map() and not self._cancel.is_set():
                    for key, _ in selector.select(timeout=0.1):
                        if self._cancel.is_set():
                            break
                        name, pending, discarding = key.data
                        chunk = os.read(key.fd, READ_BYTES)
                        if not chunk:
                            if pending and not discarding:
                                self._line(name, pending.rstrip(b"\r"))
                            selector.unregister(key.fileobj)
                            continue
                        pending, discarding = self._consume(
                            name, chunk, pending, discarding=discarding
                        )
                        selector.modify(
                            key.fileobj,
                            selectors.EVENT_READ,
                            (name, pending, discarding),
                        )
                if self._cancel.is_set() and selector.get_map():
                    raise ConsoleCaptureError("console drain deadline exceeded")
        except Exception:
            self._failure = "console reader, write or rotation failed"
            self._failed.set()
            try:
                _reap(self.process, 10, 5)
            except Exception:
                self._failure = "console capture failed; owned child cleanup failed"
        finally:
            for _, pipe in streams:
                try:
                    pipe.close()
                except Exception:
                    self._failure = "console pipe cleanup failed"
                    self._failed.set()

    def stop(
        self,
        *,
        term_timeout: float = 10,
        kill_timeout: float = 5,
        drain_timeout: float = 2,
    ) -> None:
        if self._closed:
            self.wait_console(0)
            return
        cleanup_failure = False
        try:
            _reap(self.process, term_timeout, kill_timeout)
        except Exception:
            cleanup_failure = True
        if self.reader.ident is not None:
            self.reader.join(timeout=drain_timeout)
            if self.reader.is_alive():
                self._cancel.set()
                self.reader.join(timeout=0.5)
            if self.reader.is_alive():
                # A blocked sink cannot safely be interrupted. Keep ownership and
                # the handler open; the caller must retry cleanup once it unblocks.
                raise ConsoleCaptureError(
                    "console writer cleanup incomplete; retry stop"
                ) from None
        for pipe in (self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except Exception:
                cleanup_failure = True
        try:
            self.handler.close()
        except Exception:
            cleanup_failure = True
        if cleanup_failure:
            raise ConsoleCaptureError(
                "console owned-child cleanup failed; retry stop"
            ) from None
        self._closed = True
        self.wait_console(0)


def start_child(
    command: Sequence[str],
    *,
    cwd: Path,
    console_path: Path,
    pid_path: Path,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
    record_limit: int = RECORD_LIMIT,
) -> CapturedChild:
    """Start only after opening capture; roll back child ownership on any failure."""
    for value, minimum in ((max_bytes, 1024), (backup_count, 1), (record_limit, 512)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("invalid console retention limits")
    handler = None
    child = None
    process = None
    pid_fd = None
    try:
        _check_rotations(console_path, backup_count)
        # Open/validate PID output before spawning, without truncating an existing
        # file until ownership of a child has actually been acquired.
        pid_fd = _private_open(pid_path)
        handler = _ConsoleHandler(
            console_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(_ConsoleFormatter(min(record_limit, max_bytes - 1)))
        process = subprocess.Popen(  # noqa: S603 - keeper-owned argv, never a shell
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        child = CapturedChild(process, handler, record_limit)
        child.reader.start()
        os.ftruncate(pid_fd, 0)
        with os.fdopen(pid_fd, "a", encoding="ascii") as pid_file:
            pid_fd = None
            pid_file.write(str(process.pid))
        child.wait_console(0)
        return child
    except BaseException:
        cleanup_failed = False
        if child is not None:
            try:
                child.stop()
            except Exception:
                cleanup_failed = True
        else:
            if process is not None:
                try:
                    _reap(process, 10, 5)
                except Exception:
                    cleanup_failed = True
                for pipe in (process.stdout, process.stderr):
                    try:
                        pipe.close()
                    except Exception:
                        cleanup_failed = True
            if handler is not None:
                try:
                    handler.close()
                except Exception:
                    cleanup_failed = True
        message = (
            "console startup failed; cleanup failed"
            if cleanup_failed
            else "console capture startup failed"
        )
        raise ConsoleCaptureError(message) from None
    finally:
        if pid_fd is not None:
            os.close(pid_fd)
