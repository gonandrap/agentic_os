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

#: The backoff for a turn the TRANSPORT lost — a 500, a 529, a dropped connection. One
#: delay per attempt, in seconds, and its length is also the retry cap: five tries over
#: about thirty-nine minutes, then it becomes the user's problem.
#:
#: Unlike the usage limit, nothing here states when the API will be well again, so there
#: is no moment to wait for and the schedule has to be a guess. It is shaped like one:
#: the first two are quick because most 500s are over in seconds and a work order that
#: heals in a minute costs the user no attention at all, and the tail stretches out
#: because a fault still there after ten minutes is an outage, and hammering an outage
#: helps nobody. Claude Code already retries inside the turn before it gives up, so
#: every delay here sits on top of a burst it has lost.
TRANSIENT_BACKOFF = (60, 120, 360, 600, 1200)  # 1m, 2m, 6m, 10m, 20m


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


def _append_system_prompt(*parts: str | None) -> str:
    """Join the pieces of `--append-system-prompt` into one stable block.

    Jarvis's own git briefing comes first and the project's standing instructions
    second, so the order is fixed by construction rather than by whichever happens to
    be set: appending a project's instructions to the end of a static prefix keeps the
    common part of the prompt identical across every work order in the fleet, which is
    the same cache-prefix argument that motivates the briefing in the first place.
    """
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


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

    `append_system_prompt` is COMPOSED here rather than passed through: Jarvis's static
    git briefing plus whatever standing instructions the work order or project carry.
    The briefing exists because dispatch switches off Claude Code's own git blocks to
    keep the system prompt from moving between turns (see `_write_worker_settings`), and
    re-derivation is exactly why it has to be rebuilt here — a turn that composed it
    differently would move the prefix it was added to protect.

    `add_dirs` is the one flag that now depends on the work order's KIND: a planner also
    reaches the architect and test-lead seat definitions, and an ordinary worker does not
    (design decision 4). Because a resumed turn re-derives its flags from argv, this is
    also what keeps the seats available on the planner's second and later turns — dropping
    the kind here would make them vanish after turn 1.

    `autocompact_window` is the one flag read from the CATALOG on every turn rather than
    from the work-order row. Model, effort and permission mode are resolved onto the row
    at dispatch so a running work order keeps the briefing it started with; this is the
    opposite case on purpose. It is a spend control, not a property of the task, so
    lowering it in the catalog must reach the long-running work orders that are the
    reason to lower it — not just the ones dispatched afterwards.
    """
    from .bootstrap import install_agent_assets
    from .dispatch import _write_worker_settings
    from .worker_brief import git_briefing

    model = wo.get("model") or project.worker.model
    return {
        "model": model,
        "effort": wo.get("effort") or project.worker.effort,
        "permission_mode": (wo.get("permission_mode")
                            or project.worker.permission_mode),
        "append_system_prompt": _append_system_prompt(
            git_briefing(model),
            wo.get("append_system_prompt") or project.worker.append_system_prompt),
        "settings_file": _write_worker_settings(project, wo),
        "add_dirs": install_agent_assets(project.path, wo.get("kind") or "worker"),
        "autocompact_window": project.worker.autocompact_window,
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
        # A turn lost to the usage window or to a broken API is not a failure of the
        # work, so it does not get "Worker turn failed" in the timeline — the turn row
        # still records state `failed`, which is the plumbing truth, but the story the
        # user reads is that the OS paused and will resume itself. Emitted here, where a
        # turn is reaped exactly once, so a pause lasting hours says so once and not
        # every tick.
        #
        # Classified off the LIVE result rather than the row about to be written, which
        # is the same evidence `_diagnose` will re-read later (the row carries both
        # fields for exactly that reason) — but this is the one moment the stderr tail is
        # also in hand.
        limit = claude_cli.usage_limit(result.error)
        transient = None if limit else claude_cli.transient_failure(
            result.error, terminal_reason=result.terminal_reason,
            api_error_status=result.api_error_status)
        payload: dict[str, Any] = {"seq": turn["seq"], "error": result.error[:500]}
        if limit:
            payload |= {"reason": PAUSE_USAGE_LIMIT, "reset_at": limit.reset_at}
        elif transient:
            payload |= {"reason": PAUSE_TRANSIENT, "status": transient.status}
        store.add_event(wo_id, "turn_paused" if limit or transient else "turn_failed",
                        payload)
        return store.finish_turn(turn["id"], "failed", error=result.error,
                                 result=result.result or None,
                                 cost_usd=result.cost_usd, num_turns=result.num_turns,
                                 usage_json=usage_json,
                                 terminal_reason=result.terminal_reason,
                                 api_error_status=result.api_error_status)

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


# -- parked, and coming back by itself ------------------------------------------------
#
# TWO WAYS A TURN DIES WITHOUT THE WORK BEING WRONG, and neither is the work order's
# fault, so neither should cost the user an evening.
#
#   `usage_limit`  the account's window is spent. The turn did not happen at all:
#                  nothing was sent, nothing was billed, and the CLI says WHEN the
#                  window reopens, so the wait is a fact rather than a guess.
#   `transient`    the transport broke — a 500, a 529, a dropped connection. The turn
#                  DID happen, possibly at length, and nothing says when the API will
#                  be well again, so the wait is a backoff (`TRANSIENT_BACKOFF`).
#
# Treating either as a failure settles the work order into `failed`, which is a
# DEPENDENCY_DEAD_STATUS, fails the parent feature order and puts a permanent attention
# flag on something that will be fine again in a minute. So the OS does not fail it at
# all: the work order stays in the active status it already had and
# `Daemon.retry_paused_turns` relaunches the turn when the wait is up.
#
# WHY ONE ABSTRACTION AND NOT TWO. The pause is read in four places — the settler,
# message delivery, the retry pass and `invariants.status_label` — and every one of them
# asks the same question ("is this work order coming back by itself, and when?"). A
# second parallel predicate would mean four more call sites that could be updated one at
# a time and drift, which is the exact failure the original design called out. So there
# is ONE predicate, `turn_pause`, and the reason it returns is a field.
#
# There is still NO column and NO status for the pause itself. It is re-derived from the
# latest turn every time it is asked for, which is the rule project_store.py states for
# exactly this choice ("`waiting_pr_merge` earned a status because nothing derived it;
# this does not"). What IS stored is the turn's own diagnosis — the CLI's
# `terminal_reason` and `api_error_status` — because that is evidence, and the file it
# came from gets pruned.

#: The window is spent. Deterministic: the refusal names the moment it reopens.
PAUSE_USAGE_LIMIT = "usage_limit"
#: The API broke. Nothing names a moment, so `TRANSIENT_BACKOFF` picks one.
PAUSE_TRANSIENT = "transient"

#: What to call each reason in a sentence written for the user. Here rather than at each
#: surface so the notification, the status note and the timeline cannot end up calling
#: the same pause three different things.
PAUSE_NOUN = {
    PAUSE_USAGE_LIMIT: "usage-limit",
    PAUSE_TRANSIENT: "Claude API",
}


@dataclass(frozen=True)
class TurnPause:
    """A work order that stopped for the transport, and is coming back on its own."""

    #: `PAUSE_USAGE_LIMIT` or `PAUSE_TRANSIENT` — why it stopped, and which schedule
    #: decided `retry_at`.
    reason: str
    #: The turn that died. Its `prompt` is what a retry re-sends, when it re-sends one.
    turn: dict[str, Any]
    #: The earliest moment the OS will relaunch.
    retry_at: float
    #: Consecutive failures of this same kind, this one included.
    attempts: int
    #: The failure, verbatim and squeezed onto one line.
    message: str
    #: When the usage window reopens, as the CLI stated it. None for a transient failure,
    #: and for a limit message that named a reset in no time this parser could read.
    reset_at: float | None = None
    #: The HTTP status, for a transient failure that carried one. Display only.
    status: int | None = None

    @property
    def max_attempts(self) -> int:
        return (MAX_RATE_LIMIT_RETRIES if self.reason == PAUSE_USAGE_LIMIT
                else len(TRANSIENT_BACKOFF))

    @property
    def exhausted(self) -> bool:
        """Retried enough. The work order fails for real and asks for the user."""
        return self.attempts > self.max_attempts

    def due(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.retry_at


def turn_pause(store: ProjectStore, wo_id: str) -> TurnPause | None:
    """This work order's pause, or None if it is not in one.

    None for every ordinary failure and for every healthy conversation, so callers use
    it as the predicate itself. Costs one indexed row in the common case: the streak is
    only counted once the latest turn is already known to be one of these.

    The usage limit is tested first and `claude_cli.transient_failure` re-tests it, so a
    429 can never be read as a transient fault however this is called. That ordering is
    load-bearing: the un-retryable form of a 429 is a SPEND cap, and backing off and
    retrying one would burn five attempts on something only the user can clear.
    """
    turn = store.latest_turn(wo_id)
    if turn is None or turn["state"] != "failed":
        return None
    reason, ended = _diagnose(turn), turn.get("ended_at") or turn["started_at"]
    if reason is None:
        return None
    if isinstance(reason, claude_cli.UsageLimit):
        when = (ended + RATE_LIMIT_FALLBACK_DELAY if reason.reset_at is None
                else reason.reset_at)
        return TurnPause(
            reason=PAUSE_USAGE_LIMIT,
            turn=turn,
            reset_at=reason.reset_at,
            retry_at=max(when, ended + RATE_LIMIT_MIN_DELAY),
            attempts=pause_streak(store, wo_id, PAUSE_USAGE_LIMIT),
            message=reason.message,
        )
    attempts = pause_streak(store, wo_id, PAUSE_TRANSIENT)
    # One delay per attempt, and the last one repeats — which it only can when the
    # streak has already run past the cap, i.e. when `exhausted` is about to be true.
    delay = TRANSIENT_BACKOFF[min(max(attempts, 1), len(TRANSIENT_BACKOFF)) - 1]
    return TurnPause(
        reason=PAUSE_TRANSIENT,
        turn=turn,
        retry_at=ended + delay,
        attempts=attempts,
        message=reason.message,
        status=reason.status,
    )


def _diagnose(turn: dict[str, Any]) -> claude_cli.UsageLimit | claude_cli.TransientFailure | None:
    """Why this failed turn is worth retrying, or None if it is not.

    One place, so the pause and the streak that counts it can never disagree about what
    a given turn was. The usage limit goes first because its message is also an API
    error and would otherwise be read as one.

    A PURE FUNCTION OF THE ROW, AND `now` IS WHAT MAKES IT ONE. The refusal states a
    wall-clock time ("resets 12pm") or a countdown ("resets in 2h 15m"), and both are
    statements made at the moment the turn ENDED. Resolving them against the clock of
    whichever pass happens to be asking is what cost wo-b4f207ad twelve hours: at 12:00
    exactly, `claude_cli._reset_moment`'s "that time has gone, so it means tomorrow"
    rule starts firing, so every subsequent pass pushed the deadline another day out
    and `TurnPause.due` was never true — the deadline outran the pass chasing it, for
    ever. The countdown form was worse still: `now + 2h15m` on every pass is a moment
    that can never arrive. Anchored here instead, the answer is fixed the moment the
    turn is settled and no later reader can move it.
    """
    error = turn.get("error")
    limit = claude_cli.usage_limit(error, now=turn.get("ended_at") or turn["started_at"])
    if limit is not None:
        return limit
    return claude_cli.transient_failure(
        error,
        terminal_reason=turn.get("terminal_reason"),
        api_error_status=turn.get("api_error_status"),
    )


def pause_streak(store: ProjectStore, wo_id: str, reason: str) -> int:
    """How many turns in a row this conversation has lost to `reason`.

    Counted off the end of the conversation rather than stored, so it resets itself the
    moment one turn gets through — which is the only definition of "recovered" that does
    not need a column someone has to remember to clear.

    The count stops at the first turn that was anything ELSE, a different pause reason
    included: a conversation that hit the usage limit and then hit a 500 is on its first
    500, and charging it the limit's attempts would cut its backoff short.
    """
    want = claude_cli.UsageLimit if reason == PAUSE_USAGE_LIMIT else claude_cli.TransientFailure
    n = 0
    for turn in store.recent_turns(wo_id, limit=MAX_RATE_LIMIT_RETRIES + 2):
        if turn["state"] != "failed" or not isinstance(_diagnose(turn), want):
            break
        n += 1
    return n


def retry(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
          pause: TurnPause) -> dict[str, Any]:
    """Relaunch a turn the transport lost. Same conversation, and usually same prompt.

    WHAT IT RE-SENDS DEPENDS ON WHETHER THE TURN EVER REACHED THE MODEL (Neo, question
    126). The prompt is read back off the turn row, which is why nothing is lost when the
    lost turn was carrying a user message: `Daemon._deliver` marks a message `delivered`
    the instant the process starts, so the turn row is the only remaining copy of what
    the worker never got to read. Re-sending it verbatim is exactly right for a turn that
    was refused BEFORE it ran — the conversation is untouched and the worker has still
    never seen the message.

    It is wrong for a turn that died in flight. Verified on wo-4f460495, the work order
    that motivated this: its turn took a 500 after 350 seconds, and the session transcript
    holds the prompt followed by ~55 messages and $1.99 of real work. Re-sending the
    prompt there would put the same user message into the conversation a second time and
    invite the worker to redo everything it had already done. So when the turn reached the
    model, the retry resumes with a short continuation nudge instead (`_nudge`) and lets
    the transcript speak for what was already said.

    The test is `duration_api_ms`, which the CLI reports on the failed envelope too: a
    turn refused before it reached the API records 0 there, and a turn that got any
    distance records the time it spent. It is read from the stored usage envelope, so it
    outlives the result file.

    THE PAUSE REASON IS NOT A PROXY FOR THAT, and an earlier version of this comment
    claimed it was — that a usage-limit refusal always records 0, "verified across every
    usage-limit refusal the fleet has recorded". That is false, and the fleet has since
    disproved it comprehensively: on 2026-08-22 five work orders were refused for the
    session limit MID-TURN, after real work, recording 223,944ms / 433,119ms /
    545,416ms / 1,003,519ms / 1,150,038ms of API time and $1.69–$15.80 of spend apiece.
    The window can close under a conversation that is already running, not only in front
    of one about to start. Reading the measurement rather than trusting the reason is
    what makes this correct anyway — do not "simplify" it back to a check on
    `pause.reason`, which would re-send a prompt those five workers had already acted on.

    Two flags have to be re-decided rather than copied off the original turn, because a
    failure can land on either side of both. Each is settled by asking the filesystem
    what actually exists, so the answer is a fact and not a guess about how far the CLI
    got before it gave up:

    * `--resume` vs `--session-id`: a session that was never written cannot be resumed,
      and one that WAS cannot be re-opened. The transcript file is the test.
    * `--worktree`: the flag creates the worktree, so passing it again once the
      directory exists asks for one that is already there. Once it exists the retry
      simply runs from inside it, which is what every later turn does anyway.
    """
    turn = pause.turn
    started = _conversation_started(project, wo)
    tree = worktree_path(project, wo)
    # Both halves are required. A turn can bill API time and still leave no session to
    # resume (the opening turn, dying before the transcript lands), and nudging a
    # conversation that does not exist would open one whose entire content is the nudge.
    prompt = _nudge(pause) if started and _reached_model(turn) else turn["prompt"]
    return _launch(
        store, project, wo, prompt, kind=turn["kind"], resume=started,
        worktree=None if tree or turn["kind"] != "dispatch" else wo.get("worktree"),
        cwd=tree or project.path, msg_id=turn.get("msg_id"),
    )


def _reached_model(turn: dict[str, Any]) -> bool:
    """Did this turn get as far as the API before it died?

    False when there is no usage envelope at all: a turn reaped before that column
    existed, or one that crashed without writing a result. Both are better served by the
    verbatim re-send, which is the behaviour that was already there.
    """
    from . import db

    usage = db.from_json(turn.get("usage_json"), None)
    if not isinstance(usage, dict):
        return False
    return (usage.get("duration_api_ms") or 0) > 0


def _nudge(pause: TurnPause) -> str:
    """What a worker is told when its own turn is relaunched under it.

    Addressed to the worker, not the user, and it has one job: make clear that the
    conversation above is intact and that the interruption was the transport, so the
    worker continues instead of starting again. It says what happened because a worker
    that can see the gap in its own transcript will otherwise spend a turn working out
    what it missed.
    """
    what = ("Claude's usage limit was reached" if pause.reason == PAUSE_USAGE_LIMIT
            else f"the Claude API failed ({pause.message})")
    return (
        f"[Jarvis] Your previous turn was cut short because {what}. This was the "
        "transport, not anything you or the work did, and nothing you had already "
        "done was lost — the conversation above is intact and is where you left off. "
        "Carry on from there and finish that turn. Do not start again and do not "
        "repeat work that is already done; re-check the state on disk first if you "
        "are unsure how far you got."
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
