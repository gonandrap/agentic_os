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

A worker `claude` process used to live inside `jarvis.service`'s cgroup, so a release
run from a Jarvis-dispatched work order killed its own worker when the daemon restarted:
the final turn died mid-report, and the work order settled `failed` even though the
release fully applied (that is exactly how v0.5.1 shipped — see
`docs/superpowers/specs/2026-08-10-why-a-self-ship-reports-failure.md`). Turns now run in
transient units of their own (see *Worker turns run outside the daemon's cgroup* below),
so that no longer happens — but a self-ship still **stages** rather than restarting,
because staging is also what makes the release verifiable and what protects a host that
has fallen back to the direct transport:

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

### The unit's PATH is the whole fleet's PATH

`Environment=PATH=` in `jarvis.service` is the only PATH the daemon has, and every
worker it spawns inherits it. A `systemd --user` service gets none of a login shell's
PATH, so a tool you can run by hand may still be invisible to the fleet — and the
symptom is not an error, it is a feature that quietly never happens. Issue #90:
`gh` is a snap at `/snap/bin/gh`, `/snap/bin` was not on the unit's PATH, and
auto-complete-on-merge was off for every project for a release.

`scripts/install_prod_service.sh` builds that PATH — prod venv first, then the
directories of `uv`, `claude`, `node` and `gh` as it finds them, then a fixed fallback
list (`/snap/bin`, `~/.local/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`). It prints
whether the result can resolve `gh` and says so loudly when it cannot. Re-run it
whenever a tool the fleet needs moves or is newly installed:

```bash
scripts/install_prod_service.sh --dry-run --unit-dir /tmp/units   # see the PATH first
systemctl --user show jarvis.service -p Environment               # what is live now
```

If a tool lives somewhere the script does not look, add its directory to that fallback
list — and to `GH_SEARCH_DIRS` in `src/jarvis/bugreport.py`, its mirror, which is what
lets an already-installed daemon find `gh` without being re-installed.

**Three things keep that PATH applied**, because writing the fix into the script was not
enough on its own: the units are installed by hand, once, so the #90 fix sat rendered
but unapplied from 2026-07-19 until 2026-09-01 while every worker kept getting a bash
with no `gh`.

| | What it does | When |
|---|---|---|
| `scripts/shipit.sh` step 5a | re-renders both units from the tag being deployed (`install_prod_service.sh --no-restart`; the restarts stay with shipit, which owns their order) | every release, staged or not |
| `bugreport.heal_path` | appends the missing `GH_SEARCH_DIRS` to the daemon's own `os.environ["PATH"]` at start-up, which every worker then inherits — appends, never prepends, so nothing shadows the prod venv | every daemon start |
| `INV-SERVICE-PATH` | reports an installed unit whose PATH cannot reach `gh`, reading the file rather than the healed process | `jarvis doctor` |

### Worker turns run outside the daemon's cgroup

`systemd --user` defaults to `KillMode=control-group`, so anything left in
`jarvis.service`'s cgroup dies with the daemon on every restart — a deploy, a
crash-restart, `jarvis stop`. Detaching a turn (`start_new_session=True`) does not help:
that leaves the process *group*, not the cgroup. On the jarvis-0.6.2 release this killed
a feature order's planner, which then sat dead for nine hours (issue #133).

Each worker turn therefore gets a transient unit of its own:

```
jarvis-turn-<wo-id>-<seq>.service
```

started with `systemd-run --user --collect`, so it lives under `app.slice` beside
`jarvis.service` rather than inside it. Restarting the daemon does not touch a running
turn; the turn writes its result file, and the **new** daemon reaps it on its first tick.
`--collect` means a finished unit unloads itself, so `systemctl --user list-units
'jarvis-turn-*'` showing nothing is the normal state, not a fault.

The transport is auto-detected per spawn — `systemd-run` on `PATH`, `XDG_RUNTIME_DIR`
set, and the daemon itself in a `.service` cgroup. A dev checkout, `jarvis start
--foreground` from a shell and any host without systemd keep the plain detached-`Popen`
path, and a `systemd-run` that fails falls back to it rather than stalling dispatch.
`JARVIS_TURN_TRANSPORT=systemd|direct|auto` overrides the detection.

> A transient unit inherits the **systemd user manager's** environment, not the daemon's.
> Everything a worker needs — `JARVIS_HOME`, `JARVIS_ENV`, the `Environment=PATH=` that
> puts `gh` within reach (#90) — is forwarded explicitly with `--setenv`. If a worker
> suddenly cannot see something the daemon can, that forwarding is the first place to
> look.

### Restarting `jarvis.service` from a session Jarvis spawned

A Claude session that the daemon started used to live **inside `jarvis.service`'s
cgroup**, so `systemctl --user restart jarvis.service` killed that session along with
whatever script it was running. This is what left 0.5.0 half-applied: the deploy script
restarted the daemon, died, and never reached `jarvis-ui.service`. Worker turns now sit
in their own units and are safe, but keep doing the below — it costs nothing, and it
still covers a host on the direct fallback.

`scripts/shipit.sh` and `scripts/install_prod_service.sh` both handle it the same way —
the UI restarts inline, the daemon restarts **last**, detached into a transient unit:

```bash
systemd-run --user --collect --unit=jarvis-restart \
  /bin/sh -c 'sleep 3; systemctl --user restart jarvis.service'
```

Do the same by hand if you are restarting the daemon from inside one of its own
sessions; a plain `systemctl --user restart jarvis` is only safe from your own shell.

### You should not need `journalctl` to find out something broke

The journal is for following along live. Everything the OS needs you to *know* it
mirrors into `$JARVIS_HOME/logs/`, because the journal is invisible to `jarvis`
commands and to every agent working in the fleet:

| File | What |
|---|---|
| `jarvisd.log` | the daemon |
| `notifications.log` | every notification that went out, and any deep link withheld because it would have dead-ended |
| `ui.log` | one entry per unhandled dashboard error, with traceback |
| `ui-access.log` | one line per dashboard request (the auto-refresh poll is skipped unless it fails) |

`ui.log` is *read back*, not just written: the daemon raises a `jarvis inbox` item —
and so a Telegram alert — for each new dashboard error, `jarvis status` counts recent
ones as an attention item, and `jarvis doctor` reports them as `INV-UI-HEALTHY`. A 500
on the dashboard reaches you the same way a daemon problem does. Both UI logs rotate at
512 KiB into a single `.1` sibling, so a crash loop cannot fill the state directory.

## Rollback

Redeploy a previous tag, or point production back and restart:

```bash
git -C "$PRODUCTION_CODE/jarvis_os" checkout -f jarvis-<older>
(cd "$PRODUCTION_CODE/jarvis_os" && uv sync --frozen --extra ui)
systemctl --user restart jarvis jarvis-ui
```
