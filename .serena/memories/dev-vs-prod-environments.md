# Dev vs production: two checkouts of this same repo

Jarvis OS runs in two places. They are the SAME git repository, so this file and the rest
of `.serena/memories/` ship to production with every release tag — which is the point:
prod sessions get the code map for incident root-causing without re-exploring.

| | Development | Production |
|---|---|---|
| Path | `~/workspace/agentic_os` (+ worktrees under `.claude/worktrees/`) | `$PRODUCTION_CODE/jarvis_os`, default `~/workspace/production/jarvis_os` |
| Git state | on a **branch** (`main` or a feature branch) | **detached HEAD at a `jarvis-X.Y.Z` tag** |
| Run as | `uv run jarvis …` | systemd user services `jarvis.service`, `jarvis-ui.service` |
| `JARVIS_HOME` | unset → `~/.jarvis` (`paths.py:18`) | `$PROD_ROOT/state` (`deploy/jarvis.service.template`) |
| Catalog | untracked under `catalogs/` | `$PROD_ROOT/config/catalog.json` |
| Secrets | — | `$PROD_ROOT/secrets/jarvis.env`, mode 600, systemd `KEY=VALUE`, no `export` |

## Telling them apart from inside a session

`git symbolic-ref -q HEAD` succeeds in dev (on a branch) and fails in prod (detached at a
tag). Equivalently `git describe --tags --exact-match` succeeds only in prod. Path check
(`pwd` contains `/production/`) is the quick eyeball version.

## Consequences that matter

- **Never edit code in the production checkout.** It is a tag checkout whose `origin` is
  GitHub; the next `shipit` does `git fetch` + `checkout <tag>` and silently discards local
  edits. It also breaks the invariant that prod == what is on the remote. Fixes go through
  dev → PR → merge → `shipit`.
- **Production state lives outside the checkout** (`$PROD_ROOT/state`), so it survives
  redeploys. Reading prod DBs means pointing at `$PROD_ROOT/state/os.db`, not `~/.jarvis/os.db`.
- `main`'s `pyproject.toml` version intentionally lags the shipped one — the bump lives only
  on `release/jarvis-X.Y.Z` branches. `main` is never committed to.
- Known inconsistency: `src/jarvis/__init__.py:3` `__version__ = "0.1.0"` disagrees with
  `pyproject.toml:3`. Do not trust `__version__` for release identity; use `git describe --tags`.

## Release path

`shipit` skill → `scripts/shipit.sh`: refuses a dirty tree or `HEAD != origin/main`, cuts
`release/jarvis-X.Y.Z` from `main`, bumps + tags on that branch in a throwaway worktree,
pushes branch and tag, deploys the tag into `$PRODUCTION_CODE/jarvis_os` (`git fetch` +
`checkout <tag>` + `uv sync --frozen`), restarts the services, notifies Telegram.
Full detail in `docs/DEPLOYMENT.md`.
