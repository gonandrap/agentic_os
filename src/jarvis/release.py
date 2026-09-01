"""Staged releases — the daemon performs the restarts a self-ship must not.

Why this exists: a worker `claude` process used to live inside jarvis.service's cgroup
(systemd's default KillMode=control-group), so a deploy script that restarts the daemon
killed the shipping worker mid-final-turn. The turn lands `is_error` and the work order
settles "failed — review and retry" even though the release fully applied — that is
exactly how wo-2fa7c0e9 shipped v0.5.1 perfectly and reported failure. Post-mortem:
docs/superpowers/specs/2026-08-10-why-a-self-ship-reports-failure.md.

Turns now run in their own transient units (`systemd_units`, issue #133), so a restart
no longer reaches them and this handshake is belt-and-braces rather than the only thing
standing between a deploy and a failed fleet. KEPT ANYWAY, deliberately: it is what makes
the release *verifiable* — the marker is how a rebooted daemon proves the version landed
— and the running-turn guard still holds the restart for the shipping worker on a host
where the transient-unit transport is unavailable and the direct fallback is in use.

So the deploy script gained a `--stage` mode: it performs every release step EXCEPT the
service restarts and the notify, then writes a JSON marker file at
`$JARVIS_HOME/run/pending_release.json`. This module is the daemon's half of the
handshake:

* **every tick** (`maybe_restart`, hooked from `Daemon.release_tick`): when the marker
  is `staged` AND the shipping work order has no running turn — the worker has settled,
  so the restart can no longer kill a report in flight; that guard is the whole point —
  the daemon appends a timeline event, rewrites the marker to `restarting`, restarts
  jarvis-ui.service inline (it never hosts us) and hands jarvis.service to a detached
  transient unit via systemd-run, the same technique the deploy script itself uses
  (PR #85): the restart outlives the daemon it kills.

* **on boot** (`verify_on_boot`, hooked from `Daemon.run_forever`): the new daemon
  proves the release actually applied — the production checkout's pyproject.toml
  version equals the marker's, and ExecMainStartTimestamp of BOTH units is newer than
  the restart. Never `is-active` and never `git describe`: both were true while 0.5.0
  ran half-applied (kn-58429229). On success it settles the work order, queues the
  user-facing notification through the project outbox (outbox → central inbox → every
  sink, Telegram included), and deletes the marker. On failure the marker becomes
  `failed_verification` with the reason and is NEVER deleted automatically — a release
  the OS cannot prove landed is a human's to look at.

Marker lifecycle (state → who writes it):
    staged               the deploy script (--stage)
    restarting           `maybe_restart`, immediately before it touches any unit
    failed_verification  `verify_on_boot`, on any failed check (kept on disk)
    (file deleted)       `verify_on_boot`, on success

Every systemctl/systemd-run call goes through an injectable `SystemdRunner` so tests
never touch real systemd. `check_release_marker` in invariants.py flags a marker stuck
in flight for over an hour.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .paths import production_code_dir, run_dir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_store import ProjectStore

log = logging.getLogger("jarvisd.release")

UI_UNIT = "jarvis-ui.service"
DAEMON_UNIT = "jarvis.service"

#: Where `scripts/install_prod_service.sh` installs the two units. Overridable so the
#: suite can point a check at a rendered unit with no systemd anywhere near it — the
#: same seam the script's own `--unit-dir` gives its tests.
UNIT_DIR_ENV = "JARVIS_SYSTEMD_UNIT_DIR"


def unit_dir() -> Path:
    return Path(os.environ.get(UNIT_DIR_ENV) or "~/.config/systemd/user").expanduser()


def unit_environment(unit: str, name: str) -> str | None:
    """One `Environment=<name>=…` value from an installed unit, or None.

    Reads the FILE, not `systemctl show`: what is on disk is what the next start will
    use, and a stale unit that has not been reloaded is exactly the case worth catching.
    Deliberately tolerant — a unit that is absent (no services installed) or unreadable
    is not this function's problem to report.
    """
    try:
        text = (unit_dir() / unit).read_text(encoding="utf-8")
    except OSError:
        return None
    prefix = f"Environment={name}="
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip('"')
    return None

#: How long a `staged` marker may sit before the BOOT check stops waiting for the
#: reconcile hook and verifies against `staged_at` instead. Generous on purpose: the
#: normal path is minutes (the worker ends its turn, the next tick restarts), so a
#: boot that finds a `staged` marker this old means the restart never happened — the
#: daemon was down, or died between staging and restarting. Verifying then either
#: proves the release landed anyway (someone restarted by hand) or parks the marker
#: as `failed_verification` for a human, which beats waiting forever.
STAGED_BOOT_GRACE = 30 * 60

MARKER_NAME = "pending_release.json"

#: The store lookup the daemon hands in: project name → its ProjectStore, or None.
StoreLookup = Callable[[str], Optional["ProjectStore"]]


def marker_path() -> Path:
    return run_dir() / MARKER_NAME


def read_marker() -> dict[str, Any] | None:
    """The marker as written, or None when absent or unreadable.

    Corrupt JSON reads as None so the restart/verify paths never act on garbage; the
    doctor invariant (`invariants.check_release_marker`) is what reports the file
    itself being wrong.
    """
    try:
        return json.loads(marker_path().read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("release marker unreadable: %s", e)
        return None


def write_marker(marker: dict[str, Any]) -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker, indent=2) + "\n")
    tmp.replace(path)


def delete_marker() -> None:
    marker_path().unlink(missing_ok=True)


class SystemdRunner:
    """Every systemd interaction the release path performs, in one injectable seam.

    Tests hand in a fake; nothing else in this module (or in daemon.py) shells out.
    """

    def _run(self, argv: list[str]) -> str:
        import subprocess

        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            log.warning("%s failed (%s): %s", argv[0], proc.returncode,
                        (proc.stderr or "").strip())
        return proc.stdout or ""

    def restart_unit(self, unit: str) -> None:
        """Inline restart — only ever used for units that cannot host this process."""
        self._run(["systemctl", "--user", "restart", unit])

    def restart_unit_detached(self, unit: str, tag: str) -> None:
        """Restart `unit` from a transient systemd unit OUTSIDE our cgroup.

        Restarting jarvis.service from inside it SIGTERMs the whole cgroup — this
        daemon included — so the restart must outlive us. Same shape the deploy
        script uses (PR #85): `--collect` reaps the transient unit, the short sleep
        lets this process finish its tick and exit cleanly first.
        """
        slug = re.sub(r"[^A-Za-z0-9-]", "-", tag)
        self._run([
            "systemd-run", "--user", "--collect",
            f"--unit=jarvis-release-restart-{slug}",
            f"--description=release {tag}: restart {unit}",
            "/bin/sh", "-c", f"sleep 3; systemctl --user restart {unit}",
        ])

    def unit_start_time(self, unit: str) -> float | None:
        """When the unit's main process started, as a unix epoch — or None.

        Read as ExecMainStartTimestampMonotonic (µs since boot) and converted via
        CLOCK_BOOTTIME rather than parsing the human-readable ExecMainStartTimestamp,
        whose format is locale/timezone prose. Same fact, machine-comparable.
        """
        out = self._run(["systemctl", "--user", "show", unit,
                         "--property=ExecMainStartTimestampMonotonic", "--value"])
        try:
            usec = int(out.strip())
        except ValueError:
            return None
        if usec <= 0:
            return None
        boot_epoch = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
        return boot_epoch + usec / 1e6


# -- the reconcile-tick half ----------------------------------------------------------


def maybe_restart(store_for: StoreLookup, runner: SystemdRunner | None = None,
                  now: float | None = None) -> str | None:
    """Apply a staged release once its shipping worker has settled.

    Returns what happened ("waiting" | "restarting" | None) for logs and tests.
    Called after the daemon's settlement pass, so `running_turns` reflects the turns
    this very tick reaped.
    """
    marker = read_marker()
    if not marker or marker.get("state") != "staged":
        return None
    wo_id = marker.get("wo_id") or ""
    tag = marker.get("tag") or f"jarvis-{marker.get('version')}"
    store = store_for(marker.get("project") or "")
    if store is None:
        # No store means no running-turn guard: restarting would be a guess about a
        # worker we cannot see. Leave the marker; the doctor invariant flags it.
        log.warning("staged release %s names unknown project %r — not restarting",
                    tag, marker.get("project"))
        return None
    if any(t["wo_id"] == wo_id for t in store.running_turns()):
        # The whole point of staging: the shipping worker is still mid-turn, and the
        # restart would kill it exactly the way it killed wo-2fa7c0e9's final report.
        return "waiting"

    store.add_event(wo_id, "release_restart", {
        "version": marker.get("version"), "tag": tag,
        "detail": f"restarting services to apply {tag}",
    })
    marker["state"] = "restarting"
    marker["restart_at"] = now if now is not None else time.time()
    write_marker(marker)  # before any unit moves: the daemon may not survive this

    runner = runner or SystemdRunner()
    runner.restart_unit(UI_UNIT)  # never hosts us; safe inline (kn-58429229 ordering)
    runner.restart_unit_detached(DAEMON_UNIT, tag)
    log.info("release %s: restarted %s, queued detached restart of %s",
             tag, UI_UNIT, DAEMON_UNIT)
    return "restarting"


# -- the boot half --------------------------------------------------------------------


def verify_on_boot(store_for: StoreLookup, runner: SystemdRunner | None = None,
                   now: float | None = None) -> dict[str, Any] | None:
    """Prove a restarting (or long-stranded staged) release actually applied.

    Success: timeline event, the work order settled, the user notified, marker gone.
    Failure: marker parked as `failed_verification` (never deleted), attention raised.
    """
    marker = read_marker()
    if not marker:
        return None
    now = now if now is not None else time.time()
    state = marker.get("state")
    if state == "staged":
        if now - float(marker.get("staged_at") or 0) <= STAGED_BOOT_GRACE:
            return None  # young: the reconcile hook still owns it
        reference = float(marker.get("staged_at") or 0)
    elif state == "restarting":
        reference = float(marker.get("restart_at") or marker.get("staged_at") or 0)
    else:
        return None  # failed_verification: already reported; a human clears it

    wo_id = marker.get("wo_id") or ""
    version = marker.get("version") or ""
    tag = marker.get("tag") or f"jarvis-{version}"
    runner = runner or SystemdRunner()

    problems: list[str] = []
    prod_version = _production_version()
    if prod_version != version:
        problems.append(
            f"production pyproject.toml says {prod_version!r}, expected {version!r}")
    # ExecMainStartTimestamp on BOTH units, newer than the restart. Never `is-active`
    # (both units were active throughout the 0.5.0 half-apply) and never git describe
    # (correct and misleading at once) — kn-58429229's verification rule.
    for unit in (DAEMON_UNIT, UI_UNIT):
        started = runner.unit_start_time(unit)
        if started is None:
            problems.append(f"{unit}: no ExecMainStartTimestamp (unit not running?)")
        elif started <= reference:
            problems.append(
                f"{unit} has not restarted since the release "
                f"(main process started {int(reference - started)}s before it)")

    store = store_for(marker.get("project") or "")
    if problems:
        reason = "; ".join(problems)
        marker.update(state="failed_verification", reason=reason, failed_at=now)
        write_marker(marker)  # kept on disk: never auto-delete a failed release
        if store is not None:
            _report_failure(store, wo_id, tag, reason)
        log.warning("release %s failed verification: %s", tag, reason)
        return {"verified": False, "tag": tag, "wo_id": wo_id, "reason": reason}

    if store is not None:
        store.add_event(wo_id, "release_verified", {
            "version": version, "tag": tag,
            "detail": f"release {tag} verified live",
        })
        settled = _settle(store, wo_id, tag)
        store.add_notification(
            title=f"Shipped {tag} to production",
            body=(f"{tag} verified live: production is on version {version} and both "
                  f"services restarted after the deploy. Work order {wo_id} "
                  f"{settled}."),
            level="info", wo_id=wo_id, source="release",
        )
    else:
        log.warning("release %s verified but project %r has no store — "
                    "work order %s not settled", tag, marker.get("project"), wo_id)
    delete_marker()
    log.info("release %s verified live", tag)
    return {"verified": True, "tag": tag, "wo_id": wo_id}


def _report_failure(store: ProjectStore, wo_id: str, tag: str, reason: str) -> None:
    try:
        store.get_work_order(wo_id)
    except KeyError:
        log.error("release %s names unknown work order %s", tag, wo_id)
        return
    store.add_event(wo_id, "release_verification_failed",
                    {"tag": tag, "reason": reason})
    store.flag_attention(wo_id, f"release {tag} failed verification: {reason}"[:300])
    store.add_notification(
        title=f"release {tag} failed verification",
        body=(f"{reason}\n\nThe marker is kept at {marker_path()} "
              f"(state: failed_verification). Check the units and the production "
              f"checkout, then delete the marker once resolved."),
        level="warning", wo_id=wo_id, source="release",
    )


def _settle(store: ProjectStore, wo_id: str, tag: str) -> str:
    """Move the shipping work order to `completed`, respecting kn-99d3f1d4's traps.

    Returns a phrase for the notification body describing what was done.

    * `completed` — nothing to do beyond clearing any stale flag.
    * `waiting_pr_merge` — left parked: it finished behind a pull request and the
      merge is its real ending; the merge poller closes it (pulling it off the merge
      queue here would complete it before anyone merged).
    * pending assumptions — left in `needs_review`: completing over them would accept
      them silently, the exact back door `wo ack`/`wo done` refuse.
    * anything else (`failed` is the self-ship case, also `needs_review` without
      assumptions, `waiting_input`, `running`) — settled through `ops.close_out`, the
      same "this is over and it went fine" path a merged PR uses, plus the backlog
      close that only ever happens at completion (kn-99d3f1d4 fact 4). `completed`
      is stable against the reconciler: `settle_turns` only re-examines
      running/waiting_input/dispatching.
    """
    from . import ops

    try:
        wo = store.get_work_order(wo_id)
    except KeyError:
        log.error("release %s names unknown work order %s — nothing settled", tag, wo_id)
        return "was not found"
    if wo["status"] == "completed":
        store.clear_attention(wo_id)
        return "was already completed"
    if wo["status"] == "waiting_pr_merge":
        store.clear_attention(wo_id)
        return "stays parked on its pull request"
    if store.pending_assumptions(wo_id):
        return "still has assumptions pending your review"
    ops.close_out(store, wo, "release_completed",
                  why=f"release {tag} verified live",
                  payload={"tag": tag})
    ops.mark_backlog_done(wo)
    return f"completed (was {wo['status']})"


def _production_version() -> str | None:
    """The version the production checkout carries ON DISK.

    Read from pyproject.toml as a file, never from git: `git describe` was correct
    and misleading at once during the 0.5.0 half-apply (kn-58429229).
    """
    try:
        text = (production_code_dir() / "pyproject.toml").read_text()
    except OSError:
        return None
    m = re.search(r'^version *= *"([^"]+)"', text, flags=re.MULTILINE)
    return m.group(1) if m else None
