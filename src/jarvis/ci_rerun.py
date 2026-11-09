"""Re-running a check GitHub cancelled on a pull request the OS waits on, once per head (#1123)"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .bugreport import gh_bin, gh_missing_message
from .ci import RED_RUN_CONCLUSIONS, RUN_FIELDS, SHA_RE, Run, parse_ts
from .github import GhUnavailable, GitHubError

log = logging.getLogger("jarvis.ci_rerun")

#: Same ceiling as `ci.CI_TIMEOUT`
RERUN_TIMEOUT = 30

#: Every `gh` subcommand this module may run
VERBS = (("run", "list"), ("run", "rerun"), ("run", "view"))

#: Of those, the one that changes something at GitHub
WRITE_VERBS = (("run", "rerun"),)

#: The statuses whose order waits on this pull request's CI (#1123)
CI_WAIT_STATUSES = ("waiting_pr_merge", "validating")

#: A WORKFLOW RUN conclusion that judged nothing, lower case as `gh run list` answers it
ABORTED_RUN_CONCLUSIONS = frozenset({"cancelled", "startup_failure", "stale"})

#: A run conclusion that, on a run with no job, judged nothing and reported no check (#1657)
JOBLESS_RUN_CONCLUSIONS = RED_RUN_CONCLUSIONS | ABORTED_RUN_CONCLUSIONS

#: How many runs of one head are read
HEAD_RUN_LIMIT = 30

#: Timeline kinds: the one re-run, the give-up, and the head clearing
RERUN_EVENT = "ci_rerun_requested"
STUCK_EVENT = "ci_rerun_unrecoverable"
RECOVERED_EVENT = "ci_rerun_recovered"

#: A FAILED check's one re-run per head, and its green result: a flake (#1465)
FLAKE_RERUN_EVENT = "ci_flake_rerun_requested"
FLAKE_CONFIRMED_EVENT = "ci_flake_confirmed"

#: `ci_reruns` key prefix of a red default-branch run's one re-run; an aborted run's key is its bare id
BASE_FAILED_PREFIX = "failed:"

#: A pytest summary line in a failed job's log
FAILED_TEST_RE = re.compile(r"\b(?:FAILED|ERROR) (\S+\.py(?:::\S+)?)")

#: How many test names one flake record carries
MAX_FLAKE_TESTS = 20

#: Why the OS stopped, as stable tokens
CAUSE_CANCELLED_AGAIN = "cancelled_again"
CAUSE_NO_RUN = "no_run"
CAUSE_REFUSED = "rerun_refused"
CAUSE_NEVER_RAN = "never_ran"


def _run(args: list[str], *, cwd: Path | None, missing_hint: str) -> str:
    """One `gh` call, as stdout"""
    where = str(cwd) if cwd and Path(cwd).is_dir() else None
    shown = "`gh " + " ".join(args[:3]) + "`"
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=RERUN_TIMEOUT, cwd=where, errors="replace")
    except FileNotFoundError as e:
        raise GhUnavailable(gh_missing_message(missing_hint), GitHubError.NO_GH) from e
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"{shown} timed out after {RERUN_TIMEOUT}s",
                          GitHubError.TIMEOUT) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        log.info("%s failed: %s", shown, detail)
        raise GitHubError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def head_runs(sha: str, cwd: Path | None = None) -> tuple[Run, ...]:
    """Every workflow run on commit `sha`, newest first"""
    if not SHA_RE.match(sha or ""):
        raise GitHubError(f"{sha!r} is not a commit this may ask about",
                          GitHubError.URL_REFUSED)
    stdout = _run(["run", "list", "--commit", sha, "--limit", str(HEAD_RUN_LIMIT),
                   "--json", RUN_FIELDS], cwd=cwd,
                  missing_hint="so Jarvis cannot re-run a cancelled check")
    try:
        rows = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(f"`gh run list --commit {sha}` returned no JSON",
                          GitHubError.UNREADABLE) from e
    if not isinstance(rows, list):
        raise GitHubError(f"`gh run list --commit {sha}` returned {rows!r}",
                          GitHubError.UNREADABLE)
    runs = [Run(run_id=int(r.get("databaseId") or 0),
                head_sha=str(r.get("headSha") or ""),
                conclusion=str(r.get("conclusion") or "").lower(),
                status=str(r.get("status") or "").lower(),
                started_at=parse_ts(r.get("startedAt")),
                workflow=str(r.get("workflowName") or ""))
            for r in rows if isinstance(r, dict)]
    return tuple(sorted(runs, key=lambda r: (r.started_at is not None, r.started_at or 0),
                        reverse=True))


def rerun_command(run_id: int) -> str:
    """The exact command the OS runs, and the one it hands the user"""
    return f"gh run rerun {int(run_id)} --failed"


def rerun(run_id: int, cwd: Path | None = None) -> None:
    """Re-run the failed and cancelled jobs of one run; the whole run if it has none"""
    run_id = int(run_id)
    if run_id <= 0:
        raise GitHubError(f"{run_id!r} is not a run this may re-run",
                          GitHubError.URL_REFUSED)
    hint = "so Jarvis cannot re-run a cancelled check"
    try:
        _run(["run", "rerun", str(run_id), "--failed"], cwd=cwd, missing_hint=hint)
    except GhUnavailable:
        raise
    except GitHubError:
        # A startup failure has no job to re-run
        _run(["run", "rerun", str(run_id)], cwd=cwd, missing_hint=hint)


def runs_to_rerun(aborted: tuple[dict[str, str], ...],
                  runs: tuple[Run, ...]) -> tuple[Run, ...]:
    """The newest aborted run of each workflow an aborted check belongs to, or `()` if any has none"""
    picked: dict[str, Run] = {}
    for check in aborted:
        workflow = str(check.get("workflow") or "")
        found = next((r for r in runs if workflow and r.workflow == workflow
                      and r.completed and r.conclusion in ABORTED_RUN_CONCLUSIONS
                      and r.run_id > 0), None)
        if found is None:
            return ()
        picked[workflow] = found
    return tuple(picked.values())


def job_count(run_id: int, cwd: Path | None = None) -> int:
    """How many jobs the latest attempt of this run has"""
    shown = f"`gh run view {int(run_id)}`"
    stdout = _run(["run", "view", str(int(run_id)), "--json", "jobs"], cwd=cwd,
                  missing_hint="so Jarvis cannot re-run a run that produced no job")
    try:
        jobs = json.loads(stdout)["jobs"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise GitHubError(f"{shown} returned no job list", GitHubError.UNREADABLE) from e
    if not isinstance(jobs, list):
        raise GitHubError(f"{shown} returned no job list", GitHubError.UNREADABLE)
    return len(jobs)


def jobless_runs(runs: tuple[Run, ...], cwd: Path | None = None) -> tuple[Run, ...]:
    """Each workflow's newest run on this head that ended unsuccessful with no job (#1657)"""
    newest: dict[str, Run] = {}
    for r in runs:
        newest.setdefault(r.workflow, r)
    return tuple(r for r in newest.values()
                 if r.completed and r.run_id > 0 and r.conclusion in JOBLESS_RUN_CONCLUSIONS
                 and job_count(r.run_id, cwd) == 0)


def failed_runs_to_rerun(red: tuple[dict[str, str], ...],
                         runs: tuple[Run, ...]) -> tuple[Run, ...]:
    """The newest red run of each workflow a failing check belongs to (#1465)"""
    picked: dict[str, Run] = {}
    for check in red:
        workflow = str(check.get("workflow") or "")
        found = next((r for r in runs if workflow and r.workflow == workflow
                      and r.completed and r.red and r.run_id > 0), None)
        if found is not None:
            picked[workflow] = found
    return tuple(picked.values())


def base_failed_key(run: Run) -> str:
    """Once per head: a second run of this workflow on this commit shares the key"""
    return f"{BASE_FAILED_PREFIX}{run.head_sha}:{run.workflow}"


def base_verdict(runs: tuple[Run, ...], workflow: str, reruns: Any) -> Run | None:
    """`ci.latest`, except a red run's re-run stays red until judged: older green opens the merges"""
    for r in runs:
        if r.workflow != workflow:
            continue
        if (base_failed_key(r) in reruns
                and (not r.completed or r.conclusion in ABORTED_RUN_CONCLUSIONS)):
            return replace(r, status="completed", conclusion="failure")
        if r.completed:
            return r
    return None


def failed_tests(run_id: int, cwd: Path | None = None) -> tuple[str, ...]:
    """The tests a finished run failed, or `()` if its log names none or cannot be read"""
    try:
        stdout = _run(["run", "view", str(int(run_id)), "--log-failed"], cwd=cwd,
                      missing_hint="so Jarvis cannot name the failed tests")
    except GitHubError as e:
        log.info("could not read the failed log of run %s: %s", run_id, e)
        return ()
    return tuple(dict.fromkeys(FAILED_TEST_RE.findall(stdout)))[:MAX_FLAKE_TESTS]


def head_events(store: Any, wo_id: str, kind: str, head: str) -> list[dict[str, Any]]:
    """The payloads of `kind` events on this head"""
    from . import db

    return [p for p in (db.from_json(e["payload"], {}) or {}
                        for e in store.events_of_kind(wo_id, kind))
            if str(p.get("head_sha") or "") == head]


@dataclass(frozen=True)
class Episode:
    """What the timeline says the OS already did about one head"""

    reruns: tuple[dict[str, Any], ...]
    stuck: dict[str, Any] | None


def episode(store: Any, wo_id: str, head: str) -> Episode:
    """This head's re-runs and give-up, read off the timeline"""
    stuck = head_events(store, wo_id, STUCK_EVENT, head)
    return Episode(reruns=tuple(head_events(store, wo_id, RERUN_EVENT, head)),
                   stuck=stuck[-1] if stuck else None)


def stuck_blocker(pr_url: str, head: str, cause: str, checks: str,
                  commands: tuple[str, ...], jobless: bool = False) -> str:
    """The one attention line, naming the pull request and what to type"""
    what = {
        CAUSE_CANCELLED_AGAIN: f"CI was cancelled again after the OS's one re-run ({checks})",
        CAUSE_NO_RUN: f"CI was cancelled ({checks}) and the OS found no run to re-run",
        CAUSE_REFUSED: f"CI was cancelled ({checks}) and GitHub refused the OS's re-run",
        CAUSE_NEVER_RAN: (f"CI ran but produced no job, so no check ever reported "
                          f"({checks}), again after the OS's one re-run" if jobless else
                          f"required check(s) never reported ({checks}) — the branch's "
                          f"workflow may predate its base's"),
    }.get(cause, f"CI did not finish ({checks})")
    return (f"{pr_url} at {head[:10]}: {what}. The OS will not re-run it again on this "
            f"commit — run {' or '.join(f'`{c}`' for c in commands)}")


def update_branch_command(pr_url: str) -> str:
    return f"gh pr update-branch {pr_url}"
