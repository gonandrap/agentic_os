"""Repo-root conftest: the test-isolation gate.

This file exists at the rootdir on purpose. pytest loads the rootdir conftest for every
suite collected beneath it — tests/, evals/, tests_browser/, and any suite added later —
so no suite can opt out of isolation by forgetting an import. Moving this into a
per-suite conftest would re-open the hole it closes.

What it prevents: a test run reaching production state or the real user. Workers inherit
`JARVIS_HOME=<production state>`, so on 2026-07-27 a worker's test run wrote the live
central store and the live daemon Telegrammed the user two critical alerts about the
fixture project `proj_a`. `jarvis.testing.gate_test_environment` documents every route.
"""

from __future__ import annotations

from jarvis.testing import (  # noqa: F401 — autouse fixtures, inherited by every suite
    allow_external_sinks,
    claude_json,
    gate_test_environment,
    jarvis_home,
    teardown_test_environment,
)

_AMBIENT_NOTE: str | None = None
_REDIRECTED_LIVE_STATE: str | None = None


def pytest_configure(config):
    """Install the gate before collection, so import-time code is covered too."""
    global _AMBIENT_NOTE, _REDIRECTED_LIVE_STATE
    import os
    from pathlib import Path

    ambient = os.environ.get("JARVIS_HOME")
    root = gate_test_environment()
    _AMBIENT_NOTE = f"jarvis test-isolation gate: JARVIS_HOME -> {root / 'jarvis-home'}"
    if ambient:
        _AMBIENT_NOTE += f" (redirected away from {ambient})"
        # An ambient home with an os.db in it is somebody's live state — a worker session
        # inheriting production, most likely. Worth saying out loud even under `-q`.
        if (Path(ambient).expanduser() / "os.db").exists():
            _REDIRECTED_LIVE_STATE = ambient


def pytest_report_header(config):
    """Say so in the header. A gate nobody can see is a gate nobody maintains."""
    return _AMBIENT_NOTE or ""


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _REDIRECTED_LIVE_STATE:
        terminalreporter.write_line(
            f"test-isolation gate: kept this run out of live state at "
            f"{_REDIRECTED_LIVE_STATE}", yellow=True)


def pytest_unconfigure(config):
    teardown_test_environment()
