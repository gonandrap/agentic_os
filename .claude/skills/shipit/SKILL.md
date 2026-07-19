---
name: shipit
description: Cut a Jarvis OS release from main and deploy it to production. Use when the user wants to ship/release/deploy Jarvis OS, promote dev to prod, or cut a new jarvis-X.Y.Z version. Bumps the version, creates the release/jarvis-X.Y.Z branch + jarvis-X.Y.Z tag, deploys the tag to $PRODUCTION_CODE/jarvis_os, and restarts the systemd service.
---

# shipit — release Jarvis OS to production

Jarvis OS runs in two places: the **dev** checkout (`~/workspace/agentic_os`, branch
`main`, run with `uv run jarvis`) where the OS itself is developed, and **production**
(`$PRODUCTION_CODE/jarvis_os`, default `~/workspace/production/jarvis_os`), a checkout
pinned to a release tag and run as a systemd service. `shipit` is the one-way door from
dev to prod so that in-progress dev changes never touch the running fleet.

## What it does

`scripts/shipit.sh` performs, in order:

1. Refuses to run on a dirty tree; resolves the target version `X.Y.Z`.
2. Bumps `pyproject.toml` and commits the release on `main` — **only if** the version
   actually changes.
3. Cuts branch `release/jarvis-X.Y.Z` and annotated tag `jarvis-X.Y.Z`.
4. Deploys that tag to `$PRODUCTION_CODE/jarvis_os` (clones the local repo on first run,
   then `git fetch` + `checkout <tag>` + `uv sync --frozen`), creating a default
   production catalog if none exists.
5. Restarts the production services (`jarvis.service`, `jarvis-ui.service`) if installed.

Production tracks the **local** dev repo (its git `origin` is the local path), so
releases are offline and deterministic. Nothing is pushed to GitHub.

## How to run it

Pick the version from the user's intent, then run the script and report the result:

```bash
scripts/shipit.sh                 # ship pyproject version if untagged, else patch bump
scripts/shipit.sh patch|minor|major
scripts/shipit.sh 1.4.0           # explicit version
scripts/shipit.sh --dry-run       # preview; changes nothing
```

Always run `--dry-run` first if the user is unsure of the version, show them the plan,
then run for real. After shipping, report: the version/tag, the release branch, the prod
directory, and the `jarvis.service` status.

## First-time production setup (once)

If `jarvis.service` isn't installed yet, after the first `shipit` run:

1. Place production secrets at `$PRODUCTION_CODE/secrets/jarvis.env` (systemd
   `KEY=VALUE` format, **no** `export`): `JARVIS_TELEGRAM_TOKEN=…`,
   `JARVIS_TELEGRAM_CHAT_ID=…`. `chmod 600`.
2. `scripts/install_prod_service.sh` — renders `deploy/jarvis.service.template`,
   installs it under `~/.config/systemd/user/`, and enables + starts it with
   `Restart=always` recovery.

See `docs/DEPLOYMENT.md` for the full dev/prod split, service management, and rollback.
