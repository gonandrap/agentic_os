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

### A release re-verifies nothing. It stops at CI status.

`main` is ready **by assumption**: nothing lands there except through a pull request with
CI green. A release therefore runs **no** tests of its own — not `pytest`, not the LLM
evals, not a quick smoke run, not "just the fast ones to be safe". Shipping is a deploy,
not a second opinion.

The evidence a release stands on is CI's verdict on the exact commits being shipped,
which you **read** rather than produce: `gh pr checks <pr-number>` for each PR in the
release, or the recent runs on `main`. That is the whole verification step, and it costs
seconds. Put those results in the gate request (`--evidence`) — the reviewer sees only
what you write, and CI green on the merged commits is complete evidence for a release.
You will not be asked for more.

If CI is red, or never ran on those commits, **stop and tell the user**. That is not a
cue to run the suite locally and ship on your own say-so: a local green run says nothing
about what CI would have said, and substitutes your judgement for the check the user
actually relies on.

Why this is spelled out: wo-52a6164d shipped 0.5.4 by running the scripted suite, then
the opt-in LLM eval suite, then re-running that suite against the previous tag as an A/B
comparison — 55 minutes and 3.4M tokens to re-establish what the merge had already
established.

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
6. Restarts `jarvis-ui.service` (if installed) and verifies it.
7. Notifies Telegram (best-effort; sources `$PRODUCTION_CODE/secrets/jarvis.env`).
8. Restarts `jarvis.service` **last, detached** into a transient `systemd-run` unit.
   A session Jarvis spawned lives inside that unit's cgroup, so restarting it inline
   SIGTERMs the script mid-deploy — which is how 0.5.0 shipped half-applied. Nothing
   may be added after this step.

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
directory, and the `jarvis-ui.service` status.

The daemon restart is queued (step 8) and lands a few seconds after the script exits —
**and if you were run by Jarvis, it kills this session**, which is by design and not a
failed deploy. Confirm it afterwards from a fresh session:

```bash
systemctl --user status jarvis --no-pager --lines=0   # active, and started just now
```

## Shipping from a work order: use `--stage`

A worker session lives inside `jarvis.service`'s cgroup, so even the detached restart
kills it mid-final-turn and the work order lands as `failed` despite a perfect deploy
(this is exactly what happened to 0.5.1). A Jarvis-dispatched work order must therefore
run the staged mode instead:

```bash
scripts/shipit.sh --stage 1.4.0 --wo <your-wo-id>
```

`--stage` performs every step **except** the service restarts and the Telegram notify,
then writes `$JARVIS_HOME/run/pending_release.json`. From there the OS finishes the
job itself: the daemon restarts the services only after your work order's turn has
settled (so finish the work order promptly after staging), verifies the release on its
next boot (version on disk + fresh start timestamps on both units), settles the work
order as completed, and notifies the user. Do not restart anything yourself and do not
wait around for the restart — stage, `jarvis wo finish`, done. Interactive user
sessions (not spawned by Jarvis) may keep using the classic mode above.

## First-time production setup (once)

If `jarvis.service` isn't installed yet, after the first `shipit` run:

1. Place production secrets at `$PRODUCTION_CODE/secrets/jarvis.env` (systemd
   `KEY=VALUE` format, **no** `export`): `JARVIS_TELEGRAM_TOKEN=…`,
   `JARVIS_TELEGRAM_CHAT_ID=…`. `chmod 600`.
2. `scripts/install_prod_service.sh` — renders `deploy/jarvis.service.template`,
   installs it under `~/.config/systemd/user/`, and enables + starts it with
   `Restart=always` recovery.

See `docs/DEPLOYMENT.md` for the full dev/prod split, service management, and rollback.
