"""The scheduler: work orders the OS files without being asked.

Design, and the ruling behind each guard rail:
docs/superpowers/specs/2026-09-14-the-scheduler.md.

Layering: this module imports NOTHING else of Jarvis's, which is what lets `catalog`
import it for the job roster without a cycle. The cadence is a PURE FUNCTION of a stored
row, a config and a clock (`decide`), so the one thing that must never be got wrong — "may the OS spend money right now" — is
testable without a store, a daemon or a catalog. `Daemon.schedule_tick` is the only
caller that turns a `Decision` into a work order.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The origin every scheduled work order carries — `project_store.WO_ORIGINS`. It exists
#: for the reason `neo` does: the OS filed this, nobody typed it, and a listing that
#: cannot tell the two apart cannot answer "why am I paying for this".
ORIGIN = "schedule"

SECONDS_PER_HOUR = 3600

#: What `decide` returns. `wait` is the overwhelmingly common answer and writes nothing.
FIRE, HOLD, WAIT = "fire", "hold", "wait"


@dataclass(frozen=True)
class JobContext:
    """What a job knows about the firing it is about to describe.

    A struct rather than a bare project name so a rider added later can branch on
    something new without changing every existing `describe`.
    """

    project: str
    #: Whether THIS project is the one that owns the running OS install. Only it runs
    #: the OS-level checks: they are fleet-wide, so N projects reporting the same OS
    #: fault every morning is N-1 copies of the alarm nobody reads.
    owns_os: bool = False


@dataclass(frozen=True)
class ScheduledJob:
    """One recurring job. Adding a rider is an entry in `JOBS` and nothing else.

    `describe` builds the work order's description at firing time rather than storing
    one: the text is the ONLY thing the worker sees, and a job whose description was
    frozen at catalog-parse time could not name the project it woke up in.
    """

    id: str
    title: str
    describe: Callable[[JobContext], str]


# -- the first rider: the daily doctor ---------------------------------------------------


def _doctor_description(ctx: JobContext) -> str:
    """The daily `jarvis doctor` run, and what to do with what it finds.

    Deliberately does NOT enumerate the checks. `invariants.INVARIANTS` grows — two
    sibling orders on issue #164 are adding to it right now — and a description that
    listed them would have to be rewritten by each one, which is exactly the coupling
    this registry exists to avoid. The order runs the doctor; the doctor decides what
    the doctor checks.
    """
    scope = f"jarvis doctor {ctx.project} --repair"
    if not ctx.owns_os:
        scope += " --skip-os"
    os_note = (
        "You are the project that owns the running OS install, so this run also carries "
        "the OS-level checks (the dashboard, the gate canaries, config drift). No other "
        "project runs them."
        if ctx.owns_os else
        "`--skip-os` because the OS-level checks are fleet-wide and another project owns "
        "them: without it every project in the fleet would report the same OS fault every "
        "morning."
    )
    return f"""\
The daily post-condition check for `{ctx.project}`. The OS filed this on a schedule — \
nobody typed it.

RUN IT:

    {scope}

`--repair` because the daemon already applies these same repairs on every reconcile \
tick, so this run repairs nothing the OS would not have done anyway. \
{os_note}

THEN TURN WHAT IT FOUND INTO WORK. For each violation reported:

* A line with `→ fixed:` under it was repaired by this run. Nothing further is owed.
* Anything else is a defect a person has to fix. BEFORE FILING ANYTHING, run \
`jarvis wo list {ctx.project}` and check whether an open work order already covers it — \
this job runs every day and the same fault will still be there tomorrow. If one exists, \
`jarvis wo send <id> "<what today's run still shows>"` instead of filing a duplicate.
* Otherwise file it: `jarvis wo create {ctx.project} "<title>" --description "<the \
violation verbatim, what it means, and what a fix has to verify>"`. Where the fix has to \
be shipped as well as written, file the shipping order too and point it at the first \
with `--depends-on <id>`.

YOU DO NOT FIX ANYTHING YOURSELF. This order's deliverable is the triage and the orders \
it filed; it writes no code, so a plain `jarvis wo finish <id> --summary "…"` settles it.

Name every work order you filed in that summary, and say what you deliberately did NOT \
file and why. If the run was clean, one line saying so is the whole summary.

Do not list the checks that ran: the set grows, and a summary that enumerates it is \
wrong by the next release."""


DOCTOR_JOB = ScheduledJob(
    id="daily-doctor",
    title="Daily doctor: check this project's post-conditions and file what they found",
    describe=_doctor_description,
)

#: Every recurring job the OS knows how to file. A rider is one entry here plus its
#: `describe`; `ScheduleConfig.jobs` decides which of them a project actually runs, and
#: an id named there that is absent from this tuple is a `CatalogError` at boot.
JOBS: tuple[ScheduledJob, ...] = (DOCTOR_JOB,)

JOB_IDS: tuple[str, ...] = tuple(j.id for j in JOBS)


def job(job_id: str) -> ScheduledJob:
    for j in JOBS:
        if j.id == job_id:
            return j
    raise KeyError(job_id)


# -- who owns the OS ---------------------------------------------------------------------


def os_owner(projects: Iterable[tuple[str, Path]]) -> str | None:
    """Which project runs the fleet-wide OS checks — exactly one, or none at all.

    The project whose directory contains the `jarvis` package that is RUNNING: in dev
    that is the checkout being edited, in production the deployed tag. Derived rather
    than declared because a catalog key would be one more thing to get wrong on a
    deployment that already knows the answer.

    Falls back to the first project in catalog order when no project contains the
    install (a pip-installed OS driving projects that are not it). The fallback is
    arbitrary but it is DETERMINISTIC AND UNIQUE, which is the property that matters:
    the checks still run somewhere, and they still run once.
    """
    pkg = Path(__file__).resolve().parent
    first: str | None = None
    for name, path in projects:
        if first is None:
            first = name
        try:
            if pkg.is_relative_to(Path(path).resolve()):
                return name
        except (OSError, ValueError):   # unresolvable path: not the owner
            continue
    return first


# -- the cadence -------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """What to do about one job on one tick."""

    action: str          # FIRE | HOLD | WAIT
    reason: str = ""     # why, in words, for the record and for `jarvis doctor`


def decide(state: dict[str, Any], *, interval_seconds: float, now: float,
           blocker: dict[str, Any] | None) -> Decision:
    """Fire, hold, or wait — the whole cadence, as a pure function.

    THREE PROPERTIES, each of which is a ruling rather than an implementation choice
    (spec §3):

    * **At most one catch-up, never a backfill.** Due-ness is `now - last_fired_at >=
      interval` and firing sets `last_fired_at = now`, NOT `last_fired_at + interval`. A
      daemon that was off for a week comes back and files one order, not seven. The
      alternative — advancing by whole intervals — turns an outage into a burst of
      spending at exactly the moment the fleet is least well.

    * **A restart never fires.** `last_fired_at` is seeded to the moment the job is first
      seen enabled (`ProjectStore.seed_schedule`) and lives in the project store, so it
      survives the daemon. Nothing about starting the OS makes a job due.

    * **Held, not queued.** A job whose previous order has not settled does not fire and
      does not advance its clock, so the backlog can never grow past one. It raises no
      attention either — six stacked doctor orders is precisely the alarm nobody reads —
      but the hold IS persisted, so `jarvis doctor` and `jarvis status` can say that a
      scheduler is parked rather than dead (INV-SCHEDULE-HELD).
    """
    if now - float(state["last_fired_at"] or 0.0) < interval_seconds:
        return Decision(WAIT)
    if blocker is not None:
        return Decision(HOLD, f"{blocker['id']} is still {blocker['status']}")
    return Decision(FIRE)


def held_seconds(state: dict[str, Any], now: float) -> float:
    """How long this job has wanted to fire and could not. 0.0 when it is not held."""
    since = state.get("held_since")
    return max(0.0, now - float(since)) if since else 0.0
