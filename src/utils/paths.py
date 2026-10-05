"""One absolute root for every durable state path.

State used to be resolved against the process working directory, so the same
wallet opened a different ledger, position journal and cleanup journal when a
restart used a different ``WorkingDirectory``. Both instances could then submit
while each believed it was alone, and a restart could miss open positions and
unresolved signatures. Every state path is anchored to the project root
instead; nothing here reads credentials or creates directories.
"""

from __future__ import annotations

from pathlib import Path

# src/utils/paths.py -> src/utils -> src -> project root
REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / ".state"


def state_path(*parts: str) -> Path:
    """Return a path under the absolute state directory."""
    return STATE_DIR.joinpath(*parts)


def legacy_cwd_state_conflict(cwd: Path | None = None) -> Path | None:
    """Return a cwd-relative ``.state`` that predates root anchoring, if any.

    Durable state used to live in ``.state`` relative to the working
    directory. A deployment started from another directory that upgrades to
    this anchoring would otherwise silently open a fresh, empty state tree
    and lose open positions, unresolved wires and cumulative session risk.
    Callers must fail closed on the returned path: its contents belong to the
    previous state model and must be migrated deliberately, never auto-read.
    """
    cwd = Path.cwd() if cwd is None else Path(cwd)
    candidate = (cwd / ".state").resolve()
    if candidate == STATE_DIR.resolve():
        return None
    if not candidate.exists():
        return None
    if not any(candidate.iterdir()):
        return None
    return candidate
