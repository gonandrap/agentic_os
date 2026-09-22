"""Background jobs a worker turn left behind when it ended.

docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md, issue
#575. A turn is one `claude -p` process: whatever it backgrounded dies with it, and the
worker's parting "I'll report when it lands" is false the moment it is written. The
briefing has forbidden this since `TEMPLATE_VERSION` v10 and a worker violated it on two
consecutive turns, so what is needed here is a CHECK rather than another sentence.

Reads the session transcript and nothing else — no process lookup, no model call. The
job is dead by the time anyone asks; the question this module answers is whether the
turn ever collected it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import db, usage

#: The event `worker_session._reap` writes when it catches one. Payload: `seq`, the
#: reply's `msg_id`, and `jobs` — one `{id, command}` each.
EVENT = "background_orphaned"

#: `wo_messages.source` for the note a resume carries. In
#: `timeline.UNAUTHORED_SOURCES`, so the conversation renders it as Jarvis speaking: a
#: detector wrote it, not the user, and the worker must not read it as the user's words.
SOURCE = "background-orphan"

#: The tool that kills a background job. Naming one is collection of a sort — the worker
#: knew the job was there and dealt with it, so nothing was abandoned.
KILL_TOOL = "KillShell"

#: The tool that polls one. Its result carries a status; anything other than `running`
#: means the worker saw the job end inside the turn.
POLL_TOOL = "BashOutput"

#: The status a `<task-notification>` or a poll reports while the job is still going.
#: Every other value is an ending of some kind — completed, failed, killed — and all of
#: them are collection. UNVERIFIED AGAINST A LIVE POLL: no transcript in the fleet has
#: ever contained a `BashOutput`, which is exactly why this bug reached production, so
#: the poll shape is read tolerantly (any `<status>` tag, any `status` field) rather than
#: matched exactly. kn-df5574d3: a measurement of an external tool expires when that tool
#: ships a version, and nothing fails when it does.
RUNNING = "running"

_TASK_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
_STATUS = re.compile(r"<status>\s*([^<\s]+)\s*</status>")


@dataclass(frozen=True)
class Job:
    """One background job a turn started and never collected."""

    job_id: str
    command: str

    def label(self) -> str:
        """How the job is named to a person — the id first, because that is what the
        user can check against `ps` and what the OS's own alarm quoted."""
        if not self.command:
            return f"`{self.job_id}`"
        return f"`{self.job_id}` (`{_clip(self.command)}`)"


def labels(jobs: list[Job]) -> str:
    return ", ".join(job.label() for job in jobs)


#: How much of a command survives into a label. A `uv run pytest …` line with three
#: pipes is a paragraph, and this text lands in a message the worker reads and a marker
#: the user reads; both want the head of it.
COMMAND_CHARS = 80


def _clip(command: str) -> str:
    one_line = " ".join(command.split())
    return (one_line[:COMMAND_CHARS] + "…" if len(one_line) > COMMAND_CHARS
            else one_line)


def orphaned_in_turn(store: Any, wo_id: str,
                     turn: dict[str, Any]) -> list[Job]:
    """Background jobs this turn started and never collected before it ended.

    The exemption is a `finished` event inside the turn: a worker that called
    `jarvis wo finish` has had its authoritative last word recorded, and a job it
    collected on the way to finishing must not be reported as abandoned (spec §2).

    Returns empty rather than raising on anything the transcript cannot answer — no
    session id, no file, an unreadable one. A detector that crashes the reaper would
    cost the work order its whole turn record.
    """
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
    """The transcript half, with no store in it — spec §2's table, in order.

    Bounded to ONE TURN by `since`/`until` for `usage.said_in_session`'s reason: a
    session outlives the turn that stalled in it, and a job the previous turn left
    behind is the previous turn's finding, already on the record.
    """
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
    """What the worker ASKED for: a launch to remember, a kill or a poll to match up."""
    for block in usage.blocks_of(row, "tool_use"):
        call_id, name = str(block.get("id") or ""), str(block.get("name") or "")
        params = block.get("input")
        if not call_id or not isinstance(params, dict):
            continue
        if params.get("run_in_background"):
            launched[call_id] = params
        # Both spellings, because the parameter is `bash_id` on the poll and `shell_id`
        # on the kill and neither is this module's to choose.
        job_id = str(params.get("bash_id") or params.get("shell_id") or "")
        if not job_id:
            continue
        if name == KILL_TOOL:
            collected.add(job_id)
        elif name == POLL_TOOL:
            polls[call_id] = job_id


def _scan_results(row: dict[str, Any], launched: dict[str, dict[str, Any]],
                  polls: dict[str, str], jobs: dict[str, Job],
                  collected: set[str]) -> None:
    """What came BACK: the id a launch was given, and what a poll said about one.

    The id is read from `toolUseResult.backgroundTaskId` — structured, and the same
    field the harness writes for every background launch — rather than parsed out of the
    English sentence beside it.
    """
    result = row.get("toolUseResult")
    outcome = result if isinstance(result, dict) else {}
    job_id = str(outcome.get("backgroundTaskId") or "")
    for block in usage.blocks_of(row, "tool_result"):
        call_id = str(block.get("tool_use_id") or "")
        if job_id and call_id in launched:
            params = launched[call_id]
            jobs[job_id] = Job(job_id, str(params.get("command")
                                           or params.get("description") or ""))
        polled = polls.get(call_id)
        if polled and _says_stopped(_text_of(block.get("content")), outcome):
            collected.add(polled)


def _says_stopped(text: str, outcome: dict[str, Any]) -> bool:
    status = str(outcome.get("status") or "")
    found = _STATUS.search(text)
    if found:
        status = status or found.group(1)
    return bool(status) and status.lower() != RUNNING


def _notified(row: dict[str, Any]) -> set[str]:
    """Job ids this row reports as finished.

    The `<task-notification>` block Claude Code delivers when a background job ends —
    verified on a real transcript. It is what a worker turn NEVER receives, which is the
    bug; it is here so that an interactive session, which does, is not accused of
    abandoning a job it was told about.
    """
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
    """Every place a transcript row carries free text. A notification arrives as a
    `queue-operation`'s `content` and again as an `attachment`'s `prompt`; a user row
    carries it in the message."""
    for value in (row.get("content"), (row.get("attachment") or {}).get("prompt")
                  if isinstance(row.get("attachment"), dict) else None):
        if isinstance(value, str):
            yield value
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        yield content
    for block in usage.blocks_of(row):
        text = block.get("text") if block.get("type") == "text" else None
        if isinstance(text, str):
            yield text


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content
                        if isinstance(b, dict))
    return ""


def record(store: Any, wo_id: str, turn: dict[str, Any], jobs: list[Job],
           msg_id: int | None) -> None:
    """File the finding against the turn that produced it.

    `msg_id` is the reply the promise is IN, and it is what `timeline.build_conversation`
    voids. A turn that ended without saying anything has none, and the event is still
    worth writing: the job died either way.
    """
    store.add_event(wo_id, EVENT, {
        "seq": turn["seq"], "msg_id": msg_id,
        "jobs": [{"id": job.job_id, "command": job.command} for job in jobs],
    })


def pending_orphan(store: Any, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The finding against this work order's LATEST turn, if that turn has one.

    Keyed on the turn rather than on "has this ever happened": the next turn is the
    retry, and once it has run the note has been given. Nothing has to be cleared.
    """
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


#: What the OS tells the worker on the way back in. Says the three things the last turn
#: got wrong — the job is dead, a turn is one-shot, run it in the foreground — and says
#: who is speaking, because the user did not write it (spec §4).
RESUME_NOTE = """\
[jarvis] Before anything else: your last turn ended while it still had a background job \
running, so that job was killed with the turn and produced nothing. Dead: {jobs}. \
Nobody typed this message — the OS detected it when the turn ended.

A turn is one `claude -p` process. NOTHING wakes you when a background job finishes, so \
backgrounding a command and ending the turn is the same as not running it. Re-run it in \
the FOREGROUND and wait for it, however long it takes, and do not end a turn saying you \
will report when a run lands."""


def resume_note(store: Any, wo: dict[str, Any],
                queued: list[dict[str, Any]] = ()) -> str:  # type: ignore[assignment]
    """The note to carry into this work order's next turn, or "".

    ONCE PER TURN, and `queued` is what makes that true: a delivery whose transport
    failed leaves its messages QUEUED, so the note it already wrote comes back in the
    next tick's batch. Seeing itself there is the whole guard — the turn it was written
    for is still the latest one, so nothing else about the finding has changed.

    Past that tick the seq is the guard: `worker_session.send` opens a new turn, and
    `pending_orphan` is keyed on the latest one.
    """
    if any(m.get("source") == SOURCE for m in queued):
        return ""
    payload = pending_orphan(store, wo)
    if payload is None:
        return ""
    jobs = jobs_of(payload)
    return RESUME_NOTE.format(jobs=labels(jobs) if jobs else "the job it started")
