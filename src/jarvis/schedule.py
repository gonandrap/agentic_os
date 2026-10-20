"""The scheduler: work orders the OS files without being asked (docs/superpowers/specs/2026-09-14-the-scheduler.md)"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The origin every scheduled work order carries — `project_store.WO_ORIGINS`
ORIGIN = "schedule"

SECONDS_PER_HOUR = 3600

#: What `decide` returns
FIRE, HOLD, WAIT = "fire", "hold", "wait"


@dataclass(frozen=True)
class JobContext:
    """What a job knows about the firing it is about to describe"""

    project: str
    #: Whether THIS project is the one that owns the running OS install
    owns_os: bool = False


@dataclass(frozen=True)
class ScheduledJob:
    """One recurring job"""

    id: str
    title: str
    describe: Callable[[JobContext], str]


# -- the first rider: the daily doctor


def _doctor_description(ctx: JobContext) -> str:
    """The daily `jarvis doctor` run, and what to do with what it finds (#164)"""
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

#: Every recurring job the OS knows how to file
JOBS: tuple[ScheduledJob, ...] = (DOCTOR_JOB,)

JOB_IDS: tuple[str, ...] = tuple(j.id for j in JOBS)


def job(job_id: str) -> ScheduledJob:
    for j in JOBS:
        if j.id == job_id:
            return j
    raise KeyError(job_id)


# -- who owns the OS


def os_owner(projects: Iterable[tuple[str, Path]], *, fallback: bool = True) -> str | None:
    """Which project runs the fleet-wide OS checks — exactly one, or none at all"""
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
    return first if fallback else None


#: `os_project`'s origin reads, memoised by the path asked about
_ORIGIN_CACHE: dict[str, tuple[str, str] | None] = {}


def _origin_of(path: Path | None) -> tuple[str, str] | None:
    """`github.origin_repo(path)`, cached, and never the thing that raises"""
    # Function-local, and must stay so: this module imports nothing of Jarvis's at module scope
    from . import github

    if path is None:
        return None
    key = str(path)
    if key not in _ORIGIN_CACHE:
        try:
            _ORIGIN_CACHE[key] = github.origin_repo(path)
        except (OSError, ValueError):   # an unreadable path is not an identity
            _ORIGIN_CACHE[key] = None
    return _ORIGIN_CACHE[key]


def os_candidates(projects: Iterable[tuple[str, Path]], *,
                  install: Path | None = None) -> list[str]:
    """Every catalog project whose git `origin` is this code's — `os_project`'s matches"""
    mine = _origin_of(install if install is not None else Path(__file__).resolve().parent)
    if mine is None:
        return []
    return [name for name, path in projects if _origin_of(path) == mine]


def os_project(projects: Iterable[tuple[str, Path]], *,
               install: Path | None = None) -> str | None:
    """Which catalog project IS the OS — the one whose git `origin` is this code's, or None"""
    matches = os_candidates(projects, install=install)
    return matches[0] if len(matches) == 1 else None


# -- the cadence


@dataclass(frozen=True)
class Decision:
    """What to do about one job on one tick"""

    action: str          # FIRE | HOLD | WAIT
    reason: str = ""     # why, in words, for the record and for `jarvis doctor`


def decide(state: dict[str, Any], *, interval_seconds: float, now: float,
           blocker: dict[str, Any] | None) -> Decision:
    """Fire, hold, or wait — the whole cadence, as a pure function"""
    if now - float(state["last_fired_at"] or 0.0) < interval_seconds:
        return Decision(WAIT)
    if blocker is not None:
        return Decision(HOLD, f"{blocker['id']} is still {blocker['status']}")
    return Decision(FIRE)


def held_seconds(state: dict[str, Any], now: float) -> float:
    """How long this job has wanted to fire and could not. 0.0 when it is not held"""
    since = state.get("held_since")
    return max(0.0, now - float(since)) if since else 0.0
