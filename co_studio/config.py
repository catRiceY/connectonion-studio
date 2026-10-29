"""Paths, ports, and constants shared across the studio."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from filelock import FileLock

STUDIO_HOST = "127.0.0.1"  # manager is loopback-only: agent ports are unauthenticated
STUDIO_PORT = 9900
AGENT_PORT_RANGE = range(8000, 8100)

def _configured_directory(name: str, default: Path) -> Path:
    """Explicit absolute paths let preview builds avoid the user's live data."""
    value = os.environ.get(name)
    if value is None:
        return default
    directory = Path(value)
    if not value.strip() or not directory.is_absolute():
        raise ValueError(f"{name} must be a non-empty absolute directory")
    return directory.resolve()


STUDIO_HOME = _configured_directory("STUDIO_DATA_DIR", Path.home() / ".co-studio")
SETTINGS_FILE = STUDIO_HOME / "config.json"
SETTINGS_LOCK = STUDIO_HOME / "config.lock"
DEFAULT_AGENTS_DIR = STUDIO_HOME / "agents"
INDEX_LOCK = STUDIO_HOME / "index.lock"


def _read_settings() -> dict:
    """The persisted studio config (empty dict if absent/unreadable)."""
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_setting(key: str, value: object) -> None:
    """Atomically update one persisted setting without losing concurrent changes."""
    STUDIO_HOME.mkdir(parents=True, exist_ok=True)
    with FileLock(str(SETTINGS_LOCK)):
        data = _read_settings()
        data[key] = value
        tmp = SETTINGS_FILE.with_name(f"{SETTINGS_FILE.name}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, SETTINGS_FILE)


def save_agents_dir(path: Path) -> None:
    """Persist the agents directory so the choice survives a restart."""
    _save_setting("agents_dir", str(path))


APPEARANCES = ("warm", "lavender")


def configured_appearance() -> str | None:
    """Return the saved appearance, or None before the user has made/migrated a choice."""
    value = _read_settings().get("appearance")
    return value if value in APPEARANCES else None


def appearance() -> str:
    """The saved appearance with Warm as the first-run fallback."""
    return configured_appearance() or "warm"


def save_appearance(value: str) -> None:
    """Persist a validated appearance independently of the web origin/manager port."""
    if value not in APPEARANCES:
        raise ValueError(f"unknown appearance: {value!r}")
    _save_setting("appearance", value)


# Where agent folders live. Loaded from config.json (falls back to the default),
# and reassigned at runtime by storage.change() when the user moves it.
_saved = _read_settings().get("agents_dir")
AGENTS_DIR = Path(_saved).expanduser() if _saved else DEFAULT_AGENTS_DIR

MAIN_CO_DIR = _configured_directory("AGENT_CONFIG_PATH", Path.home() / ".co")
KEYS_ENV = MAIN_CO_DIR / "keys.env"

PACKAGE_DIR = Path(__file__).resolve().parent
RUNNER_PATH = PACKAGE_DIR / "runner" / "co_studio_runner.py"
TEMPLATE_PATH = PACKAGE_DIR / "templates" / "agent.py.tmpl"
CO_AI_TEMPLATE_PATH = PACKAGE_DIR / "templates" / "co_ai_agent.py.tmpl"
FRONTEND_DIR = PACKAGE_DIR / "frontend"  # bundled inside the package so it ships in the wheel
SKILLS_DIR = PACKAGE_DIR / "skills"      # bundled SKILL.md catalog for the co-ai skills store


def agent_python() -> str:
    """Absolute path to the Python interpreter used to spawn agent subprocesses.

    The supervisor launches each agent as ``[agent_python(), RUNNER_PATH, agent.py]``. In every
    normal deployment ``sys.executable`` already IS a real interpreter that can import connectonion:
    a pip/venv install resolves to the venv python, and the bundled macOS .app is launched as
    ``…/Contents/Resources/python/bin/python3 -m co_studio`` (module form, so a relocated
    console-script shebang is never involved) — so ``sys.executable`` is that embedded relocatable
    interpreter and agent spawning needs no special-casing.

    ``CO_STUDIO_PYTHON`` is the escape hatch for the one case where ``sys.executable`` is NOT a
    usable interpreter — e.g. a PyInstaller-frozen build whose ``sys.executable`` is the frozen app
    binary; point it at a sidecar python that can import connectonion.
    """
    return os.environ.get("CO_STUDIO_PYTHON") or sys.executable


STDOUT_LOG_NAME = "studio-stdout.log"   # legacy single log (pre-per-run); still read as a fallback
RUNS_DIR_NAME = "runs"                   # per-run stdout captures: agents/<slug>/runs/<timestamp>.log
PIDFILE_NAME = "studio.pid"
WORKSPACE_DIR_NAME = "workspace"

HEALTH_INTERVAL = 5.0  # seconds between health polls and WS status keepalives
STOP_GRACE = 5.0  # seconds between SIGTERM and SIGKILL
START_GRACE = 45.0  # seconds an agent may stay "starting" before it is "offline"
