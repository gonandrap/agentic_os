# Testing Jarvis OS

## Running the suite

```bash
uv sync --extra dev     # required in a fresh worktree; plain `uv sync` installs no pytest
uv run pytest -q        # testpaths = ["tests"], addopts = "-q" (pyproject.toml:33-35)
```

`tests/conftest.py` just re-exports the fixtures from `jarvis/testing.py` — put new
shared fixtures in the package, not in conftest.

## The test-isolation gate — a test run cannot reach production

`conftest.py` **at the repo root** is the gate. pytest loads the rootdir conftest for every
suite beneath it (`tests/`, `evals/`, `tests_browser/`, anything added later), so isolation
is structural: a new suite cannot opt out by forgetting an import. Its `pytest_configure`
calls `jarvis.testing.gate_test_environment()` *before collection*, which redirects:

| Route | Gated to | Escape hatch |
|---|---|---|
| `JARVIS_HOME` (central `os.db`, `neo.db`, logs, pidfile) | throwaway `/tmp/jarvis-test-gate-*` | — |
| Telegram + desktop sinks | refuse to fire (`JARVIS_DISABLE_EXTERNAL_SINKS`, checked in `notify.sink_telegram`/`sink_desktop`) | `allow_external_sinks` fixture |
| `gh` (`jarvis bug report` → **public** issue tracker) | a stub that exits 1 (`JARVIS_GH_BIN`) | `fake_gh` fixture |
| real `claude` (spawns real agents, bills real tokens) | a stub that exits 1 (`JARVIS_CLAUDE_BIN`) | `fake_claude` fixture, or `JARVIS_EVALS_LLM=1` |

On top of that floor, `jarvis_home` is an **autouse** fixture, so every test gets its own
home under its own `tmp_path` — forgetful tests are isolated *and* cannot collide with each
other. Naming `jarvis_home` as a parameter is still how you get the path.

This replaced the session-scoped `isolate_jarvis_home` fixture from #30. That fixture was
session-scoped because function-scoped monkeypatch teardown restores the *pre-test* value,
handing the real home back to anything still running (a daemon thread outliving its test, a
subprocess mid-flight). The property survives: the gate overwrites `os.environ` directly at
`pytest_configure` — not via monkeypatch, so nothing undoes it — and teardown therefore
falls back onto the sandbox, never onto production.

Why this exists: a worker session inherits `JARVIS_HOME=~/workspace/production/state`, so
before the gate any test that touched central state wrote **live** state, and the live
daemon then routed the central inbox to the real sinks. On 2026-07-27 that Telegrammed the
user two critical alerts about the fixture project `proj_a`, one with a deep link that
500'd. `tests/test_isolation_gate.py` (21 tests) proves each route is shut, including an
end-to-end subprocess run of a known-leaky test against a poisoned `JARVIS_HOME`.

Recognising a leak: an inbox item for project **`proj_a`** (the fixture project name, never
registered in production), or a notification quoting a fake-claude stub string.
When `-q` hides the header, the gate still prints a yellow terminal-summary line whenever it
redirected away from a home that actually had an `os.db` in it.

## Coverage map

| Test file | Covers |
|---|---|
| `tests/test_bootstrap.py` (9) | `bootstrap.py` — `bootstrap_project`, `build_settings`, `deep_merge`, `settings_drift` |
| `tests/test_catalog.py` (8) | `catalog.py` — `load_catalog`, `parse_catalog`, `CatalogError` |
| `tests/test_stores.py` (9) | `central_store.py` + `project_store.py`, incl. the WO status machine |
| `tests/test_timeline.py` (12) | `timeline.py` — `build_timeline`, `event_level` |
| `tests/test_notify.py` (6) | `notify.py` + catalog UI config |
| `tests/test_neo.py` (13) | `neo.py`, `neo_store.py`, and their `ops`/`daemon` integration |
| `tests/test_pipeline.py` (25) | end-to-end: `ops`, `daemon`, `dispatch`, `hooks`, `claude_cli` (fake), stores |
| `tests/test_wo_hide_delete.py` (13) | `ops.hide/delete_work_order` + `cli` + cascade across all three stores |
| `tests/test_ui.py` (18) | `ui/app.py` via `TestClient`, actions routed through `ops` |
| `tests/test_shipit.py` (9) | `scripts/shipit.sh` (shell, not a Python module) |
| `tests/test_isolation_gate.py` (21) | the gate above: `JARVIS_HOME`, both external sinks, `gh`, `claude` |
| `tests/test_state_isolation.py` (3) | the same invariant from the store side — `CentralStore`/`NeoStore` resolve their paths at construction, so the guard has to be in the environment |

Thin spots: no dedicated tests for `paths.py`, `db.py`, `claude_cli.py` (only exercised
through the fake), or `cli.py` (only via `test_wo_hide_delete.py`).

## LLM-graded persona evals — read before editing CLAUDE.md

`evals/llm/test_jarvis_judgment.py` loads `CLAUDE.md` (`:24`, `PERSONA_PATH`) as a bare
**system prompt** and grades whether the persona routes work through the CLI. Opt-in:

```bash
JARVIS_EVALS_LLM=1 uv run pytest evals/llm -q     # costs real model calls
```

Critical constraint: the eval supplies **no cwd, no git context, no repo** — only the file's
text. So `CLAUDE.md` must still read as the *operator* persona when the environment is
undetermined. Any dev-mode behavior must be a scoped override further down the file, never a
top-level fork, or all 14 routing scenarios regress.

`tests_browser/` holds Playwright UI tests (separate from the default `testpaths`).
