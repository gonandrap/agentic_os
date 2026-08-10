"""The conversation layer: how a message reaches a working session, and how the OS
learns what came back.

Everything above this module speaks in **turns**. Nothing above it knows that a turn is
a `claude -p` process, where its output lands, or how its liveness is judged — so the
transport can change again without the daemon, dispatch, ops or the UI moving.

## Why turns and not background sessions

Workers used to run as Claude Code background agents, one *new* agent per delivered turn
(`claude --bg --resume <sid>`), with the superseded one retired on a best-effort
`claude stop`. That transport had three problems, all structural:

* it leaked — any failed retirement left a dead agent in the roster forever (the live
  fleet reached 63);
* the session id moved — `--bg --resume` forks under a supervisor-assigned id, which
  needed `bind_session`, a `prior_sessions` trail, an invariant, hook-time binding and
  name-matching just to keep a pointer honest;
* completion was read out of the supervisor's private `~/.claude/jobs/<id>/state.json`,
  behind a three-strikes retry because the file lands asynchronously.

A headless turn fixes all three at once, because of one verified property: **`claude -p
--resume <sid>` reuses the session id** rather than forking it (`--fork-session` exists
to opt into forking). So Jarvis mints the id with `--session-id` before the first process
starts, and it is immutable for the work order's whole life. The pointer cannot move, so
nothing is needed to stop it moving.

## The shape of a turn

    claude -p --session-id <uuid> [briefing] -- "<prompt>"      # turn 1
    claude -p --resume     <uuid> [briefing] -- "<prompt>"      # every turn after

Detached, stdout redirected to `<project>/.jarvis/turns/<wo-id>/<seq>.json`. `poll()`
reaps it: process gone + parseable JSON = done, and the JSON's `result` IS the worker's
final message.

Turns are detached rather than awaited on a thread pool because `shipit` restarts jarvisd
on every release and a turn can run for hours — a turn parented to the daemon would lose
its reply on each deploy.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude_cli
from .catalog import ProjectSpec
from .project_store import ProjectStore

log = logging.getLogger("jarvisd")

#: A turn still running after this long is reported, never killed: a legitimately long
#: turn is indistinguishable from a hung one from out here, and killing loses real work.
TURN_STALL_SECONDS = 6 * 3600

#: How long to wait before retrying a turn whose limit message named a reset but no time
#: this parser could read. Short enough to recover the same evening, long enough that a
#: dozen work orders re-asking do not become a poll loop against the CLI.
RATE_LIMIT_FALLBACK_DELAY = 15 * 60

#: The floor under every retry, measured from the refusal. The reset moment is a clock
#: time rounded to the minute, so it can land a few seconds in the past the instant it
#: is parsed; without this the first retry would go out immediately and be refused again.
RATE_LIMIT_MIN_DELAY = 60

#: How many consecutive turns of ONE conversation may be refused for the limit before
#: the OS stops retrying and hands it to the user. A real window reopens after one wait,
#: so a streak this long means the parse is wrong or something structural is — and
#: retrying for ever would hide that behind a work order that looks busy and is not.
MAX_RATE_LIMIT_RETRIES = 8


def new_session_id() -> str:
    """Mint the session id for a work order. `--session-id` requires a valid UUID."""
    return str(uuid.uuid4())


def turns_dir(project: ProjectSpec, wo_id: str) -> Path:
    return project.path / ".jarvis" / "turns" / wo_id


def worktree_path(project: ProjectSpec, wo: dict[str, Any]) -> Path | None:
    wt = wo.get("worktree")
    if not wt:
        return None
    path = project.path / ".claude" / "worktrees" / wt
    return path if path.is_dir() else None


def briefing_for(project: ProjectSpec, wo: dict[str, Any]) -> dict[str, Any]:
    """The flags every turn of this work order is launched with.

    A resumed session re-derives its model, effort, permission mode, system prompt and
    reachable directories from argv — it does NOT inherit them from the transcript. So
    the briefing is rebuilt identically for every turn; anything dropped here is simply
    absent from that turn onwards, including the project's standing instructions and the
    OS's own skills. (This is the property #42 established for the old fallback path,
    preserved on the new transport.)

    `permission_mode` falls back to the project default rather than staying NULL: a
    headless turn cannot answer a permission prompt, so Claude's default mode is not the
    conservative choice, it is a guaranteed stall. Only `adhoc` rows reach here with the
    column unset — dispatch persists the resolved value back onto dispatched ones — and
    resolving it here keeps the column's NULL meaningful.

    `add_dirs` is the one flag that now depends on the work order's KIND: a planner also
    reaches the architect and test-lead seat definitions, and an ordinary worker does not
    (design decision 4). Because a resumed turn re-derives its flags from argv, this is
    also what keeps the seats available on the planner's second and later turns — dropping
    the kind here would make them vanish after turn 1.
    """
    from .bootstrap import install_agent_assets
    from .dispatch import _write_worker_settings

    return {
        "model": wo.get("model") or project.worker.model,
        "effort": wo.get("effort") or project.worker.effort,
        "permission_mode": (wo.get("permission_mode")
                            or project.worker.permission_mode),
        "append_system_prompt": (wo.get("append_system_prompt")
                                 or project.worker.append_system_prompt),
        "settings_file": _write_worker_settings(project, wo),
        "add_dirs": install_agent_assets(project.path, wo.get("kind") or "worker"),
    }


def busy(store: ProjectStore, wo_id: str) -> dict[str, Any] | None:
    """The work order's in-flight turn, or None. One turn at a time, always.

    A second concurrent turn would resume the same session id while the first still
    holds it, which the CLI refuses — and even if it did not, the two turns would
    interleave into one transcript.
    """
    turn = store.latest_turn(wo_id)
    return turn if turn and turn["state"] == "running" else None


def start(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
          prompt: str) -> dict[str, Any]:
    """Open the conversation: turn 1, in a fresh worktree, under a minted session id."""
    wo_id = wo["id"]
    session_id = wo.get("session_id") or new_session_id()
    store.update_work_order(wo_id, session_id=session_id, worktree=wo_id)
    return _launch(store, project, {**wo, "session_id": session_id, "worktree": wo_id},
                   prompt, kind="dispatch", resume=False, worktree=wo_id,
                   cwd=project.path)


def send(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any], text: str,
         msg_id: int | None = None) -> dict[str, Any]:
    """Deliver a message as the work order's next turn.

    The turn resumes the same session id the work order has always had, from inside its
    worktree (transcripts are keyed by the directory the session was created in, so
    resuming from the project root would not find the conversation).
    """
    _release_background_owner(store, wo)
    cwd = worktree_path(project, wo) or project.path
    return _launch(store, project, wo, text, kind="message", resume=True,
                   worktree=None, cwd=cwd, msg_id=msg_id)


def _launch(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any], prompt: str,
            kind: str, resume: bool, worktree: str | None, cwd: Path,
            msg_id: int | None = None) -> dict[str, Any]:
    from .dispatch import worker_name

    wo_id = wo["id"]
    tdir = turns_dir(project, wo_id)
    # The row exists before the process does: a turn that started with nothing on record
    # would run to completion with no one to reap it or capture what it said.
    turn = store.create_turn(wo_id, kind=kind, prompt=prompt, msg_id=msg_id)
    outfile = tdir / f"{turn['seq']}.json"
    errfile = tdir / f"{turn['seq']}.err"
    store.conn.execute("UPDATE wo_turns SET outfile=?, errfile=? WHERE id=?",
                       (str(outfile), str(errfile), turn["id"]))
    try:
        pid = claude_cli.spawn_turn(
            prompt=prompt,
            cwd=cwd,
            session_id=wo["session_id"],
            outfile=outfile,
            errfile=errfile,
            resume=resume,
            name=worker_name(wo),
            worktree=worktree,
            **briefing_for(project, wo),
        )
    except claude_cli.ClaudeCliError as e:
        store.finish_turn(turn["id"], "failed", error=str(e))
        store.add_event(wo_id, "turn_failed", {"seq": turn["seq"], "error": str(e)})
        raise
    store.set_turn_pid(turn["id"], pid)
    store.add_event(wo_id, "turn_started", {
        "seq": turn["seq"], "kind": kind, "pid": pid,
        "session_id": wo["session_id"], "resumed": resume,
    })
    log.info("[%s] %s turn %s for %s (pid %s)", project.name, kind, turn["seq"],
             wo_id, pid)
    return store.get_turn(turn["id"])  # type: ignore[return-value]


def _release_background_owner(store: ProjectStore, wo: dict[str, Any]) -> None:
    """Hand a session over from the background agent that owns it, if one does.

    `--resume` refuses a session a background agent still holds, and two kinds of work
    order can be in that state: one dispatched before headless turns existed, and one
    adopted from a session the user started themselves. Both are recognised the same
    way — no turn on record means Jarvis has never driven this conversation — so the
    roster is consulted exactly once per work order and never again afterwards.

    Best effort throughout: if the roster is unreachable the resume will fail loudly on
    its own, which is a better error than one raised here.
    """
    if store.latest_turn(wo["id"]) is not None:
        return  # already on the headless transport
    sid = wo.get("session_id")
    try:
        for sess in claude_cli.list_background_sessions():
            if sess.session_id and sess.session_id == sid:
                if claude_cli.stop_session(sess.id):
                    store.add_event(wo["id"], "session_released", {
                        "bg_id": sess.id, "session_id": sid,
                        "reason": "migrated from the background-session transport",
                    })
                break
    except claude_cli.ClaudeCliError as e:
        log.warning("could not check the agents roster for %s: %s", wo["id"], e)
    store.update_work_order(wo["id"], job_id=None)


def poll(store: ProjectStore) -> list[dict[str, Any]]:
    """Reap every turn whose process has ended. Returns the turns that just settled.

    Turn-level only: this decides whether a *turn* finished and records what it said.
    Deciding what that means for the *work order* is the reconciler's job, which reads
    these rows.
    """
    settled: list[dict[str, Any]] = []
    for turn in store.running_turns():
        if claude_cli.process_alive(turn["pid"]):
            continue
        if turn["pid"] is None and time.time() - turn["started_at"] < 30:
            continue  # spawned this instant; the pid write has not landed yet
        settled.append(_reap(store, turn))
    return settled


def _reap(store: ProjectStore, turn: dict[str, Any]) -> dict[str, Any]:
    wo_id = turn["wo_id"]
    result = claude_cli.read_turn_result(Path(turn["outfile"]),
                                         Path(turn["errfile"]) if turn["errfile"] else None)
    if result is None:
        error = _stderr_tail(turn) or "the turn's process ended without writing a result"
        store.add_event(wo_id, "turn_failed", {"seq": turn["seq"], "error": error[:500]})
        return store.finish_turn(turn["id"], "failed", error=error)
    # Recorded on BOTH outcomes: a failed turn's tokens were spent just the same — the
    # turn that motivated this hit a 429 having already paid $0.07 for the attempt.
    usage_json = json.dumps(result.usage) if result.usage else None
    if not result.ok:
        # A refusal for the usage limit is not a failure of the work, so it does not get
        # "Worker turn failed" in the timeline — the turn row still records state
        # `failed`, which is the plumbing truth, but the story the user reads is that
        # the OS paused and will resume itself. Emitted here, where a turn is reaped
        # exactly once, so a pause lasting hours says so once and not every tick.
        limit = claude_cli.usage_limit(result.error)
        kind = "rate_limited" if limit else "turn_failed"
        payload: dict[str, Any] = {"seq": turn["seq"], "error": result.error[:500]}
        if limit:
            payload["reset_at"] = limit.reset_at
        store.add_event(wo_id, kind, payload)
        return store.finish_turn(turn["id"], "failed", error=result.error,
                                 result=result.result or None,
                                 cost_usd=result.cost_usd, num_turns=result.num_turns,
                                 usage_json=usage_json)

    reply = result.result or _last_assistant_message(store, wo_id, turn)
    if reply:
        store.record_agent_reply(wo_id, reply)
    store.add_event(wo_id, "turn_ended", {
        "seq": turn["seq"], "chars": len(reply or ""),
        "cost_usd": result.cost_usd, "turns": result.num_turns,
    })
    return store.finish_turn(turn["id"], "done", result=reply or "",
                             cost_usd=result.cost_usd, num_turns=result.num_turns,
                             usage_json=usage_json)


def _last_assistant_message(store: ProjectStore, wo_id: str,
                            turn: dict[str, Any]) -> str:
    """Backup reply source: what the `Stop` hook saw, for a turn whose JSON came back
    with an empty `result`. The hook fires inside the session and carries the final
    message verbatim, so it is the same text by another road."""
    from . import db

    for event in reversed(store.list_events(wo_id, limit=200)):
        if event["kind"] != "hook:Stop" or event["ts"] < turn["started_at"]:
            continue
        payload = db.from_json(event.get("payload"), {}) or {}
        message = payload.get("last_assistant_message")
        if message:
            return str(message)
    return ""


def _stderr_tail(turn: dict[str, Any]) -> str:
    if not turn.get("errfile"):
        return ""
    try:
        return Path(turn["errfile"]).read_text().strip()[-1000:]
    except OSError:
        return ""


def is_stalled(turn: dict[str, Any] | None) -> bool:
    return bool(turn and turn["state"] == "running"
                and time.time() - turn["started_at"] > TURN_STALL_SECONDS)


# -- parked on the usage limit -------------------------------------------------------
#
# A turn refused for the account's usage limit did not happen: nothing was sent, nothing
# was billed, and the conversation is exactly where it was. Treating it as a failure
# settles the work order into `failed`, which is a DEPENDENCY_DEAD_STATUS, fails the
# parent feature order and puts a permanent attention flag on something that will be
# fine again at 11:50pm. So the OS does not fail it at all: the work order stays in the
# active status it already had and `Daemon.retry_rate_limited` relaunches the turn once
# the window reopens.
#
# There is NO column and NO status for this. The whole condition is re-derived from the
# latest turn every time it is asked for, which is the rule project_store.py states for
# exactly this choice ("`waiting_pr_merge` earned a status because nothing derived it;
# this does not"). Four readers share `rate_limit_pause` — the settler, message
# delivery, the retry pass and `invariants.status_label` — so none of them can drift.


@dataclass(frozen=True)
class RateLimitPause:
    """A work order waiting for the usage window to reopen."""

    #: The turn that was refused. Its `prompt` is what the retry re-sends.
    turn: dict[str, Any]
    #: When the window reopens, as the CLI stated it. None when it named no readable time.
    reset_at: float | None
    #: The earliest moment the OS will relaunch — the reset plus the minimum floor.
    retry_at: float
    #: Consecutive refusals, this one included.
    attempts: int
    #: The refusal, verbatim and squeezed onto one line.
    message: str

    @property
    def exhausted(self) -> bool:
        """Retried enough. The work order fails for real and asks for the user."""
        return self.attempts > MAX_RATE_LIMIT_RETRIES

    def due(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.retry_at


def rate_limit_pause(store: ProjectStore, wo_id: str) -> RateLimitPause | None:
    """This work order's usage-limit pause, or None if it is not in one.

    None for every ordinary failure and for every healthy conversation, so callers use
    it as the predicate itself. Costs one indexed row in the common case: the streak is
    only counted once the latest turn is already known to be a refusal.
    """
    turn = store.latest_turn(wo_id)
    if turn is None or turn["state"] != "failed":
        return None
    limit = claude_cli.usage_limit(turn.get("error"))
    if limit is None:
        return None
    ended = turn.get("ended_at") or turn["started_at"]
    when = (ended + RATE_LIMIT_FALLBACK_DELAY if limit.reset_at is None
            else limit.reset_at)
    return RateLimitPause(
        turn=turn,
        reset_at=limit.reset_at,
        retry_at=max(when, ended + RATE_LIMIT_MIN_DELAY),
        attempts=rate_limit_streak(store, wo_id),
        message=limit.message,
    )


def rate_limit_streak(store: ProjectStore, wo_id: str) -> int:
    """How many turns in a row this conversation has had refused for the limit.

    Counted off the end of the conversation rather than stored, so it resets itself the
    moment one turn gets through — which is the only definition of "recovered" that does
    not need a column someone has to remember to clear.
    """
    n = 0
    for turn in store.recent_turns(wo_id, limit=MAX_RATE_LIMIT_RETRIES + 2):
        if turn["state"] != "failed" or claude_cli.usage_limit(turn.get("error")) is None:
            break
        n += 1
    return n


def retry(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
          turn: dict[str, Any]) -> dict[str, Any]:
    """Relaunch a turn that was refused before it ran. Same prompt, same conversation.

    The prompt is read back off the turn row, which is why nothing is lost when the
    refused turn was carrying a user message: `Daemon._deliver` marks a message
    `delivered` the instant the process starts, so the turn row is the only remaining
    copy of what the worker never got to read.

    Two flags have to be re-decided rather than copied off the original turn, because a
    refusal can land on either side of both. Each is settled by asking the filesystem
    what actually exists, so the answer is a fact and not a guess about how far the CLI
    got before it gave up:

    * `--resume` vs `--session-id`: a session that was never written cannot be resumed,
      and one that WAS cannot be re-opened. The transcript file is the test.
    * `--worktree`: the flag creates the worktree, so passing it again once the
      directory exists asks for one that is already there. Once it exists the retry
      simply runs from inside it, which is what every later turn does anyway.
    """
    started = _conversation_started(project, wo)
    tree = worktree_path(project, wo)
    return _launch(
        store, project, wo, turn["prompt"], kind=turn["kind"], resume=started,
        worktree=None if tree or turn["kind"] != "dispatch" else wo.get("worktree"),
        cwd=tree or project.path, msg_id=turn.get("msg_id"),
    )


def _conversation_started(project: ProjectSpec, wo: dict[str, Any]) -> bool:
    """Has this work order's session ever been written to disk?"""
    session_id = wo.get("session_id")
    if not session_id:
        return False
    cwd = worktree_path(project, wo) or project.path
    return claude_cli.session_transcript_path(cwd, session_id).exists()


def cancel(store: ProjectStore, wo_id: str) -> dict[str, Any]:
    """Take down the work order's in-flight turn, if it has one.

    Best effort by design, exactly like the `claude stop` it replaces: the caller's
    state change (cancelling, deleting) must never hinge on a process being killable.
    """
    turn = busy(store, wo_id)
    if turn is None:
        return {"stopped": False, "reason": "no turn in flight"}
    killed = claude_cli.kill_process_group(turn["pid"])
    store.finish_turn(turn["id"], "failed",
                      error="cancelled" if killed else "cancelled (process already gone)")
    store.add_event(wo_id, "turn_cancelled", {"seq": turn["seq"], "pid": turn["pid"],
                                              "killed": killed})
    return {"stopped": killed, "pid": turn["pid"], "seq": turn["seq"]}
