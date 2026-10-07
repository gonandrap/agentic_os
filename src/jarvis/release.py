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
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .paths import production_code_dir, run_dir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_store import ProjectStore

log = logging.getLogger("jarvisd.release")

UI_UNIT = "jarvis-ui.service"
DAEMON_UNIT = "jarvis.service"

#: The key under a release work order's `metadata` that says "this order exists to ship
#: fixes, and these are the ones it is shipping". HERE rather than on `Daemon`, whose
#: `RELEASE_BATCH_KEY` is now an alias: `ops` needs the predicate and importing `daemon`
#: at module level would be a cycle (2026-09-29 spec §1).
#: SECOND READER: `ProjectStore.settled_release_orders` spells this value out as the SQL
#: path `'$.release_for_issues'`, so changing it here means changing that query too.
BATCH_KEY = "release_for_issues"

#: Said once per BROKEN COMMIT when a release that delivered no release is re-parked to
#: wait for the base to go green (2026-09-29 spec §1).
RED_DEFER_EVENT = "release_deferred_red_base"

#: Said ONCE PER EPISODE when that wait runs past `Daemon.RED_PARK_AFTER_SECONDS` and the
#: release asks the user instead (§3).
RED_PARK_EVENT = "release_park_red_base"


def is_release_order(wo: dict[str, Any]) -> bool:
    """Is this work order a release the OS filed? The batch in `metadata` is the test."""
    from . import db

    meta = db.from_json(wo.get("metadata"), {}) or {}
    return isinstance(meta.get(BATCH_KEY), list)

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
        settled = settle(store, wo_id, tag)
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


def settle(store: ProjectStore, wo_id: str, tag: str, why: str | None = None) -> str:
    """Move the shipping work order to `completed`, respecting kn-99d3f1d4's traps.

    Returns a phrase for the notification body describing what was done.

    `why` is which ENDING this was, and there is one function rather than two because
    every trap below exists because it was a bug once; the first divergence a sibling
    would grow is a release order completed over an assumption nobody answered (spec §5).
    It defaults to the verify-on-boot wording, which is that path's, unchanged.

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

    why = why or f"release {tag} verified live"
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
    ops.close_out(store, wo, "release_completed", why=why,
                  payload={"tag": tag, "why": why})
    ops.mark_backlog_done(wo)
    return f"completed (was {wo['status']})"


#: Seconds of slack allowed on the tag-postdates-approval check — see its call site.
TAG_CLOCK_SLACK = 1.0


def _tag_created_at(root: Path, tag: str) -> float | None:
    """When the ANNOTATED tag object was made, or None if it cannot be read as one.

    `taggerdate` and not `creatordate`: the latter falls back to the commit's date for a
    lightweight tag, so a plain `git tag <name>` on an old commit would answer with that
    commit's age and read as a release that happened long ago. A release always makes an
    annotated tag; anything else here is "cannot tell", which is a refusal.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "--format=%(taggerdate:unix)",
             f"refs/tags/{tag}"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = out.stdout.strip()
    if out.returncode != 0 or not stamp:
        return None
    try:
        return float(stamp)
    except ValueError:
        return None


#: Every tag a release of this repository cuts. HARDCODED, not a setting (spec §3): the
#: whole release path — `scripts/shipit.sh`, `_production_ref`'s `jarvis-X.Y.Z` rule,
#: `DAEMON_UNIT` — is already this repository's, and a knob would imply the rest
#: generalises.
RELEASE_TAG_GLOB = "jarvis-*"


def _tag_version(tag: str) -> tuple[int, Any]:
    """Sort key for a release tag: `(major, minor, patch)`, lexical for anything else."""
    parts = tag.removeprefix("jarvis-").split(".")
    try:
        return (0, tuple(int(p) for p in parts))
    except ValueError:
        return (1, tag)


def tags_containing(root: Path, shas: Sequence[str]) -> tuple[list[str], str]:
    """The `jarvis-*` tags that carry EVERY one of `shas`, lowest version first.

    `git tag --contains X` is ancestry — it lists the tags whose commit is a descendant
    of X — and it is the only correct reading: release tags are descendants of `main`,
    never ancestors (kn-179cd767), so "main is ahead of the tag" decides nothing
    (kn-4f5aaa2b measured main 5 commits ahead while the fix was unshipped) and "the tag
    list has not changed" answers a different question.

    ONE INVOCATION PER SHA, intersected here: `git tag --contains A --contains B` is a
    UNION, so a single call would report a tag carrying half a batch as carrying all of
    it. A batch is one or two commits.

    Returns `(tags, error)`. A non-empty error is "cannot tell" and every caller reads it
    as a refusal, never as "no tags" — `_tag_created_at`'s direction. Lowest version
    first because the first release that carried the whole batch is the honest answer,
    and it is deterministic across ticks.
    """
    if not shas:
        return [], "no payload commits to look for"
    # FIRST, and best-effort: a tag another release path cut is not in this checkout
    # until it is fetched, and the defect's window is minutes long — so a stale tag list
    # is the normal state. A failed fetch can only make a tag unseen, which leaves the
    # work order open (spec §3A).
    try:
        fetched = subprocess.run(
            ["git", "-C", str(root), "fetch", "--tags", "--quiet", "--force", "origin"],
            capture_output=True, text=True, timeout=60, check=False)
        if fetched.returncode != 0:
            log.debug("could not fetch tags in %s: %s", root,
                      (fetched.stderr or "").strip()[:200])
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("could not fetch tags in %s: %s", root, e)

    common: set[str] | None = None
    for sha in shas:
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "tag", "--list", RELEASE_TAG_GLOB,
                 "--contains", sha],
                capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            return [], f"git could not be run in {root}: {type(e).__name__}: {e}"
        names = set(out.stdout.split())
        if out.returncode != 0 or (not names and out.stderr.strip()):
            detail = (out.stderr or out.stdout).strip().replace("\n", " ")[:200]
            return [], f"`git tag --contains {sha}` failed: {detail}"
        common = names if common is None else common & names
    return sorted(common or set(), key=_tag_version), ""


@dataclass(frozen=True)
class Overtaken:
    """Has another release already carried a batch, and does the fleet RUN it.

    Built by `overtaken_by`. `error` is a refusal — "cannot tell" — and is never the same
    claim as `tag == ""`, which is the ordinary "no release carries this yet".
    """

    #: The lowest `jarvis-*` tag containing the whole batch; `""` when none does.
    tag: str = ""
    #: Every containing tag, lowest version first.
    tags: tuple[str, ...] = ()
    #: `_production_ref`'s answer, when it could be read.
    deployed: str = ""
    #: Production is at one of `tags`, with the file and git agreeing.
    live: bool = False
    #: Why nothing can be concluded; empty when something can.
    error: str = ""


def overtaken_by(project_root: Path, shas: Sequence[str]) -> Overtaken:
    """Did another release already ship this payload, and is production running it.

    The two halves run in different checkouts (spec §3): ancestry in the PROJECT's own
    clone, where the tags and the objects are, and the deploy question in the PRODUCTION
    checkout, read-only.

    The production test is MEMBERSHIP in `tags`, deliberately not ancestry re-run there:
    `git merge-base --is-ancestor` needs the payload object present, which a deploy clone
    does not guarantee, and making it reliable would need a fetch into a checkout that is
    a release tag and must never be written. Note what membership correctly refuses — a
    later tag cut from an older base does not contain the fix, so production running it
    leaves the order open.

    BOTH READINGS of what production is, the file and git, on kn-58429229's rule and for
    its reason: during the 0.5.0 half-apply the tag was checked out and the running code
    was not it, so a check that reads one of the two is the check that was already fooled
    once.
    """
    tags, error = tags_containing(project_root, shas)
    if error:
        return Overtaken(error=error)
    if not tags:
        return Overtaken()
    found = {"tag": tags[0], "tags": tuple(tags)}
    root = production_code_dir()
    if not root.is_dir():
        return Overtaken(**found, error=f"no production checkout at {root}")
    ref = _production_ref(root)
    if ref == "HEAD":
        return Overtaken(**found,
                         error="the production checkout is not at a release tag")
    version = _production_version()
    if version is None:
        return Overtaken(**found, deployed=ref,
                         error="the production checkout's version could not be read")
    if ref.removeprefix("jarvis-") != version:
        return Overtaken(**found, deployed=ref,
                         error=f"production is at {ref} and says version {version}")
    return Overtaken(**found, deployed=ref, live=ref in tags)


def verify_release_claim(store: ProjectStore, wo_id: str, version: str, tag: str) -> str:
    """Did THIS WORK ORDER ship this release? `""` when it checks out, else why not.

    THE PROVENANCE CHECK BEHIND `attested`, and the reason the validation panel may skip
    a release round at all (spec
    docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §4).

    Three checks, and each one closes a hole the previous draft left open.

    1. **Production is on exactly this version AND at exactly this tag.** The marker and
       the timeline both say what a release DID, and neither is proof of it: a marker is
       a JSON file under `$JARVIS_HOME/run/` and an event is a row, so a work order that
       delivered nothing could write either (review round 1). The production checkout is
       state no submitting worker owns. BOTH readings, on kn-58429229's rule and for its
       reason — the version from the FILE and the ref from GIT answered differently
       during the 0.5.0 half-apply, so a check that reads one of them is the check that
       was already fooled once.

    2. **This work order holds an APPROVED `release` gate that it actually used.** Check
       1 alone proves that *a* release landed and stays true until the next one, so a
       work order that delivered nothing could name the version production was ALREADY on
       and replay it at no cost (review round 2). An `approvals` row reaching `approved`
       is written by `ProjectStore.decide_approval` on a verdict from Neo or the user —
       a worker cannot author one for itself — and `uses > 0` means the gate actually
       opened for it. `dismissed` deliberately does not count: a dismissal records that
       the recogniser was wrong and no authorisation at all.

    3. **The tag was created after that authorisation.** Otherwise an order approved to
       ship could still be credited with a tag that already existed when it was approved.

    Never raises, and every "cannot tell" is a failure with a sentence: the caller turns
    any non-empty return into an effect that is NOT attested, which sends the round to
    the panel. A fleet with no production checkout, or one that does not gate releases,
    therefore JUDGES its releases rather than voiding them — the safe direction, and the
    reason none of this can fail open.

    WHAT IS STILL NOT PROVEN, said out loud because a reader will otherwise assume it is:
    nothing in the production checkout names a work order, so these checks bind the order
    to an AUTHORISATION and a WINDOW rather than to the deploy itself. An order holding
    its own approved release gate, whose tag postdates that approval, is as close as the
    recorded state gets.
    """
    if not version or not tag:
        return "the release claim names no version or no tag"
    live = _production_version()
    if live is None:
        return "the production checkout's version could not be read"
    if live != version:
        return f"production is on {live}, not {version}"
    root = production_code_dir()
    ref = _production_ref(root)
    if ref != tag:
        return f"the production checkout is at {ref}, not {tag}"

    grants = [a for a in store.list_approvals(wo_id=wo_id, statuses=("approved",))
              if a["kind"] == "release" and int(a["uses"] or 0) > 0]
    if not grants:
        return (f"{wo_id} holds no approved release gate it used, so nothing ties this "
                f"order to the release production is running")
    authorised = min(float(g["decided_at"] or g["ts"]) for g in grants)
    made = _tag_created_at(root, tag)
    if made is None:
        return f"{tag} is not an annotated tag in the production checkout"
    # `+ TAG_CLOCK_SLACK`: a tag's date has one-second resolution and the approval's does
    # not, so a tag cut in the same second it was approved reads as fractionally older.
    # The slack is spent on the honest side — a replay has to beat the approval by a
    # whole second, and a real release is minutes of scripted work after it.
    if made + TAG_CLOCK_SLACK < authorised:
        return (f"{tag} already existed when {wo_id} was authorised to release, so this "
                f"order did not cut it")
    return ""


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


@dataclass(frozen=True)
class ProductionStatus:
    """What the production checkout is, as git sees it. Built by `production_status`."""

    #: Tracked files modified since the tag was checked out, or None if git could not say.
    dirty: list[str] | None
    #: The ref to restore the checkout to — the deployed tag, else the literal `HEAD`.
    ref: str
    #: Why `dirty` is None; empty when it is not.
    error: str


def _production_ref(root: Path) -> str:
    """The ref that restores the production checkout, resolved FROM GIT.

    Never from `pyproject.toml`: that file is one of the things drift can touch, and a
    remedy built from a drifted version names a tag nobody ever cut — so the pathspec
    fails and the reader concludes the CHECK is broken rather than the checkout. Falls
    back to `HEAD`, which restores the same tree whatever it is called.

    Not to be confused with `_production_version`, which reads the file deliberately:
    the release handshake is verifying what landed ON DISK, where git was correct and
    misleading at once (kn-58429229). Different question, different source.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return "HEAD"
    tag = out.stdout.strip()
    return tag if out.returncode == 0 and tag else "HEAD"


def production_status(directory: Path | None = None) -> ProductionStatus:
    """Is the production checkout still byte-identical to its tag, and how to fix it.

    `dirty == []` is clean. `-uno`: untracked files are not drift. The deploy's
    `git checkout -f` never removed them either, and `.venv/` and `.jarvis/` live there
    by design.

    `dirty is None` means git could not answer at all: not installed, the checkout
    unreadable, or — the realistic one, since the daemon and the user are different uids
    on the same tree — `detected dubious ownership`. Collapsing that into "clean" would
    silence the invariant permanently on the one checkout it exists to watch, so the two
    are distinguishable rather than both `[]`.

    `ref` is here rather than at the call site so the `jarvis-X.Y.Z` naming rule stays in
    this module, beside the `scripts/shipit.sh` counterpart that mints it.

    Backs `invariants.check_production_clean`.
    """
    root = directory or production_code_dir()
    ref = _production_ref(root)
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotePath=false", "-C", str(root),
             "status", "--porcelain", "-uno"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return ProductionStatus(None, ref, f"{type(e).__name__}: {e}")
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().replace("\n", " ")
        return ProductionStatus(
            None, ref, f"git status exited {out.returncode}: {detail or '(no output)'}")
    dirty = sorted(line[3:] for line in out.stdout.splitlines() if line.strip())
    return ProductionStatus(dirty, ref, "")
