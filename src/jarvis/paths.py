"""Filesystem layout for Jarvis state.

Central state lives under $JARVIS_HOME (default ~/.jarvis):
    os.db            central database (projects, inbox, backlog, knowledge)
    logs/            jarvisd.log, notifications.log, ui.log, ui-access.log
    run/             daemon pidfile
Per-project state lives under <project>/.jarvis/ (gitignored):
    jarvis.db        work orders, events, messages, notifications, assumptions

This module also answers *which instance is running* (`deployment_env`) — production
and development are two checkouts of the same code, told apart by where they live.
"""

from __future__ import annotations

import os
from pathlib import Path


PRODUCTION = "production"
DEVELOPMENT = "development"

#: Set by the production systemd units (deploy/*.service.template). Overrides detection.
ENV_OVERRIDE = "JARVIS_ENV"
#: Production's root, and its default — the same variable the deploy scripts use.
PRODUCTION_ROOT_ENV = "PRODUCTION_CODE"
DEFAULT_PRODUCTION_ROOT = "~/workspace/production"


def jarvis_home() -> Path:
    return Path(os.environ.get("JARVIS_HOME", "~/.jarvis")).expanduser()


def production_code_dir() -> Path:
    """The checkout production runs from, per docs/DEPLOYMENT.md."""
    root = os.environ.get(PRODUCTION_ROOT_ENV) or DEFAULT_PRODUCTION_ROOT
    return (Path(root).expanduser() / "jarvis_os")


def deployment_env() -> tuple[str, str]:
    """Which instance this process is: `(PRODUCTION | DEVELOPMENT, how we know)`.

    The dashboard says this out loud, so nobody drives the live fleet believing they
    are in a dev sandbox. Two signals, in order:

    1. ``JARVIS_ENV`` — the explicit knob the production units export.
    2. otherwise the code's own location: production is the checkout at
       ``$PRODUCTION_CODE/jarvis_os``. This is what labels an already-deployed
       production instance correctly without redeploying it.

    Never raises, and never *guesses* production: an undetectable environment is
    development, so a check that breaks downgrades the badge instead of dressing a dev
    checkout up as the live fleet. The second element is human-readable detail for the
    badge's tooltip — it exists so a wrong label is diagnosable without reading code.
    """
    try:
        declared = os.environ.get(ENV_OVERRIDE, "").strip()
        if declared:
            env = PRODUCTION if declared == PRODUCTION else DEVELOPMENT
            return env, f"{ENV_OVERRIDE}={declared}"
        code = Path(__file__).resolve().parent
        env = PRODUCTION if code.is_relative_to(production_code_dir().resolve()) \
            else DEVELOPMENT
        return env, f"code at {code}"
    except Exception:  # noqa: BLE001 — a badge must never be why a page 500s
        return DEVELOPMENT, "environment could not be determined"


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
