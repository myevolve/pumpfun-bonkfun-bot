# ruff: noqa: S101, S603
"""The built wheel must still expose the bot's root modules."""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    "bot_runner",
    "config_loader",
    "dashboard",
    "dashboard_theme",
    "onboard",
)


def test_wheel_install_imports_entry_point_and_root_modules(tmp_path: Path) -> None:
    uv = _uv()
    wheel_dir = tmp_path / "wheels"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    names = _wheel_names(wheel)
    for module in MODULES:
        assert f"{module}.py" in names
    assert any(name.startswith("meme_studio/") for name in names)
    venv = tmp_path / "venv"
    subprocess.run([uv, "venv", str(venv), "--python", sys.executable], check=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    script = (
        "import bot_runner, config_loader, dashboard, dashboard_theme, onboard\n"
        "assert callable(bot_runner.main)\n"
    )
    probe = subprocess.run(
        [str(python), "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def _wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def _uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "uv"
    if candidate.is_file():
        return str(candidate)
    env_uv = os.environ.get("UV")
    if env_uv:
        return env_uv
    message = "uv is required to build the wheel"
    raise RuntimeError(message)
