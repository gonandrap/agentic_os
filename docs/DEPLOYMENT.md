# Jarvis OS — deployment & the dev/prod split

Jarvis OS runs in two isolated instances so that developing the OS never disturbs the
running fleet.

- **Production** is the instance you *actually use*: real dev work on your projects and
  prod monitoring of your live projects. The **real fleet catalog lives here.** It runs
  as always-on systemd services.
- **Development** is only for building/testing Jarvis OS itself. Onboard throwaway test
  projects to exercise the flow end-to-end — but **no real project work runs from dev.**

| | **Development** | **Production** |
|---|---|---|
| Purpose | develop/test Jarvis OS itself | run the real fleet (dev work + prod monitoring) |
| Location | `~/workspace/agentic_os` | `$PRODUCTION_CODE/jarvis_os` (default `~/workspace/production/jarvis_os`) |
| Git | branch `main` (trunk) | detached at tag `jarvis-X.Y.Z` |
| Run | `uv run jarvis …` (manual) | `systemctl --user … jarvis` / `jarvis-ui` (services) |
| `JARVIS_HOME` | `~/.jarvis` (default) | `$PRODUCTION_CODE/state` |
| `JARVIS_ENV` | unset | `production` (set by the units) |
| Catalog | `catalogs/gonzalo.json` (empty / test projects) | `$PRODUCTION_CODE/config/catalog.json` (real fleet) |
| Secrets | none | `$PRODUCTION_CODE/secrets/jarvis.env` |
| UI port | 8788 | 8787 |

Both dashboards say which one they are, in the header: an amber **`prod`** badge on
production, a muted **`dev`** one otherwise, each followed by the running version
(hover for the full detail). `JARVIS_ENV` decides when it is set; otherwise the code's
own location does — a checkout under `$PRODUCTION_CODE/jarvis_os` is production — so an
instance deployed before this existed still labels itself correctly. Anything
undetectable reads as `dev`, never as `prod`.

`JARVIS_HOME` is what keeps them apart: separate pidfiles, databases, and logs, so both
daemons can run at once without clashing. The dev catalog and `MIGRATION.md` are
gitignored, so a production clone is automatically free of dev-only config, and no
secrets ever live in git.

## Releasing (dev → prod)

Releasing has two parts, and **shipit only does the second one**.

**1. Land the code on `main` via a reviewed PR.** Work in a worktree, TDD, push the
branch, open a PR against `main`, get CI green, merge. shipit never commits to `main`.

**2. Cut and deploy the release** — from the dev checkout, on `main`, synced with origin:

```bash
scripts/shipit.sh --dry-run        # preview
scripts/shipit.sh                  # patch bump from the latest jarvis-* tag
scripts/shipit.sh minor            # or patch | major
scripts/shipit.sh 1.4.0            # explicit version
```

shipit cuts `release/jarvis-X.Y.Z` from `main`, bumps `pyproject.toml` + commits +
tags `jarvis-X.Y.Z` **on that release branch** (via a throwaway worktree, so `main` is
never modified), **pushes the branch and the tag to `origin`**, deploys the tag to
`$PRODUCTION_CODE/jarvis_os` (`git fetch` + `checkout` + `uv sync --frozen`), restarts
the services, and notifies Telegram.

**Git is the source of truth.** shipit refuses to run unless `HEAD` is exactly
`origin/main`, and it pushes every release ref before deploying — production's `origin`
is the GitHub remote, so what runs in prod is exactly what is on the remote and any
release is reproducible from a fresh clone.

Version numbering comes from the latest `jarvis-*` **tag**, not from `pyproject.toml`
on `main` — the bump lives only on release branches, so `main`'s version string
intentionally lags behind the shipped one.

> Git note: there is no bare `release` branch. Git cannot hold both a ref named
> `release` and refs named `release/…` (a file/directory conflict), so the release line
> is the versioned `release/jarvis-X.Y.Z` branches plus `jarvis-X.Y.Z` tags.

### Staged releases (`--stage`) — shipping from a work order

A worker `claude` process lives inside `jarvis.service`'s cgroup, so a release run
from a Jarvis-dispatched work order kills its own worker when the daemon restarts:
the final turn dies mid-report, and the work order settles `failed` even though the
release fully applied (that is exactly how v0.5.1 shipped — see
`docs/superpowers/specs/2026-08-10-why-a-self-ship-reports-failure.md`). A self-ship
therefore **stages** instead of restarting:

```bash
scripts/shipit.sh --stage 1.4.0 --wo wo-abc12345
```

This performs every release step — preconditions, release branch, bump + tag, push,
deploy of the tag to `$PRODUCTION_CODE/jarvis_os`, `uv sync` — **except** the service
restarts and the Telegram notify, then writes a marker file
`$JARVIS_HOME/run/pending_release.json`:

```json
{"wo_id": "wo-abc12345", "project": "jarvis_os", "version": "1.4.0",
 "tag": "jarvis-1.4.0", "staged_at": 1786500000, "state": "staged"}
```

The daemon finishes the job (`src/jarvis/release.py`):

1. **Restart, once the worker has settled.** Every reconcile tick the daemon checks
   the marker; when it is `staged` and the named work order has **no running turn**
   — i.e. the shipping worker has finished reporting — it appends a timeline event,
   rewrites the marker to `restarting`, restarts `jarvis-ui.service` inline and hands
   `jarvis.service` to a detached `systemd-run` unit (the restart outlives the daemon
   it kills).
2. **Verify on boot.** The new daemon, before its first tick, checks that the
   production checkout's `pyproject.toml` version equals the marker's **and** that
   `ExecMainStartTimestamp` of *both* units is newer than the restart (never
   `is-active`, never `git describe` — both lie during a half-apply). On success it
   settles the work order as `completed`, sends the release notification through the
   normal inbox → sinks path, and deletes the marker. On failure the marker becomes
   `state: "failed_verification"` with the reason, the work order gets an attention
   flag and a warning notification, and the marker is **never deleted automatically**
   — resolve it by hand, then remove the file.

A marker stuck in `staged`/`restarting` for over an hour is flagged by
`jarvis doctor` (`INV-RELEASE-MARKER-STALE`). A plain `scripts/shipit.sh` run without
`--stage` behaves exactly as described above: restarts and notify included — use that
from your own shell.

## First-time production setup

```bash
scripts/shipit.sh                                   # 1. create + populate the prod checkout
mkdir -p "$PRODUCTION_CODE/secrets"                 # 2. place secrets (KEY=VALUE, no export)
printf 'JARVIS_TELEGRAM_TOKEN=…\nJARVIS_TELEGRAM_CHAT_ID=…\n' > "$PRODUCTION_CODE/secrets/jarvis.env"
chmod 600 "$PRODUCTION_CODE/secrets/jarvis.env"
scripts/install_prod_service.sh                     # 3. install + enable + start the service
```

Start-on-boot needs user lingering (survives logout/reboot):

```bash
sudo loginctl enable-linger "$USER"        # already enabled on this host
```

## Managing the production services

Production runs **two** units: `jarvis.service` (the orchestrator daemon) and
`jarvis-ui.service` (the dashboard at http://127.0.0.1:8787).

```bash
systemctl --user status  jarvis jarvis-ui   # health + recent logs
systemctl --user restart jarvis jarvis-ui
systemctl --user stop    jarvis jarvis-ui
journalctl --user -u jarvis -f              # follow daemon logs
journalctl --user -u jarvis-ui -f           # follow UI logs
```

`Restart=always` + `RestartSec=5` + `StartLimitIntervalSec=0` means each is brought
back up whenever it exits, indefinitely (recovery).

### Restarting `jarvis.service` from a session Jarvis spawned

A Claude session that the daemon started lives **inside `jarvis.service`'s cgroup**, so
`systemctl --user restart jarvis.service` kills that session along with whatever script
it was running. This is what left 0.5.0 half-applied: the deploy script restarted the
daemon, died, and never reached `jarvis-ui.service`.

`scripts/shipit.sh` and `scripts/install_prod_service.sh` both handle it the same way —
the UI restarts inline, the daemon restarts **last**, detached into a transient unit:

```bash
systemd-run --user --collect --unit=jarvis-restart \
  /bin/sh -c 'sleep 3; systemctl --user restart jarvis.service'
```

Do the same by hand if you are restarting the daemon from inside one of its own
sessions; a plain `systemctl --user restart jarvis` is only safe from your own shell.

## Rollback

Redeploy a previous tag, or point production back and restart:

```bash
git -C "$PRODUCTION_CODE/jarvis_os" checkout -f jarvis-<older>
(cd "$PRODUCTION_CODE/jarvis_os" && uv sync --frozen --extra ui)
systemctl --user restart jarvis jarvis-ui
```
