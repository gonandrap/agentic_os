"""Background jobs a worker turn left behind when it ended (docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md)"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import db, usage

#: The event `worker_session._reap` writes when it catches one
EVENT = "background_orphaned"

#: `wo_messages.source` for the note a resume carries
SOURCE = "background-orphan"

#: The tool that kills a background job
KILL_TOOL = "KillShell"

#: The tools that poll one (docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md)
POLL_TOOL = ("BashOutput", "TaskOutput")

#: Every parameter spelling a poll or a kill names its subject with
JOB_ID_PARAMS = ("bash_id", "shell_id", "task_id", "agent_id", "subagent_id")

#: The status a `<task-notification>` or a poll reports while the job is still going
RUNNING = "running"

#: How the harness starts `toolUseResult` when the call FAILED or was refused
ERROR_PREFIX = "Error: "

_TASK_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_STATUS = re.compile(r"<status>\s*([^<\s]+)\s*</status>")


@dataclass(frozen=True)
class Job:
    """One background job a turn started and never collected"""

    job_id: str
    command: str

    def label(self) -> str:
        """How the job is named to a person"""
        if not self.command:
            return f"`{self.job_id}`"
        return f"`{self.job_id}` (`{_clip(self.command)}`)"


def labels(jobs: list[Job]) -> str:
    return ", ".join(job.label() for job in jobs)


#: How much of a command survives into a label
COMMAND_CHARS = 80


def _clip(command: str) -> str:
    one_line = " ".join(command.split())
    return (one_line[:COMMAND_CHARS] + "…" if len(one_line) > COMMAND_CHARS
            else one_line)


def orphaned_in_turn(store: Any, wo_id: str,
                     turn: dict[str, Any]) -> list[Job]:
    """Background jobs this turn started and never collected before it ended"""
    session_id = str(store.get_work_order(wo_id).get("session_id") or "")
    if not session_id:
        return []
    if _finished_in_turn(store, wo_id, turn):
        return []
    try:
        return jobs_left_running(
            session_id, since=turn["started_at"],
            until=turn.get("ended_at") or time.time())
    except OSError:
        return []


def _finished_in_turn(store: Any, wo_id: str, turn: dict[str, Any]) -> bool:
    return any(event.get("ts", 0.0) >= turn["started_at"]
               for event in store.events_of_kind(wo_id, "finished"))


def jobs_left_running(session_id: str, *, since: float, until: float,
                      index: dict[str, list[Path]] | None = None,
                      root: Path | None = None) -> list[Job]:
    """The transcript half, with no store in it — spec §2's table, in order"""
    if index is None:
        index = usage.index_sessions(root)
    launched: dict[str, dict[str, Any]] = {}  # tool_use id -> its input
    polls: dict[str, str] = {}                # tool_use id -> the job it polled
    jobs: dict[str, Job] = {}
    collected: set[str] = set()
    for path in sorted(index.get(session_id) or []):
        for row in usage.rows(path):
            stamp = usage.parse_stamp(row.get("timestamp"))
            if stamp and not since <= stamp <= until:
                continue
            _scan_calls(row, launched, polls, collected)
            _scan_results(row, launched, polls, jobs, collected)
            collected |= _notified(row)
    return [job for job_id, job in jobs.items() if job_id not in collected]


def _scan_calls(row: dict[str, Any], launched: dict[str, dict[str, Any]],
                polls: dict[str, str], collected: set[str]) -> None:
    """What the worker ASKED for: a launch to remember, a kill or a poll to match up"""
    for block in usage.blocks_of(row, "tool_use"):
        call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
        params = block.get("input")
        if not call_id or not isinstance(params, dict):
            continue
        if params.get("run_in_background"):
            launched[call_id] = params
        job_id = next((str(params[key]) for key in JOB_ID_PARAMS if params.get(key)), "")
        if not job_id:
            continue
        if name == KILL_TOOL:
            collected.add(job_id)
        elif name in POLL_TOOL:
            polls[call_id] = job_id


def _scan_results(row: dict[str, Any], launched: dict[str, dict[str, Any]],
                  polls: dict[str, str], jobs: dict[str, Job],
                  collected: set[str]) -> None:
    """What came BACK: the id a launch was given, and what a poll said about one (docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md)"""
    result = row.get("toolUseResult")
    outcome = result if isinstance(result, dict) else {}
    job_id = str(outcome.get("backgroundTaskId") or "")
    for block in usage.blocks_of(row, "tool_result"):
        call_id = str(block.get("tool_use_id") or "")
        if call_id in launched and not _failed(block, result):
            params = launched[call_id]
            named = job_id or call_id
            jobs[named] = Job(named, str(params.get("command")
                                         or params.get("description") or ""))
        polled = polls.get(call_id)
        if polled and _says_stopped(_text_of(block.get("content")), outcome):
            collected.add(polled)


def _failed(block: dict[str, Any], result: Any) -> bool:
    """Whether this tool_result reports an ERROR — a refused or failed call"""
    if block.get("is_error"):
        return True
    if isinstance(result, dict):
        return bool(result.get("is_error") or result.get("isError"))
    text = result if isinstance(result, str) else _text_of(block.get("content"))
    return text.startswith(ERROR_PREFIX)


def _says_stopped(text: str, outcome: dict[str, Any]) -> bool:
    status = str(outcome.get("status") or "")
    found = _STATUS.search(text)
    if found:
        status = status or found.group(1)
    return bool(status) and status.lower() != RUNNING


def _notified(row: dict[str, Any]) -> set[str]:
    """Job ids this row reports as finished"""
    done: set[str] = set()
    for text in _prose_of(row):
        if "<task-notification>" not in text:
            continue
        status = _STATUS.search(text)
        if status and status.group(1).lower() == RUNNING:
            continue
        done |= {found.group(1) for found in _TASK_ID.finditer(text)}
    return done


def _prose_of(row: dict[str, Any]) -> Iterator[str]:
    """Where the HARNESS writes a notification, and nowhere the model writes anything (#575)"""
    for value in (row.get("content"), (row.get("attachment") or {}).get("prompt")
                  if isinstance(row.get("attachment"), dict) else None):
        if isinstance(value, str):
            yield value
    content = (row.get("message") or {}).get("content")
    if row.get("type") == "user" and isinstance(content, str):
        yield content


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content
                        if isinstance(b, dict))
    return ""


def record(store: Any, wo_id: str, turn: dict[str, Any], jobs: list[Job],
           msg_id: int | None) -> None:
    """File the finding against the turn that produced it"""
    store.add_event(wo_id, EVENT, {
        "seq": turn["seq"], "msg_id": msg_id,
        "jobs": [{"id": job.job_id, "command": job.command} for job in jobs],
    })


def pending_orphan(store: Any, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The finding against this work order's LATEST turn, if that turn has one"""
    turn = store.latest_turn(wo["id"])
    if turn is None:
        return None
    for event in reversed(store.events_of_kind(wo["id"], EVENT)):
        payload = db.from_json(event.get("payload"), {}) or {}
        if payload.get("seq") == turn["seq"]:
            return payload
    return None


def jobs_of(payload: dict[str, Any]) -> list[Job]:
    return [Job(str(j.get("id") or ""), str(j.get("command") or ""))
            for j in (payload.get("jobs") or ()) if isinstance(j, dict)]


#: What the OS tells the worker on the way back in (docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md)
RESUME_NOTE = """\
[jarvis] Before anything else: your last turn ended while it still had a background job \
running, so that job was killed with the turn and produced nothing. Dead: {jobs}. \
Nobody typed this message — the OS detected it when the turn ended.

A turn is one `claude -p` process. NOTHING wakes you when a background job finishes, so \
backgrounding a command and ending the turn is the same as not running it. Re-run it in \
the background and POLL it inside this turn — `BashOutput` / `TaskOutput` every 3-4 \
minutes until it reports finished — and do not end a turn saying you \
will report when a run lands."""


def resume_note(store: Any, wo: dict[str, Any],
                queued: list[dict[str, Any]] = ()) -> str:  # type: ignore[assignment]
    """The note to carry into this work order's next turn, or "\""""
    if any(m.get("source") == SOURCE for m in queued):
        return ""
    payload = pending_orphan(store, wo)
    if payload is None:
        return ""
    jobs = jobs_of(payload)
    return RESUME_NOTE.format(jobs=labels(jobs) if jobs else "the job it started")


#: How many times the OS sends a work order back on its own before the user is asked
NUDGE_MAX = 2

#: The OS sent it back itself
NUDGED = "background_nudged"
UNRESOLVED = "background_unresolved"


def nudge(store: Any, wo: dict[str, Any]) -> bool:
    """Send this work order back to its worker over a job that died with its turn"""
    note = resume_note(store, wo)
    if not note:
        return False
    attempts = len(store.events_of_kind(wo["id"], NUDGED))
    if attempts >= NUDGE_MAX:
        # Said once, then never again: `pending_orphan` still matches the latest turn on every subsequent...
        if not store.events_of_kind(wo["id"], UNRESOLVED):
            store.add_event(wo["id"], UNRESOLVED, {"attempts": attempts})
        return False
    msg_id = store.queue_message(wo["id"], note, source=SOURCE)
    store.add_event(wo["id"], NUDGED, {"attempt": attempts + 1, "of": NUDGE_MAX,
                                       "msg_id": msg_id})
    return True
