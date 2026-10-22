"""The conversation layer: how a message reaches a working session"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import background, budget, claude_cli, context, systemd_units, usage
from .catalog import ProjectSpec
from .project_store import (COMPACT_TURN, COST_FROM_TRANSCRIPT, TERMINAL_STATUSES,
                            ProjectStore)

log = logging.getLogger("jarvisd")

#: A turn still running after this long is reported
TURN_STALL_SECONDS = 6 * 3600

#: How long to wait before retrying a turn whose limit message named a reset but no time this parser...
RATE_LIMIT_FALLBACK_DELAY = 15 * 60

#: The floor under every retry, measured from the refusal
RATE_LIMIT_MIN_DELAY = 60

#: How many consecutive turns of ONE conversation may be refused for the limit before the OS stops...
MAX_RATE_LIMIT_RETRIES = 8

#: The backoff for a turn the TRANSPORT lost — a 500, a 529, a dropped connection
TRANSIENT_BACKOFF = (60, 120, 360, 600, 1200)  # 1m, 2m, 6m, 10m, 20m


def new_session_id() -> str:
    """Mint the session id for a work order"""
    return str(uuid.uuid4())


def transport_of(project: ProjectSpec, wo: dict[str, Any]) -> str:
    """The transport this order's turns run on: the one its session was born on, else the catalog's"""
    if wo.get("transport"):
        return str(wo["transport"])
    # A session that predates the column was born headless
    if wo.get("session_id"):
        return claude_cli.TRANSPORT_HEADLESS
    return project.worker.transport


def turns_dir(project: ProjectSpec, wo_id: str) -> Path:
    return project.path / ".jarvis" / "turns" / wo_id


def worktree_path(project: ProjectSpec, wo: dict[str, Any]) -> Path | None:
    wt = wo.get("worktree")
    if not wt:
        return None
    path = project.path / ".claude" / "worktrees" / wt
    return path if path.is_dir() else None


def _append_system_prompt(*parts: str | None) -> str:
    """Join the pieces of `--append-system-prompt` into one stable block"""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def feature_agent(store: ProjectStore | None, project: ProjectSpec,
                  wo: dict[str, Any]) -> tuple[str | None, Path | None]:
    """This child's per-feature agent type: its name and the directory that supplies it (docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md)"""
    from . import specs

    if store is None or not wo.get("parent_id") or wo.get("kind") != "worker":
        return None, None
    plan = specs.plan_of(store, wo["parent_id"])
    content = str(plan.get("design_doc_content") or "")
    if not content:
        return None, None
    name = specs.install_agent(project.path, wo["parent_id"],
                               str(plan.get("summary") or wo.get("title") or ""), content)
    if not name:
        return None, None
    return name, specs.agent_root(project.path, wo["parent_id"])


def briefing_for(project: ProjectSpec, wo: dict[str, Any],
                 store: ProjectStore | None = None,
                 central: Any = None) -> dict[str, Any]:
    """The flags every turn of this work order is launched with (#42)"""
    from . import wiring
    from .bootstrap import install_agent_assets
    from .dispatch import _write_worker_settings
    from .worker_brief import git_briefing

    model = wo.get("model") or project.worker.model
    agent, agent_dir = feature_agent(store, project, wo)
    cap = budget.ceiling(store, central, wo) if store is not None else None
    return {
        "model": model,
        "agent": agent,
        "effort": wo.get("effort") or project.worker.effort,
        "permission_mode": (wo.get("permission_mode")
                            or project.worker.permission_mode),
        "append_system_prompt": _append_system_prompt(
            git_briefing(model),
            wo.get("append_system_prompt") or project.worker.append_system_prompt),
        "settings_file": _write_worker_settings(project, wo),
        "add_dirs": (install_agent_assets(project.path, wo.get("kind") or "worker",
                                          serena=wiring.serena_wired(project.wiring))
                     + ([agent_dir] if agent_dir else [])),
        "autocompact_window": project.worker.autocompact_window,
        # Never negative: `_launch` refuses a spent order outright
        "max_budget_usd": max(0.0, cap.remaining_usd) if cap else None,
    }


def busy(store: ProjectStore, wo_id: str) -> dict[str, Any] | None:
    """The work order's in-flight turn, or None"""
    turn = store.latest_turn(wo_id)
    return turn if turn and turn["state"] == "running" else None


#: `DeliveryHold.kind`, so a caller could branch on the hold without matching its prose
HOLD_NO_SESSION = "no_session"
HOLD_BUDGET = "budget"
HOLD_TURN_IN_FLIGHT = "turn_in_flight"
HOLD_RETRY_BOOKED = "retry_booked"
HOLD_RETRY_OVERDUE = "retry_overdue"
#: Per MESSAGE, not per work order, and the only one of these that is: see `message_hold`
HOLD_OBJECTION_PARKED = "objection_parked"


@dataclass(frozen=True)
class DeliveryHold:
    """Why `Daemon.deliver_messages` will not start a turn for this work order now"""

    kind: str
    accounted: bool
    reason: str


def delivery_hold(store: ProjectStore, wo: dict[str, Any],
                  now: float | None = None) -> DeliveryHold | None:
    """The hold on this work order's queued messages, or None if delivery may go"""
    if wo["status"] == budget.EXHAUSTED:
        # Delivering would launch a turn there is no money for, and `_launch` would refuse it anyway
        return DeliveryHold(HOLD_BUDGET, accounted=True,
                            reason="its budget is spent — raise it and the message goes")
    if not wo.get("session_id"):
        # Nothing carries a queued message into a dispatch prompt
        return DeliveryHold(HOLD_NO_SESSION, accounted=False,
                            reason="it has no session to resume")
    if busy(store, wo["id"]):
        # Mid-turn: one turn at a time, and `--resume` would refuse anyway
        return DeliveryHold(HOLD_TURN_IN_FLIGHT, accounted=True,
                            reason="a turn is already in flight")
    pause = turn_pause(store, wo["id"])
    if pause is None or not pause.resumable:
        return None
    # The lost turn has to go out first
    noun = PAUSE_NOUN[pause.reason]
    if not pause.due(now=now):
        return DeliveryHold(HOLD_RETRY_BOOKED, accounted=True,
                            reason=f"its {noun} retry is booked and has not come due")
    # Past its own deadline and still here: the relaunch `Daemon.retry_paused_turns` owes did not happen
    return DeliveryHold(HOLD_RETRY_OVERDUE, accounted=False,
                        reason=f"its {noun} retry came due and has not happened")


#: Why a queued objection waits, in one clause read after "because"
OBJECTION_PARKED_REASON = ("the OS objected and the worker is not running — it goes out "
                           "with the order's next turn")


def message_hold(store: ProjectStore, wo: dict[str, Any], msg: dict[str, Any],
                 now: float | None = None) -> DeliveryHold | None:
    """The hold on THIS message, or None if delivery may go — `delivery_hold` plus one"""
    status = str(wo["status"] or "")
    if (status != "running" and status not in TERMINAL_STATUSES
            and status != "needs_review"):
        envelope = store.envelope_for_message(int(msg["id"]))
        if envelope and envelope["kind"] == "assumption_objection":
            return DeliveryHold(HOLD_OBJECTION_PARKED, accounted=True,
                                reason=OBJECTION_PARKED_REASON)
    # Every existing per-order hold, through the one call, so none is re-derived
    return delivery_hold(store, wo, now=now)


def start(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
          prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Open the conversation: turn 1, in a fresh worktree, under a minted session id (docs/superpowers/specs/2026-09-24-order-observability.md §5)"""
    wo_id = wo["id"]
    transport = transport_of(project, wo)
    # A background session's id is minted by the CLI and learned at launch (fact 1)
    session_id = wo.get("session_id") or (
        None if transport == claude_cli.TRANSPORT_BACKGROUND else new_session_id())
    store.update_work_order(wo_id, session_id=session_id, worktree=wo_id,
                            transport=transport)
    row = {**wo, "session_id": session_id, "worktree": wo_id, "transport": transport}
    # RE-DECIDED FROM THE FILESYSTEM
    started = _conversation_started(project, row)
    tree = worktree_path(project, row)
    briefing: dict[str, Any] = {}
    turn = _launch(store, project, row, prompt, kind="dispatch", resume=started,
                   worktree=None if tree else wo_id, cwd=tree or project.path,
                   briefing_out=briefing)
    return turn, briefing


def send(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any], text: str,
         msg_id: int | None = None) -> dict[str, Any]:
    """Deliver a message as the work order's next turn"""
    _release_background_owner(store, wo)
    cwd = worktree_path(project, wo) or project.path
    return _launch(store, project, wo, text, kind="message", resume=True,
                   worktree=None, cwd=cwd, msg_id=msg_id)


# -- compaction: the one boundary the OS pays for on purpose


@dataclass(frozen=True)
class Compaction:
    """Why the OS is about to compact this conversation before its next prompt"""

    #: Seconds since the previous turn ENDED
    age: float
    #: The conversation's size as measured (#885)
    context: int | None

    @property
    def why(self) -> str:
        size = (f"the conversation is {self.context:,} tokens"
                if self.context is not None
                else "the conversation's size could not be read")
        return (f"the prompt cache expired {int(self.age // 60)}m ago and {size} — "
                f"re-sending it would cost more than summarising it")


def turn_context(turn: dict[str, Any] | None) -> int:
    """How large the conversation was at the end of a turn, or 0 if unrecorded"""
    from . import db

    envelope = db.from_json((turn or {}).get("usage_json"), None)
    return int(envelope.get("context_peak") or 0) if isinstance(envelope, dict) else 0


def measured_context(store: ProjectStore, wo_id: str) -> int | None:
    """The conversation's size as last MEASURED, or None when it is UNKNOWN (#856)"""
    for turn in store.recent_turns(wo_id, limit=MAX_RATE_LIMIT_RETRIES + 4):
        # A compaction's own prompt is the PRE-compaction peak; what it left is the post size
        achieved = compacted_to(turn)
        if achieved:
            return achieved
        context = turn_context(turn)
        if context:
            return context
    return _transcript_context(store, wo_id)


def compaction_record(turn: dict[str, Any] | None) -> dict[str, Any] | None:
    """`wo_turns.compaction_json` decoded: a boundary, `{}` for none, None if never read"""
    from . import db

    record = db.from_json((turn or {}).get("compaction_json"), None)
    return record if isinstance(record, dict) else None


def compacted_to(turn: dict[str, Any] | None) -> int:
    """The size a compaction turn left the conversation at, or 0 if it left no boundary"""
    if not turn or turn.get("kind") != COMPACT_TURN:
        return 0
    return int((compaction_record(turn) or {}).get("post") or 0)


def compaction_landed(store: ProjectStore, wo: dict[str, Any],
                      turn: dict[str, Any]) -> bool:
    """Did this compaction turn leave a boundary in the session?"""
    if compaction_record(turn):
        return True
    session_id = str(wo.get("session_id") or "")
    if not session_id:
        return False
    # Read again even when `{}` was stored: a turn that died without an envelope was never asked
    found = usage.last_compaction(session_id, since=turn["started_at"])
    if found is not None:
        store.set_turn_compaction(turn["id"], found)
    return found is not None


def _transcript_context(store: ProjectStore, wo_id: str) -> int | None:
    """The size of the last BILLED call in the session transcript (issue #885)"""
    session_id = store.get_work_order(wo_id).get("session_id")
    if not session_id:
        return 0
    try:
        index = usage.index_sessions()
        if session_id not in index:
            return 0
        calls = usage.session_calls(session_id, index=index)
    except OSError:
        return None
    for call in reversed(calls):
        if call.context:
            return call.context
    return 0


def compaction_due(store: ProjectStore, wo: dict[str, Any],
                   min_context: int | None,
                   now: float | None = None,
                   pause: "TurnPause | None" = None) -> Compaction | None:
    """Should the OS compact before this work order's next prompt goes out? (#843)"""
    if min_context is None:
        return None
    turn = pause.turn if pause is not None else store.latest_turn(wo["id"])
    if turn is None or turn["kind"] == COMPACT_TURN:
        return None
    if pause is None and turn["state"] == "running":
        return None
    # The paused turn is anchored on `started_at` when it never ended: a turn refused before it ran...
    ended = (turn.get("ended_at") or turn["started_at"] if pause is not None
             else turn.get("ended_at"))
    if not ended:
        return None
    age = (time.time() if now is None else now) - ended
    if age < usage.WRITE_TTL_SECONDS:
        return None
    context = measured_context(store, wo["id"])
    # An UNKNOWN size skips the floor rather than failing it: the floor exists to stop a small... (#885)
    if context is not None and context < min_context:
        return None
    # Last, because it is the only one that costs a second query
    if pause is None and turn["state"] == "failed":
        held = turn_pause(store, wo["id"])
        if held is not None and held.resumable:
            return None
    return Compaction(age=age, context=context)


def compact_before_relaunch(store: ProjectStore, project: ProjectSpec,
                            wo: dict[str, Any], min_context: int | None, *,
                            pause: "TurnPause | None" = None,
                            queue: str | None = None,
                            now: float | None = None,
                            queued_out: dict[str, Any] | None = None,
                            ) -> dict[str, Any] | None:
    """The ONE compaction decision, for every path that relaunches a conversation"""
    assert pause is None or queue is None, "a relaunch is either a pause or a prompt"
    due = compaction_due(store, wo, min_context, now=now, pause=pause)
    if due is None:
        return None
    if pause is not None:
        return compact_then_resume(store, project, wo, pause, due)
    if queue is not None:
        return _compact_after_queueing(store, project, wo, queue, due,
                                       queued_out=queued_out)
    return compact(store, project, wo, due)


def compact(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
            due: Compaction) -> dict[str, Any]:
    """Summarise the conversation, as a turn of its own, before the next prompt"""
    _release_background_owner(store, wo)
    cwd = worktree_path(project, wo) or project.path
    store.add_event(wo["id"], "compacting", {
        "reason": due.why, "context": due.context, "idle_seconds": round(due.age),
    })
    return _launch(store, project, wo, claude_cli.COMPACT_PROMPT, kind=COMPACT_TURN,
                   resume=True, worktree=None, cwd=cwd)


#: The source a relaunch queued behind a compaction carries (`compact_then_resume`) (docs/superpowers/specs/2026-09-29-one-compaction-decision-on-every-relaunch.md §3.5)
RESUME_SOURCE = "relaunch"


def compact_then_resume(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
                        pause: "TurnPause", due: Compaction) -> dict[str, Any]:
    """Queue the paused turn's relaunch, then compact; delivery sends it afterwards"""
    turn = pause.turn
    prompt = (_nudge_after_compaction(pause) if _reached_model(turn)
              else turn["prompt"])
    msg_id = store.queue_message(wo["id"], prompt, source=RESUME_SOURCE)
    store.add_event(wo["id"], "resume_compacting", {
        "retried_seq": turn["seq"], "msg_id": msg_id, "reason": pause.reason,
        "context": due.context, "idle_seconds": round(due.age),
    })
    return _compact_or_withdraw(store, project, wo, due, msg_id)


def _compact_after_queueing(store: ProjectStore, project: ProjectSpec,
                            wo: dict[str, Any], prompt: str, due: Compaction,
                            queued_out: dict[str, Any] | None = None,
                            ) -> dict[str, Any]:
    """Queue a prompt that was about to be LAUNCHED, then compact in front of it"""
    msg_id = store.queue_message(wo["id"], prompt, source=RESUME_SOURCE)
    if queued_out is not None:
        queued_out["msg_id"] = msg_id
    # No event of its own: `compact` writes `compacting`
    return _compact_or_withdraw(store, project, wo, due, msg_id)


def _compact_or_withdraw(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
                         due: Compaction, msg_id: int) -> dict[str, Any]:
    """Compact, and take the queued relaunch back if the compaction cannot launch (#843)"""
    try:
        return compact(store, project, wo, due)
    except Exception:
        store.mark_message(msg_id, "failed")
        raise


#: `compacted.outcome`: what a compaction turn did
COMPACTION_DONE = "compacted"
COMPACTION_DIED = "died"
COMPACTION_NOTHING = "nothing_compacted"
COMPACTION_UNVERIFIED = "unverified"

#: The error a compaction turn settles `failed` with when it returned ok and left no boundary
NOTHING_COMPACTED = ("the compaction returned success but left no compact boundary in "
                     "the transcript — nothing was summarised")


def _record_compaction(store: ProjectStore, project_name: str, wo_id: str,
                       turn: dict[str, Any], result: claude_cli.TurnResult) -> str:
    """Put the compaction's outcome on the timeline and its cost on the bill, and return it"""
    from . import agent_usage

    session_id = str(store.get_work_order(wo_id).get("session_id") or "")
    agent_usage.record(agent_usage.COMPACTION, usage=result.usage, project=project_name,
                       wo_id=wo_id, label=f"turn {turn['seq']}",
                       model=wo_model(store, wo_id), session_id=session_id,
                       ok=result.ok)
    try:
        done = usage.last_compaction(session_id, since=turn["started_at"])
    except OSError:
        # An unreadable transcript is no verdict: nothing is recorded and the envelope decides
        done, readable = None, False
    else:
        readable = True
        store.set_turn_compaction(turn["id"], done)
    outcome = (COMPACTION_DIED if not result.ok
               else COMPACTION_DONE if done is not None
               else COMPACTION_NOTHING if readable else COMPACTION_UNVERIFIED)
    store.add_event(wo_id, "compacted", {
        "seq": turn["seq"],
        "ok": outcome in (COMPACTION_DONE, COMPACTION_UNVERIFIED),
        "outcome": outcome,
        # The CLI's own measurement of what it achieved, not an estimate of ours
        "before": (done or {}).get("pre"),
        "after": (done or {}).get("post"),
        "cost_usd": result.cost_usd,
        "error": (NOTHING_COMPACTED if outcome == COMPACTION_NOTHING
                  else (result.error or "")[:500] or None),
    })
    return outcome


def wo_model(store: ProjectStore, wo_id: str) -> str:
    try:
        return str(store.get_work_order(wo_id).get("model") or "")
    except KeyError:
        return ""


def _launch(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any], prompt: str,
            kind: str, resume: bool, worktree: str | None, cwd: Path,
            msg_id: int | None = None,
            briefing_out: dict[str, Any] | None = None) -> dict[str, Any]:
    from .central_store import CentralStore
    from .dispatch import cache_ttl_env, worker_name

    wo_id = wo["id"]
    # THE ONE POINT EVERY TURN PASSES THROUGH
    central = CentralStore()
    try:
        # The LAUNCH floor as well as the cap: a remainder under one call buys a turn that dies on its first...
        out = budget.launch_refusal(store, central, wo)
        if out is not None:
            raise budget.BudgetExhausted(out)
        briefing = briefing_for(project, wo, store, central)
    finally:
        central.close()
    tdir = turns_dir(project, wo_id)
    # The row exists before the process does: a turn that started with nothing on record would run to... (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
    arm = project.worker.cache_ttl
    turn = store.create_turn(wo_id, kind=kind, prompt=prompt, msg_id=msg_id,
                             cache_ttl=arm)
    outfile = tdir / f"{turn['seq']}.json"
    errfile = tdir / f"{turn['seq']}.err"
    store.conn.execute("UPDATE wo_turns SET outfile=?, errfile=? WHERE id=?",
                       (str(outfile), str(errfile), turn["id"]))
    try:
        if is_background(wo):
            wo = _launch_bg(store, wo, prompt, cwd, resume, worktree,
                            outfile.with_suffix(".prompt"), briefing)
            spawned = claude_cli.SpawnedTurn(pid=None)
        else:
            spawned = claude_cli.spawn_turn(
                prompt=prompt,
                cwd=cwd,
                session_id=wo["session_id"],
                outfile=outfile,
                errfile=errfile,
                resume=resume,
                # Named from the row that already exists
                unit=systemd_units.unit_name(wo_id, turn["seq"]),
                cache=cache_ttl_env(arm),
                name=worker_name(wo),
                worktree=worktree,
                **briefing,
            )
    except claude_cli.ClaudeCliError as e:
        store.finish_turn(turn["id"], "failed", error=str(e))
        store.add_event(wo_id, "turn_failed", {"seq": turn["seq"], "error": str(e)})
        raise
    store.set_turn_pid(turn["id"], spawned.pid, unit=spawned.unit)
    store.add_event(wo_id, "turn_started", {
        "seq": turn["seq"], "kind": kind, "pid": spawned.pid, "unit": spawned.unit,
        "session_id": wo["session_id"], "resumed": resume,
        # Where the branch stood when this turn began (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md)
        "head": _head_sha(cwd),
        # The arm this turn was launched on, for the timeline
        "cache_ttl": arm,
    })
    log.info("[%s] %s turn %s for %s (pid %s%s)", project.name, kind, turn["seq"],
             wo_id, spawned.pid, f", unit {spawned.unit}" if spawned.unit else "")
    fresh = store.get_turn(turn["id"])
    # `start` asks for the briefing back through this dict — see its docstring
    if briefing_out is not None:
        briefing_out.update(briefing)
    # THE CONTEXT LEDGER, for every turn EXCEPT the seq-1 dispatch turn — Neo's ruling on question 681 (docs/superpowers/specs/2026-09-24-order-observability.md §5)
    if kind != "dispatch" or turn["seq"] > 1:
        context.record(store, project, wo, fresh, briefing)
    return fresh  # type: ignore[return-value]


def is_background(wo: dict[str, Any]) -> bool:
    """Does this order's conversation live in a background session? (recorded at its first launch)"""
    return wo.get("transport") == claude_cli.TRANSPORT_BACKGROUND


def _launch_bg(store: ProjectStore, wo: dict[str, Any], prompt: str, cwd: Path,
               resume: bool, worktree: str | None, promptfile: Path,
               briefing: dict[str, Any]) -> dict[str, Any]:
    """One prompt into the order's background session — the first opens it (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""
    if resume:
        # The briefing rides on the session's saved options and the settings file `briefing_for` rewrote
        claude_cli.resume_bg_turn(wo["session_id"], prompt, cwd, promptfile=promptfile)
        return wo
    if wo.get("bg_id"):
        # A session that never wrote a word: retire it rather than leave two
        claude_cli.stop_session(wo["bg_id"])
    entry = claude_cli.spawn_bg_turn(prompt, cwd, _worker_name(wo), worktree=worktree,
                                     promptfile=promptfile, **briefing)
    store.update_work_order(wo["id"], session_id=entry.session_id, bg_id=entry.id)
    return {**wo, "session_id": entry.session_id, "bg_id": entry.id}


def _worker_name(wo: dict[str, Any]) -> str:
    from .dispatch import worker_name
    return worker_name(wo)


def _head_sha(cwd: Path | None) -> str:
    """`git rev-parse HEAD` in the directory the turn runs in, or "" (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md)"""
    from . import branchproof

    if cwd is None:
        return ""
    return (branchproof.run(Path(cwd), "rev-parse", "HEAD") or "").strip()


def _release_background_owner(store: ProjectStore, wo: dict[str, Any]) -> None:
    """Hand a session over from the background agent that owns it, if one does"""
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


def poll(store: ProjectStore, project_name: str = "") -> list[dict[str, Any]]:
    """Reap every turn whose process has ended"""
    settled: list[dict[str, Any]] = []
    roster: dict[str, claude_cli.BgEntry] | None = None
    roster_failed = False
    for turn in store.running_turns():
        try:
            wo = store.get_work_order(turn["wo_id"])
        except KeyError:
            wo = {}
        if is_background(wo):
            if roster is None and not roster_failed:
                try:
                    roster = claude_cli.bg_roster()
                except claude_cli.ClaudeCliError as e:
                    # No roster is no verdict: an empty one would read every session as dead
                    log.warning("could not read `claude agents` (%s); background turns "
                                "wait for the next poll", e)
                    roster_failed = True
            if roster is None:
                continue
            done = _poll_bg(store, wo, turn, roster, project_name)
            if done is not None:
                settled.append(done)
            continue
        if claude_cli.process_alive(turn["pid"]):
            continue
        if turn["pid"] is None and time.time() - turn["started_at"] < 30:
            continue  # spawned this instant; the pid write has not landed yet
        if _unit_still_running(turn):
            continue
        settled.append(_reap(store, turn, project_name))
    return settled


def _unit_still_running(turn: dict[str, Any]) -> bool:
    """Liveness of last resort, for a turn whose transient unit started but whose main pid could not be..."""
    unit = turn.get("unit")
    return bool(unit) and turn["pid"] is None and systemd_units.unit_active(unit)


def _reap(store: ProjectStore, turn: dict[str, Any],
          project_name: str = "") -> dict[str, Any]:
    wo_id = turn["wo_id"]
    result = claude_cli.read_turn_result(
        Path(turn["outfile"]),
        Path(turn["errfile"]) if turn["errfile"] else None,
        previous=store.turn_usage_before(wo_id, turn["seq"]))
    # The turn's prompt file, if the brief was too big for argv (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)
    if turn["outfile"]:
        Path(turn["outfile"]).with_suffix(".prompt").unlink(missing_ok=True)
    if result is None:
        error = (_stderr_tail(turn) or _transcript_error(store, wo_id, turn)
                 or NO_RESULT)
        payload: dict[str, Any] = {"seq": turn["seq"], "error": error[:500]}
        auth = claude_cli.auth_failure(error)
        if auth:
            payload |= {"reason": PAUSE_AUTH}
        store.add_event(wo_id, "turn_paused" if auth else "turn_failed", payload)
        # The envelope is what `cost_usd` normally comes from and there is none
        cost = lost_turn_cost(store, wo_id, turn)
        settled = store.finish_turn(turn["id"], "failed", error=error, cost_usd=cost,
                                    cost_source=COST_FROM_TRANSCRIPT)
        if not auth:
            # WHAT THE TURN LEFT ON DISK, on the `turn_failed` outcome alone (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md)
            try:
                from . import harvest

                harvest.write(store, store.get_work_order(wo_id), turn,
                              said=_last_assistant_message(store, wo_id, turn))
            # noqa below: the harvest is best effort, the settlement above is not.
            except Exception as exc:  # noqa: BLE001
                log.warning("could not harvest turn %s of %s: %s", turn["seq"], wo_id,
                            exc)
        return settled
    return _settle_result(store, turn, result, project_name)


def _settle_result(store: ProjectStore, turn: dict[str, Any],
                   result: claude_cli.TurnResult,
                   project_name: str = "") -> dict[str, Any]:
    """Settle a turn against what it returned, whichever transport it came back on"""
    wo_id = turn["wo_id"]
    # Recorded on BOTH outcomes: a failed turn's tokens were spent just the same
    usage_json = json.dumps(result.usage) if result.usage else None
    # A background turn has no envelope: its dollars were read off the transcript
    source = (COST_FROM_TRANSCRIPT
              if ((result.usage or {}).get("reported") or {}).get("source") == "transcript"
              else None)
    if not result.ok:
        # A turn lost to the usage window or to a broken API is not a failure of the work
        overspent = claude_cli.stopped_for_budget(result)
        auth = None if overspent else claude_cli.auth_failure(result.error)
        limit = None if overspent or auth else claude_cli.usage_limit(result.error)
        transient = None if overspent or auth or limit else claude_cli.transient_failure(
            result.error, terminal_reason=result.terminal_reason,
            api_error_status=result.api_error_status)
        if overspent:
            store.add_event(wo_id, "turn_budget_exhausted",
                            {"seq": turn["seq"], "cost_usd": result.cost_usd,
                             "error": result.error[:500]})
            return store.finish_turn(turn["id"], "failed", error=result.error,
                                     result=result.result or None,
                                     cost_usd=result.cost_usd,
                                     num_turns=result.num_turns,
                                     usage_json=usage_json,
            cost_source=source,
                                     terminal_reason=result.terminal_reason,
                                     api_error_status=result.api_error_status)
        payload = {"seq": turn["seq"], "error": result.error[:500]}
        if auth:
            payload |= {"reason": PAUSE_AUTH}
        elif limit:
            payload |= {"reason": PAUSE_USAGE_LIMIT, "reset_at": limit.reset_at}
        elif transient:
            payload |= {"reason": PAUSE_TRANSIENT, "status": transient.status}
        store.add_event(
            wo_id, "turn_paused" if auth or limit or transient else "turn_failed",
            payload)
        if turn["kind"] == COMPACT_TURN:
            # A compaction that died still spent what it spent
            _record_compaction(store, project_name, wo_id, turn, result)
        return store.finish_turn(turn["id"], "failed", error=result.error,
                                 result=result.result or None,
                                 cost_usd=result.cost_usd, num_turns=result.num_turns,
                                 usage_json=usage_json,
            cost_source=source,
                                 terminal_reason=result.terminal_reason,
                                 api_error_status=result.api_error_status)

    if turn["kind"] == COMPACT_TURN:
        # NOT A REPLY, AND NOT THE WORKER'S
        outcome = _record_compaction(store, project_name, wo_id, turn, result)
        # SETTLED AGAINST ITS POST-CONDITION: ok with no boundary did not happen, and `turn_pause` retries it
        failed = outcome == COMPACTION_NOTHING
        return store.finish_turn(turn["id"], "failed" if failed else "done", result="",
                                 error=NOTHING_COMPACTED if failed else None,
                                 cost_usd=result.cost_usd,
                                 num_turns=result.num_turns, usage_json=usage_json,
            cost_source=source)

    reply = result.result or _last_assistant_message(store, wo_id, turn)
    msg_id = store.record_agent_reply(wo_id, reply) if reply else None
    # WHAT THE TURN LEFT RUNNING, asked here because this is the one place a turn is settled exactly once... (docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md)
    orphaned = background.orphaned_in_turn(store, wo_id, turn)
    if orphaned:
        background.record(store, wo_id, turn, orphaned, msg_id)
    store.add_event(wo_id, "turn_ended", {
        "seq": turn["seq"], "chars": len(reply or ""),
        "cost_usd": result.cost_usd, "turns": result.num_turns,
    })
    return store.finish_turn(turn["id"], "done", result=reply or "",
                             cost_usd=result.cost_usd, num_turns=result.num_turns,
                             usage_json=usage_json,
            cost_source=source)


# -- a turn in a background session (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)

#: How long an idle session that has not answered yet is given to pick its prompt up
BG_IDLE_GRACE = 60.0

#: How long a session missing its process is given to register one before the turn is lost
BG_DEAD_GRACE = 10.0

#: How often a running background turn's spend is read against its cap
BG_BUDGET_EVERY = 60.0

#: What a background turn lost with its process is recorded as
SESSION_LOST_ERROR = ("the worker's background session process ended before the turn "
                      "settled")

_budget_checked: dict[int, float] = {}


def _poll_bg(store: ProjectStore, wo: dict[str, Any], turn: dict[str, Any],
             roster: dict[str, claude_cli.BgEntry],
             project_name: str = "") -> dict[str, Any] | None:
    """Reap a background turn once its session has SETTLED, or None while it works"""
    entry = roster.get(str(wo.get("session_id") or ""))
    age = time.time() - turn["started_at"]
    if entry is not None and entry.alive and not entry.idle:
        # Busy covers the worker's own background tasks too (fact 10): never cut one short
        cap = _bg_over_budget(store, wo, turn)
        if cap is None:
            return None
        claude_cli.stop_bg(entry.id)
        return _reap_bg(store, wo, turn, entry, project_name, budget_cap=cap)
    answered = _bg_answered(wo, turn)
    if entry is not None and entry.idle and not answered and age < BG_IDLE_GRACE:
        return None  # the resumed process has not taken its prompt yet
    if (entry is None or not entry.alive) and not answered and age < BG_DEAD_GRACE:
        return None
    return _reap_bg(store, wo, turn, entry, project_name)


def _bg_answered(wo: dict[str, Any], turn: dict[str, Any]) -> bool:
    """Has the session said anything, or compacted, since this turn's prompt went in?"""
    sid = str(wo.get("session_id") or "")
    if not sid:
        return False
    try:
        if usage.said_in_session(sid, since=turn["started_at"]):
            return True
        return (turn["kind"] == COMPACT_TURN
                and usage.last_compaction(sid, since=turn["started_at"]) is not None)
    except OSError:
        return False


def _bg_over_budget(store: ProjectStore, wo: dict[str, Any],
                    turn: dict[str, Any]) -> float | None:
    """The cap this running turn has spent through, or None — `--max-budget-usd` is print-only (fact 13)"""
    from .central_store import CentralStore

    now = time.time()
    if now - _budget_checked.get(turn["id"], 0.0) < BG_BUDGET_EVERY:
        return None
    _budget_checked[turn["id"]] = now
    central = CentralStore()
    try:
        cap = budget.ceiling(store, central, wo)
    finally:
        central.close()
    if cap is None:
        return None
    remaining = max(0.0, cap.remaining_usd)
    return remaining if budget.in_flight(store, wo["id"]) >= remaining else None


def _reap_bg(store: ProjectStore, wo: dict[str, Any], turn: dict[str, Any],
             entry: claude_cli.BgEntry | None, project_name: str = "",
             budget_cap: float | None = None) -> dict[str, Any]:
    """Settle a background turn from what the session left: the transcript and the Stop hook"""
    _budget_checked.pop(turn["id"], None)
    sid = str(wo.get("session_id") or "")
    until = time.time()
    try:
        envelope = usage.window_envelope(sid, turn["started_at"], until,
                                         schema=claude_cli.USAGE_SCHEMA_VERSION)
        said = usage.said_in_session(sid, since=turn["started_at"])
    except OSError:
        envelope, said = None, []
    cost = envelope["total_cost_usd"] if envelope else None
    if budget_cap is not None:
        result = claude_cli.TurnResult(
            ok=False, error=f"Reached maximum budget (${budget_cap:.2f})",
            subtype=claude_cli.BUDGET_SUBTYPE,
            terminal_reason=claude_cli.BUDGET_TERMINAL_REASON,
            cost_usd=cost, usage=envelope)
    elif said and said[-1][0] == usage.SYNTHETIC_MODEL:
        # The CLI's own words for a refused or broken call: what `_diagnose` reads
        result = claude_cli.TurnResult(ok=False, error=said[-1][1], cost_usd=cost,
                                       usage=envelope)
    elif ((entry is None or not entry.alive) and turn["kind"] != COMPACT_TURN
          and not _last_assistant_message(store, wo["id"], turn)):
        # Gone with no `Stop` since the prompt: whatever it said was mid-turn
        result = claude_cli.TurnResult(ok=False, error=SESSION_LOST_ERROR,
                                       terminal_reason=claude_cli.SESSION_LOST,
                                       cost_usd=cost, usage=envelope)
    else:
        reply = (_last_assistant_message(store, wo["id"], turn)
                 or next((text for model, text in reversed(said)
                          if model != usage.SYNTHETIC_MODEL), ""))
        result = claude_cli.TurnResult(ok=True, result=reply, session_id=sid,
                                       cost_usd=cost,
                                       num_turns=(envelope or {}).get("api_calls"),
                                       usage=envelope)
    if entry is not None and entry.alive and budget_cap is None:
        # Settled and idle: its process holds nothing, and the next prompt restarts it anyway
        claude_cli.stop_session(entry.id)
    return _settle_result(store, turn, result, project_name)


def _last_assistant_message(store: ProjectStore, wo_id: str,
                            turn: dict[str, Any]) -> str:
    """Backup reply source: what the `Stop` hook saw"""
    from . import db

    # The NEWEST page: an ascending LIMIT would search the oldest events of a long order
    for event in reversed(store.list_events(wo_id, limit=200, newest=True)):
        if event["kind"] != "hook:Stop" or event["ts"] < turn["started_at"]:
            continue
        payload = db.from_json(event.get("payload"), {}) or {}
        message = payload.get("last_assistant_message")
        if message:
            return str(message)
    return ""


#: What the OS used to say about EVERY turn that died this way, and all it could say
NO_RESULT = "the turn's process ended without writing a result"


def lost_turn_cost(store: ProjectStore, wo_id: str,
                   turn: dict[str, Any]) -> float | None:
    """What a turn spent, when the CLI never got to say — read off the transcript (#471)"""
    try:
        session_id = store.get_work_order(wo_id).get("session_id")
    except KeyError:
        return None  # cancelled on its way to being deleted
    if not session_id:
        return None
    try:
        cost = usage.cost_between(session_id, turn["started_at"],
                                  turn.get("ended_at") or time.time())
    except OSError:
        return None
    return cost or None


def _transcript_error(store: ProjectStore, wo_id: str,
                      turn: dict[str, Any]) -> str:
    """Why a turn died, recovered from the session transcript — the third place to look"""
    session_id = store.get_work_order(wo_id).get("session_id")
    if not session_id:
        return ""
    try:
        said = usage.said_in_session(
            session_id, since=turn["started_at"],
            until=turn.get("ended_at") or time.time())
    except OSError:
        return ""
    for model, text in reversed(said):
        if model == usage.SYNTHETIC_MODEL:
            return text
    return f"{NO_RESULT}; its last words were: {said[-1][1][:400]}" if said else ""


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


# -- parked, and coming back by itself

#: The window is spent
PAUSE_USAGE_LIMIT = "usage_limit"
#: The API broke
PAUSE_TRANSIENT = "transient"
#: THE ODD ONE OUT, AND THE COMMENT ABOVE IS WHY IT IS STILL HERE
PAUSE_AUTH = "auth"

#: NOT A TRANSPORT PAUSE, AND NEVER DIAGNOSED AS ONE
PAUSE_BUDGET = "budget"

#: A `retry_at` that will not arrive
NEVER = float("inf")

#: What to call each reason in a sentence written for the user
PAUSE_NOUN = {
    PAUSE_USAGE_LIMIT: "usage-limit",
    PAUSE_TRANSIENT: "Claude API",
    PAUSE_AUTH: "Claude Code authentication",
    PAUSE_BUDGET: "budget",
}


@dataclass(frozen=True)
class TurnPause:
    """A work order that stopped for something other than the work, and is coming back"""

    #: `PAUSE_USAGE_LIMIT`, `PAUSE_TRANSIENT` or `PAUSE_AUTH`
    reason: str
    #: The turn that died
    turn: dict[str, Any]
    #: The earliest moment the OS will relaunch
    retry_at: float
    #: Consecutive failures of this same kind, this one included
    attempts: int
    #: The failure, verbatim and squeezed onto one line
    message: str
    #: When the usage window reopens, as the CLI stated it
    reset_at: float | None = None
    #: The HTTP status, for a transient failure that carried one
    status: int | None = None

    @property
    def max_attempts(self) -> int:
        if self.reason == PAUSE_AUTH:
            return 0  # uncapped; what bounds an auth retry is `retry_at`, see `exhausted`
        return (MAX_RATE_LIMIT_RETRIES if self.reason == PAUSE_USAGE_LIMIT
                else len(TRANSIENT_BACKOFF))

    @property
    def exhausted(self) -> bool:
        """Retried enough"""
        return self.reason != PAUSE_AUTH and self.attempts > self.max_attempts

    @property
    def resumable(self) -> bool:
        """Will the OS relaunch this turn by itself — ever?"""
        return not self.exhausted and self.retry_at != NEVER

    def due(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.retry_at


def turn_pause(store: ProjectStore, wo_id: str) -> TurnPause | None:
    """This work order's pause, or None if it is not in one"""
    turn = store.latest_turn(wo_id)
    if turn is None or turn["state"] != "failed":
        return None
    reason, ended = _diagnose(turn), turn.get("ended_at") or turn["started_at"]
    if reason is None:
        return None
    if isinstance(reason, claude_cli.AuthFailure):
        return TurnPause(
            reason=PAUSE_AUTH, turn=turn, retry_at=_auth_retry_at(ended),
            attempts=pause_streak(store, wo_id, PAUSE_AUTH),
            message=reason.message,
        )
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
    # One delay per attempt, and the last one repeats
    delay = TRANSIENT_BACKOFF[min(max(attempts, 1), len(TRANSIENT_BACKOFF)) - 1]
    return TurnPause(
        reason=PAUSE_TRANSIENT,
        turn=turn,
        retry_at=ended + delay,
        attempts=attempts,
        message=reason.message,
        status=reason.status,
    )


def _auth_retry_at(ended: float) -> float:
    """When an auth-paused turn may go again: the moment the sign-in last changed"""
    changed = claude_cli.signin_changed_at()
    return changed if changed is not None and changed > ended else NEVER


def _diagnose(
    turn: dict[str, Any],
) -> claude_cli.AuthFailure | claude_cli.UsageLimit | claude_cli.TransientFailure | None:
    """What this failed turn actually was, or None if it was an ordinary failure"""
    error = turn.get("error")
    # A compaction that summarised nothing is the transport's loss, retried on its backoff
    if turn.get("kind") == COMPACT_TURN and error == NOTHING_COMPACTED:
        return claude_cli.TransientFailure(message=NOTHING_COMPACTED, status=None)
    auth = claude_cli.auth_failure(error)
    if auth is not None:
        return auth
    limit = claude_cli.usage_limit(error, now=turn.get("ended_at") or turn["started_at"])
    if limit is not None:
        return limit
    return claude_cli.transient_failure(
        error,
        terminal_reason=turn.get("terminal_reason"),
        api_error_status=turn.get("api_error_status"),
    )


def pause_streak(store: ProjectStore, wo_id: str, reason: str) -> int:
    """How many turns in a row this conversation has lost to `reason`"""
    want = {PAUSE_USAGE_LIMIT: claude_cli.UsageLimit,
            PAUSE_TRANSIENT: claude_cli.TransientFailure,
            PAUSE_AUTH: claude_cli.AuthFailure}[reason]
    n = 0
    for turn in store.recent_turns(wo_id, limit=MAX_RATE_LIMIT_RETRIES + 2):
        if turn["state"] != "failed" or not isinstance(_diagnose(turn), want):
            break
        n += 1
    return n


def retry(store: ProjectStore, project: ProjectSpec, wo: dict[str, Any],
          pause: TurnPause) -> dict[str, Any] | None:
    """Relaunch a turn the transport lost — None when the record shows nothing is left to relaunch"""
    turn = pause.turn
    # `/compact` is re-sent only into a conversation it never compacted (io-12b7d873)
    if turn["kind"] == COMPACT_TURN and compaction_landed(store, wo, turn):
        store.settle_turn_landed(turn["id"])
        store.add_event(wo["id"], "compaction_landed", {
            "seq": turn["seq"], "reason": pause.reason,
            "after": compacted_to(store.get_turn(turn["id"])) or None,
        })
        return None
    started = _conversation_started(project, wo)
    tree = worktree_path(project, wo)
    # Both halves are required
    nudge = started and _reached_model(turn) and turn["kind"] != COMPACT_TURN
    prompt = _nudge(pause) if nudge else turn["prompt"]
    return _launch(
        store, project, wo, prompt, kind=turn["kind"], resume=started,
        worktree=None if tree or turn["kind"] != "dispatch" else wo.get("worktree"),
        cwd=tree or project.path, msg_id=turn.get("msg_id"),
    )


def _reached_model(turn: dict[str, Any]) -> bool:
    """Did this turn get as far as the API before it died?"""
    from . import db

    usage = db.from_json(turn.get("usage_json"), None)
    if not isinstance(usage, dict):
        return False
    # `api_calls` is what a transcript-built envelope counts instead (`usage.window_envelope`)
    return (usage.get("duration_api_ms") or 0) > 0 or (usage.get("api_calls") or 0) > 0


def _nudge(pause: TurnPause) -> str:
    """What a worker is told when its own turn is relaunched under it"""
    # THE BUDGET IS NOT THE TRANSPORT
    if pause.reason == PAUSE_BUDGET:
        return (
            "[Jarvis] Your previous turn was cut short because this work order reached "
            "its spending budget. The user has since raised it, so you may continue. "
            "Nothing you had already done was lost — the conversation above is intact "
            "and is where you left off. Carry on from there and finish that turn. Do "
            "not start again and do not repeat work that is already done; re-check the "
            "state on disk first if you are unsure how far you got. The new budget is "
            "not unlimited, so prefer finishing what is in flight over widening scope."
        )
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


def _nudge_after_compaction(pause: TurnPause) -> str:
    """`_nudge`, for a relaunch that goes out behind a compaction (issue #843)"""
    # THE BUDGET IS NOT THE TRANSPORT
    if pause.reason == PAUSE_BUDGET:
        return (
            "[Jarvis] Your previous turn was cut short because this work order reached "
            "its spending budget. The user has since raised it, so you may continue. "
            "While it waited, the conversation's prompt cache expired, so the OS "
            "COMPACTED it: the summary above stands in for the earlier transcript. "
            "Nothing you did on disk was lost. Continue from where the summary says "
            "you were and finish that turn. Do not start again; re-check the state on "
            "disk (git status, git log, the files you were editing) before redoing "
            "anything the summary may not mention. The new budget is not unlimited, so "
            "prefer finishing what is in flight over widening scope."
        )
    what = ("Claude's usage limit was reached" if pause.reason == PAUSE_USAGE_LIMIT
            else "the Claude API failed" if pause.reason == PAUSE_TRANSIENT
            else "the work order was paused")
    return (
        f"[Jarvis] Your previous turn was cut short because {what}. This was the "
        "transport, not anything you or the work did. While the conversation waited, "
        "its prompt cache expired, so the OS COMPACTED it: the summary above stands in "
        "for the earlier transcript. Nothing you did on disk was lost. Continue from "
        "where the summary says you were and finish that turn. Do not start again; "
        "re-check the state on disk (git status, git log, the files you were editing) "
        "before redoing anything the summary may not mention."
    )


def _conversation_started(project: ProjectSpec, wo: dict[str, Any]) -> bool:
    """Has this work order's session ever been written to disk?"""
    session_id = wo.get("session_id")
    if not session_id:
        return False
    if is_background(wo):
        # The CLI chose the cwd the transcript is filed under: look it up by id
        try:
            return session_id in usage.index_sessions()
        except OSError:
            return True  # an id the CLI handed back names a session that exists
    cwd = worktree_path(project, wo) or project.path
    return claude_cli.session_transcript_path(cwd, session_id).exists()


def cancel(store: ProjectStore, wo_id: str) -> dict[str, Any]:
    """Take down the work order's in-flight turn, if it has one"""
    turn = busy(store, wo_id)
    if turn is None:
        return {"stopped": False, "reason": "no turn in flight"}
    wo = store.get_work_order(wo_id)
    unit = turn.get("unit")
    if is_background(wo):
        # `claude stop` keeps the conversation; the next prompt resumes it
        killed = bool(wo.get("bg_id")) and claude_cli.stop_bg(str(wo["bg_id"]))
    else:
        # The unit first, and then the process group anyway
        stopped_unit = systemd_units.stop_unit(unit) if unit else False
        killed = claude_cli.kill_process_group(turn["pid"]) or stopped_unit
    # A cancelled turn wrote no envelope and spent what it spent
    store.finish_turn(turn["id"], "failed",
                      error="cancelled" if killed else "cancelled (process already gone)",
                      cost_usd=lost_turn_cost(store, wo_id, turn),
                      cost_source=COST_FROM_TRANSCRIPT)
    store.add_event(wo_id, "turn_cancelled", {"seq": turn["seq"], "pid": turn["pid"],
                                              "unit": unit, "killed": killed})
    return {"stopped": killed, "pid": turn["pid"], "unit": unit, "seq": turn["seq"]}
