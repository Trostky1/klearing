"""
Where Klearing keeps its data.

A packaged app must never write next to its own executable (Program
Files and .app bundles aren't writable, and onedir folders get
replaced on upgrade), so everything lives in the OS's per-user app
data directory:

  Windows:  %LOCALAPPDATA%\\Klearing
  macOS:    ~/Library/Application Support/Klearing
  Linux:    ~/.local/share/klearing

KLEARING_DATA_DIR overrides the location (used by tests and by anyone
who wants their data somewhere specific, e.g. a synced folder).
"""

import os
import sys
from pathlib import Path

from config import APP_ID, APP_NAME


def is_frozen():
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def data_dir():
    override = os.environ.get("KLEARING_DATA_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path():
    return data_dir() / "klearing.db"


def log_path(name):
    """A named log file inside the data dir, e.g. log_path('app') ->
    .../Klearing/app.log."""
    return data_dir() / f"{name}.log"


LOG_NAMES = ("app", "daily", "weekly", "error")


def resource_path(relative):
    """A bundled read-only asset (e.g. the favicon): inside the
    PyInstaller bundle when frozen, next to the source in dev."""
    if is_frozen():
        return Path(sys._MEIPASS) / relative
    return Path(__file__).parent / relative


def app_command(*args):
    """
    The command line that re-invokes this app with the given args —
    what scheduled tasks should run. Frozen: the bundled executable.
    Dev: the current Python plus main.py.
    """
    if is_frozen():
        return [sys.executable, *args]
    main_py = str(Path(__file__).parent / "main.py")
    return [sys.executable, main_py, *args]
