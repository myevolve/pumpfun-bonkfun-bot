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
