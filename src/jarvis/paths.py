"""Filesystem layout for Jarvis state.

Central state lives under $JARVIS_HOME (default ~/.jarvis):
    os.db            central database (projects, inbox, backlog, knowledge)
    logs/            daemon + notification logs
    run/             daemon pidfile
Per-project state lives under <project>/.jarvis/ (gitignored):
    jarvis.db        work orders, events, messages, notifications, assumptions
"""

from __future__ import annotations

import os
from pathlib import Path


def jarvis_home() -> Path:
    return Path(os.environ.get("JARVIS_HOME", "~/.jarvis")).expanduser()


def central_db_path() -> Path:
    return jarvis_home() / "os.db"


def neo_db_path() -> Path:
    """Neo (the OS answerer agent) keeps its own DB: questions, answers, reviews,
    and the learnings distilled from them."""
    return jarvis_home() / "neo.db"


def logs_dir() -> Path:
    return jarvis_home() / "logs"


def run_dir() -> Path:
    return jarvis_home() / "run"


def daemon_pidfile() -> Path:
    return run_dir() / "jarvisd.pid"


def ensure_home() -> Path:
    home = jarvis_home()
    for d in (home, logs_dir(), run_dir()):
        d.mkdir(parents=True, exist_ok=True)
    return home


def project_state_dir(project_path: Path) -> Path:
    return Path(project_path) / ".jarvis"


def project_db_path(project_path: Path) -> Path:
    return project_state_dir(project_path) / "jarvis.db"
