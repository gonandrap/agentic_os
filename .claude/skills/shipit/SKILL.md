---
name: shipit
description: Cut a Jarvis OS release from ALREADY-merged main and deploy it to production. Use when the user wants to ship/release/deploy Jarvis OS, promote dev to prod, or cut a new jarvis-X.Y.Z version. Cuts release/jarvis-X.Y.Z from main, bumps + tags on that branch (main is never committed to), pushes branch and tag to origin, deploys the tag to $PRODUCTION_CODE/jarvis_os, restarts the systemd services, and notifies Telegram.
---

# shipit — release Jarvis OS to production

Jarvis OS runs in two places: the **dev** checkout (`~/workspace/agentic_os`, branch
`main`, run with `uv run jarvis`) where the OS itself is developed, and **production**
(`$PRODUCTION_CODE/jarvis_os`, default `~/workspace/production/jarvis_os`), a checkout
pinned to a release tag and run as a systemd service. `shipit` is the one-way door from
dev to prod so that in-progress dev changes never touch the running fleet.

## The process (mandated)

Releases never bypass code review, and **git is the source of truth**.

**Part A — land the code on `main` first (shipit does NOT do this):** worktree + TDD →
push the branch → PR against `main` → CI green → merge (the user merges, or tells you
to). Then `git pull` so local `main` equals `origin/main`.

**Part B — `scripts/shipit.sh` cuts and deploys the release:**

1. Refuses to run on a dirty tree, without an `origin` remote, or when `HEAD` is not
   exactly `origin/main`; resolves `X.Y.Z` from the latest `jarvis-*` **tag**.
2. Cuts branch `release/jarvis-X.Y.Z` from `main`.
3. Bumps `pyproject.toml` + commits + annotated tag `jarvis-X.Y.Z` **on the release
   branch** — `main` is never modified (done in a throwaway `git worktree`).
4. **Pushes the release branch and the tag to `origin`.**
5. Deploys the tag to `$PRODUCTION_CODE/jarvis_os` from `origin` (clone on first run,
   then `git fetch` + `checkout <tag>` + `uv sync --frozen`), creating a default
   production catalog if none exists.
6. Restarts the production services (`jarvis.service`, `jarvis-ui.service`) if installed.
7. Notifies Telegram (best-effort; sources `$PRODUCTION_CODE/secrets/jarvis.env`).

Production's `origin` is the GitHub remote, so what runs in prod is exactly what is on
the remote — every release is reproducible from a fresh clone. `main`'s `pyproject`
version intentionally lags the shipped one: the bump lives only on release branches.

## How to run it

Pick the version from the user's intent — **ask them to confirm the number if you are
not sure** — then run the script and report the result:

```bash
scripts/shipit.sh                 # patch bump from the latest jarvis-* tag
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
