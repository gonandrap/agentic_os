"""Handler for `jarvis _hook` — invoked by the Claude Code hooks that the OS injects
into every managed project's settings (SessionStart / Stop / SessionEnd / Notification).

Claude Code pipes a JSON payload on stdin (hook_event_name, session_id, cwd, ...).
We map the session to a work order (JARVIS_WO_ID env var set at dispatch, falling back
to a session_id lookup) and update the project DB. Sessions that aren't Jarvis workers
are a silent no-op, so interactive sessions in managed projects are unaffected.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from .project_store import ProjectStore

# A Bash command every worker must be able to run without a permission prompt:
# a chain of `cd <dir>` / `jarvis …` segments joined by &&, nothing else.
_SHELL_DANGEROUS = re.compile(r"[|;`$<>]")


def is_jarvis_command_chain(command: str) -> bool:
    if _SHELL_DANGEROUS.search(command):
        return False
    for segment in command.split("&&"):
        try:
            words = shlex.split(segment.strip())
        except ValueError:
            return False
        if not words:
            return False
        if words[0] == "jarvis":
            continue
        if words[0] == "cd" and len(words) == 2:
            continue
        return False
    return "jarvis" in command


def _allow(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def gate_decision(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    """Mediate a privileged action: allow it if approved, otherwise get it reviewed.

    Returns None when the command isn't gated — the caller then applies the ordinary
    rules. Once a command IS recognised as privileged this never returns None: it either
    allows (a live grant covers it) or denies. Including when the machinery itself
    breaks — an unreadable DB must not become an open door, so errors deny.

    The deny is not a refusal, it is a redirect. Approval cannot be resolved inline: the
    hook has ~30 seconds and a Neo review takes minutes. So the first attempt files the
    request and stops; the verdict arrives through the ordinary message channel and the
    retry goes through.
    """
    from . import gates

    wo_id = env.get("JARVIS_WO_ID")
    if not wo_id:
        return None  # interactive session — gates govern dispatched workers only
    config = gates.GateConfig.from_json(env.get("JARVIS_GATES"))
    if not config:
        return None  # project hasn't opted in
    command = (payload.get("tool_input") or {}).get("command") or ""
    action = gates.classify(command, config)
    if action is None:
        return None

    try:
        return _resolve_gate(action, wo_id, env, payload)
    except Exception as e:  # noqa: BLE001 — fail closed; see the docstring
        return _deny(
            f"Gate `{action.kind}`: the OS could not verify approval for this command "
            f"({e!r}), so it is blocked. This is a fault in Jarvis, not a verdict on "
            f"your request — report it with your `report-jarvis-bug` skill."
        )


def _resolve_gate(action: Any, wo_id: str, env: dict[str, str],
                  payload: dict[str, Any]) -> dict[str, Any]:
    from . import gates
    from .neo_store import NeoStore

    root_env = env.get("JARVIS_PROJECT_PATH")
    root = Path(root_env) if root_env else find_project_root(Path(payload.get("cwd") or "."))
    if root is None or not (root / ".jarvis").is_dir():
        return _deny(
            f"Gate `{action.kind}`: this command needs approval, but the OS cannot find "
            f"the project database to record the request in. Blocked."
        )

    store = ProjectStore(root)
    try:
        grant = store.usable_grant(wo_id, action.kind, action.command)
        if grant is not None:
            store.consume_grant(grant["id"])
            return _allow(
                f"gate {grant['id']} ({action.kind}) approved by "
                f"{grant['decided_by']}: {grant['decision_reason'] or 'no reason given'}"
            )

        prior = store.latest_approval_for(wo_id, action.kind, action.command)
        if prior is not None and prior["status"] == "pending":
            return _deny(
                f"Gate `{action.kind}`: approval request {prior['id']} for this exact "
                f"command is already under review. END YOUR TURN — the verdict arrives "
                f"as your next user turn. Do not retry in a loop."
            )
        if prior is not None and prior["status"] == "denied":
            return _deny(
                f"Gate `{action.kind}`: this command was DENIED "
                f"(request {prior['id']}, by {prior['decided_by']}): "
                f"{prior['decision_reason'] or 'no reason recorded'}. Do not retry it "
                f"as-is. Address the reason, then `jarvis gate request` afresh."
            )

        wo = store.get_work_order(wo_id)
        neo = NeoStore()
        try:
            approval, question = gates.file_request(
                store, neo, env.get("JARVIS_PROJECT", ""), wo, action,
                justification=(
                    "(none — the worker ran the command directly rather than filing a "
                    "request, so no case was made for it)"
                ),
            )
        finally:
            neo.close()
        return _deny(
            f"Gate `{action.kind}`: {action.summary} needs approval, so this attempt was "
            f"blocked and approval request {approval['id']} was filed for review "
            f"(Neo question {question['id']}).\n\n"
            f"END YOUR TURN NOW — the verdict arrives as your next user turn, and the "
            f"retry will go through if it is approved.\n\n"
            f"You filed no justification because you ran the command directly. To make "
            f"the case properly (branch, PR, test results — reviewers see only what you "
            f"write), run:\n"
            f"    jarvis gate request {wo_id} \"{action.command}\" "
            f"--why \"<why this is ready to ship>\" --evidence \"<PR, tests, checks>\""
        )
    finally:
        store.close()


def preflight_decision(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    """PreToolUse auto-approvals that keep autonomous workers unattended:

    - `jarvis …` contract commands (also when prefixed with `cd <dir> &&`), which
      otherwise stall background sessions on a permission prompt.
    - File edits *inside the worker's own worktree* — the worktree exists solely for
      this work order, so the worker owns it (verified live: acceptEdits alone still
      prompted for Write in a background session). Only active for worker sessions
      (JARVIS_WO_ID set), never for interactive sessions in managed projects.

    Gated privileged actions are resolved FIRST, so no auto-approval below can hand out
    a merge or a release by accident.
    """
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        gated = gate_decision(payload, env)
        if gated is not None:
            return gated
        if is_jarvis_command_chain(tool_input.get("command", "")):
            return _allow("jarvis contract command")
        return None

    if tool in ("Edit", "Write", "NotebookEdit") and env.get("JARVIS_WO_ID"):
        cwd = payload.get("cwd") or ""
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if cwd and "/.claude/worktrees/" in cwd:
            try:
                Path(file_path).resolve().relative_to(Path(cwd).resolve())
            except ValueError:
                return None
            return _allow("worker edit inside its own worktree")
    return None


def memory_topic(file_path: str) -> str | None:
    """Topic name for a Claude Code memory file, or None if the path isn't one.

    Claude Code keeps its own per-project file memory at
    `<claude config dir>/projects/<slug>/memory/<name>.md` — a store Jarvis neither
    writes nor reads, and which dies with the worker's worktree slug. `MEMORY.md` is
    that store's index (pointers, not knowledge), so it is skipped.
    """
    try:
        p = Path(file_path)
    except (TypeError, ValueError):
        return None
    parts = p.parts
    if p.suffix != ".md" or p.name == "MEMORY.md" or len(parts) < 4:
        return None
    if parts[-2] != "memory" or parts[-4] != "projects":
        return None
    return p.stem


def capture_memory_write(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    """PostToolUse: mirror a worker's Claude-memory write into the knowledge base.

    Workers are told to run `jarvis learn add`, but "remember this" is a reflex that
    Claude Code's built-in memory answers first — and anything that lands there is
    invisible to the user, to Neo, and to every future worker. Mirroring makes the
    knowledge base the single memory regardless of which channel the worker reaches for.
    """
    wo_id = env.get("JARVIS_WO_ID")
    if not wo_id:
        return None  # interactive session — its memory is its own business
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    topic = memory_topic(file_path)
    if topic is None:
        return None
    try:
        content = Path(file_path).read_text().strip()
    except OSError:
        return None  # deleted or unreadable between write and hook — nothing to mirror
    if not content:
        return None

    from .central_store import CentralStore

    central = CentralStore()
    try:
        if not central.record_memory_file(content, project=env.get("JARVIS_PROJECT", ""),
                                          topic=topic):
            return None  # rewritten with identical content — already captured
    finally:
        central.close()

    root_env = env.get("JARVIS_PROJECT_PATH")
    root = Path(root_env) if root_env else find_project_root(Path(payload.get("cwd") or "."))
    if root is not None and (root / ".jarvis").is_dir():
        store = ProjectStore(root)
        try:
            store.add_event(wo_id, "learning_captured",
                            {"topic": topic, "source": file_path})
        except Exception:  # noqa: BLE001 — the knowledge is saved; the note is a bonus
            pass
        finally:
            store.close()
    return {"captured": topic, "wo_id": wo_id}


def find_project_root(cwd: Path) -> Path | None:
    """Map a hook cwd (possibly a worktree under .claude/worktrees/) to the project
    root that holds .jarvis/."""
    cwd = cwd.resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".jarvis").is_dir():
            return candidate
        # worktrees live at <root>/.claude/worktrees/<name>
        if candidate.parent.name == "worktrees" and candidate.parent.parent.name == ".claude":
            root = candidate.parent.parent.parent
            if (root / ".jarvis").is_dir():
                return root
    return None


def _is_current_session(store: ProjectStore, wo_id: str, session_id: str) -> bool:
    """Is this hook coming from the session the work order is actually bound to?

    A work order has exactly one session id for its whole life now, so this is close to
    a formality — but it still earns its keep for work orders created under the old
    background-session transport, whose spent sessions can be re-opened from the agents
    view and fire hooks that must not steer anything. Unknown-session hooks count as
    current only when there is nothing to compare against.
    """
    bound = store.get_work_order(wo_id).get("session_id")
    return not bound or not session_id or bound == session_id


def handle_hook(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    cwd = Path(payload.get("cwd") or env.get("PWD") or ".")

    if event == "PreToolUse":
        return preflight_decision(payload, env)

    if event == "PostToolUse":
        return capture_memory_write(payload, env)

    root_env = env.get("JARVIS_PROJECT_PATH")
    root = Path(root_env) if root_env else find_project_root(cwd)
    if root is None or not (root / ".jarvis").is_dir():
        return None  # not a managed project — no-op

    store = ProjectStore(root)
    try:
        wo_id = env.get("JARVIS_WO_ID")
        wo = None
        if wo_id:
            try:
                wo = store.get_work_order(wo_id)
            except KeyError:
                wo = None
        if wo is None and session_id:
            wo = store.find_by_session(session_id)
        if wo is None:
            return None  # not a worker session — no-op

        wo_id = wo["id"]
        store.add_event(wo_id, f"hook:{event}", {
            "session_id": session_id,
            "cwd": str(cwd),
            "message": payload.get("message"),
            # Stop carries the turn's final assistant message. Kept as the backup reply
            # source: the turn's own JSON result is primary, and `worker_session` reads
            # this only when that comes back empty.
            **({"last_assistant_message": payload["last_assistant_message"]}
               if payload.get("last_assistant_message") else {}),
        })

        if event == "SessionStart":
            # No binding to do: Jarvis mints the session id with `--session-id` before
            # the process exists, and a headless `--resume` reuses it, so the work order
            # already knows it and it never changes. This hook fires on every turn
            # (`source: resume` from turn two on) and is now purely confirmation — the
            # only state it touches is the dispatching->running correction, for the case
            # where the worker starts talking before the daemon's next tick.
            if store.get_work_order(wo_id)["status"] == "dispatching":
                store.set_status(wo_id, "running")

        elif not _is_current_session(store, wo_id, session_id):
            # A superseded session reporting on itself. Its own end is not the work
            # order's end, and its idle prompt is not the worker asking for input —
            # the live fork is elsewhere. Recorded above, acted on never.
            store.add_event(wo_id, "hook_ignored", {
                "event": event, "session_id": session_id,
                "reason": "not the session this work order is bound to",
            })
            return {"wo_id": wo_id, "event": event, "ignored": True}

        elif event == "Notification":
            # Fired when the session needs attention — but for two very different
            # reasons: a real mid-work block (permission request), or the idle prompt
            # Claude Code raises ~1 min after a turn ends, which every finished worker
            # triggers. The payload does not distinguish them.
            #
            # So the work order's own state decides. If it has already settled (the
            # worker called `jarvis wo finish`, or the reconciler filed it for review),
            # this is the idle prompt and there is nothing to report: acting on it
            # overwrites the real reason ("2 assumptions pending your review") with a
            # generic "Claude is waiting for your input" and sends the user hunting for
            # a question that does not exist. Verified against two live work orders.
            if wo["status"] not in ("running", "dispatching", "waiting_input"):
                store.add_event(wo_id, "notification_ignored", {
                    "message": payload.get("message"),
                    "reason": f"work order already {wo['status']}",
                })
                return {"wo_id": wo_id, "event": event, "ignored": True}
            message = payload.get("message") or "Worker needs attention"
            if wo["status"] in ("running", "dispatching"):
                store.set_status(wo_id, "waiting_input")
            store.flag_attention(wo_id, message)
            store.add_notification(
                title=f"{wo_id} needs input",
                body=message,
                level="warning",
                wo_id=wo_id,
                source="hook:Notification",
            )

        elif event == "Stop":
            # End of a turn. Recorded above (with the final assistant message); what it
            # means for the work order is settled from the turn row, not from here.
            pass

        elif event == "SessionEnd":
            # Deliberately inert. Under the headless-turn transport this fires at the
            # end of EVERY turn, not at the end of the conversation — so the settlement
            # this hook used to do ("session ended without `jarvis wo finish`") would
            # file every work order for review after its first turn. Settling is the
            # turn reconciler's job now (Daemon.settle_work_order), which can tell a
            # turn ending from a conversation ending. Recorded on the timeline above.
            pass
        return {"wo_id": wo_id, "event": event}
    finally:
        store.close()


def main_hook() -> int:
    """Entry point for `jarvis _hook`. Never fails the session: always exit 0."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = handle_hook(payload, dict(os.environ))
        if result and "hookSpecificOutput" in result:
            print(json.dumps(result))
    except Exception as e:  # noqa: BLE001 — a broken hook must not break sessions
        try:
            from .paths import logs_dir
            logs_dir().mkdir(parents=True, exist_ok=True)
            with (logs_dir() / "hook-errors.log").open("a") as f:
                f.write(f"{e!r}\n")
        except Exception:  # noqa: BLE001
            pass
    return 0
