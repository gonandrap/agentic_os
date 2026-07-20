# Testing Jarvis OS

## Running the suite

```bash
uv sync --extra dev     # required in a fresh worktree; plain `uv sync` installs no pytest
uv run pytest -q        # testpaths = ["tests"], addopts = "-q" (pyproject.toml:33-35)
```

No test ever invokes the real `claude` binary: `jarvis/testing.py:16` defines a `FAKE_CLAUDE`
script and the `fake_claude` fixture (`:131`) points `JARVIS_CLAUDE_BIN` at it.
`tests/conftest.py:3-10` just re-exports the fixtures from `jarvis/testing.py` — put new
shared fixtures in the package, not in conftest.

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
