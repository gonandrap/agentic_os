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


def wo_title_prefix(wo_id: str) -> str:
    """The mandatory leading token of a pull request title: `[wo-1234abcd] `.

    The work order id verbatim, so the string a reviewer sees on GitHub is the string
    `jarvis wo show` accepts — no separate numbering scheme to translate.
    """
    return f"[{wo_id}] "


#: `gh pr create` flags this module reads, mapped to the key it files them under.
_PR_CREATE_FLAGS = {"--title": "title", "-t": "title",
                    "--body": "body", "-b": "body",
                    "--body-file": "body_file", "-F": "body_file"}


def gh_pr_create_args(command: str) -> dict[str, str] | None:
    """The flags of a `gh pr create` in this command, or None.

    None means "not something these hooks have an opinion about": not a `gh pr create`,
    or a command shlex cannot parse. An empty dict is a create carrying none of the
    flags above — `--fill`, or an editor prompt. Deliberately narrow — a hook that fires
    on commands it does not really understand costs more than the leak it prevents, and
    the contract text covers the rest.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, word in enumerate(words):
        if word in ("&&", "||", ";", "|"):
            continue
        # `gh`, but also `/snap/bin/gh` — gh is commonly not on a worker's PATH (it is
        # not on the production fleet's), so an absolute path is the normal way to
        # reach it, and a matcher that only knew the bare name let the first real PR
        # through untitled.
        if (word != "gh" and not word.endswith("/gh")) \
                or words[i + 1:i + 3] != ["pr", "create"]:
            continue
        found: dict[str, str] = {}
        for j, arg in enumerate(words[i + 3:], start=i + 3):
            if arg in ("&&", "||", ";", "|"):
                break
            flag, sep, inline = arg.partition("=")
            key = _PR_CREATE_FLAGS.get(flag)
            if key is None:
                continue
            if sep:
                found[key] = inline
            elif j + 1 < len(words):
                found[key] = words[j + 1]
        return found
    return None


def gh_pr_create_title(command: str) -> str | None:
    """The `--title` of a `gh pr create` in this command, or None."""
    return (gh_pr_create_args(command) or {}).get("title")


def gh_pr_create_body(command: str, cwd: str = "") -> str | None:
    """The body text a `gh pr create` would submit, or None when it cannot be read.

    `--body-file -` reads stdin, which a PreToolUse hook cannot see, and a path that
    does not resolve is the same situation. Both are None: a body the hook cannot read
    is one it must not judge.
    """
    args = gh_pr_create_args(command)
    if args is None:
        return None
    if "body" in args:
        return args["body"]
    path = args.get("body_file")
    if not path or path == "-":
        return None
    try:
        return (Path(cwd) / path if cwd else Path(path)).read_text()
    except OSError:
        return None


def pr_title_decision(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    """Hold `gh pr create` to the work order's title prefix.

    A pull request is the one artifact of a work order that outlives the OS's own
    records and is read by people who never see them, so it has to carry the id back.
    Contract text alone leaves it to memory; this makes it an invariant on the one path
    that opens PRs in practice.

    Denies rather than rewrites: the title is the worker's to write, and a hook silently
    editing the argument of a command it was asked to approve is a worse surprise than
    being told what to fix.
    """
    wo_id = env.get("JARVIS_WO_ID")
    if not wo_id:
        return None
    title = gh_pr_create_title((payload.get("tool_input") or {}).get("command", ""))
    if title is None:
        return None
    prefix = wo_title_prefix(wo_id)
    if title.startswith(prefix):
        return None
    return _deny(
        f"PR titles in a Jarvis-managed project must start with the work order id, so "
        f"the pull request is traceable back to it. Re-run with:\n"
        f'    --title "{prefix}{title}"'
    )


# -- the PR body: what a reviewer needs, and what GitHub must not mislink --------------
# Design: docs/superpowers/specs/2026-08-24-a-pull-request-a-reviewer-can-read.md

#: The `##` headings a PR body must carry. Single-sourced here; the shipped templates
#: (`.github/pull_request_template.md` and the skill's bundled copy) are asserted
#: against this tuple by tests/test_pr_body.py, so the three cannot drift.
PR_BODY_SECTIONS = ("Summary", "Implementation notes", "Questions asked to Neo",
                    "Learnings", "Test evidence")

# GitHub renders neither of these, so neither can mislink or count as content.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^\s*\|[\s|:-]+\|\s*$")

#: A `#123` GitHub will autolink. The lookbehind drops the cases it would NOT: a
#: `owner/repo#12` cross-reference, a URL fragment, a `##` heading.
_BARE_REF = re.compile(r"(?<![\w/#-])#\d+\b")
#: ...and these words immediately before one mean the author really did mean issue N.
_REF_IS_DELIBERATE = re.compile(
    r"\b(?:issues?|prs?|pull requests?|close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*$",
    re.IGNORECASE)


def _blank_out(pattern: re.Pattern[str], text: str) -> str:
    """Erase every match, preserving length and line breaks so offsets stay usable."""
    return pattern.sub(lambda m: re.sub(r"\S", " ", m.group(0)), text)


def mislinking_ref(body: str) -> str | None:
    """The first `#N` in `body` that GitHub would turn into a link to someone else's
    issue or pull request, or None.

    Work orders number their own items, so a description's "as in #2" becomes a link to
    a stranger's PR the moment it is copied into a body — observed on PR 143.
    """
    scannable = _blank_out(_CODE_SPAN, _blank_out(_HTML_COMMENT, body))
    for m in _BARE_REF.finditer(scannable):
        if not _REF_IS_DELIBERATE.search(scannable[:m.start()].rstrip()):
            return m.group(0)
    return None


def _section_is_empty(text: str) -> bool:
    """Whether a section holds anything the template did not already put there."""
    lines = _HTML_COMMENT.sub("", text).splitlines()
    seps = {i for i, ln in enumerate(lines) if _TABLE_SEP.match(ln)}
    scaffolding = seps | {i - 1 for i in seps}  # a separator's header row
    for i, line in enumerate(lines):
        line = line.strip()
        if i in scaffolding or not line or line in ("-", "*") or set(line) <= set("-="):
            continue
        if line.startswith("|"):
            # The first column is the row's template-supplied label; the row says
            # something only once a later column is filled in.
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not any(cells[1:]):
                continue
        return False
    return True


def pr_body_problems(body: str) -> list[str]:
    """Every reason this body is not ready, as phrases naming the fix."""
    heads = [(m.group(1).strip().lower(), m.start(), m.end())
             for m in _HEADING.finditer(body)]
    # A section runs to the START of the next heading of any level, so the heading line
    # itself is never mistaken for the previous section's content.
    starts = [h[1] for h in heads] + [len(body)]
    spans = {name: body[end:starts[i + 1]] for i, (name, _, end) in enumerate(heads)}
    problems = []
    for name in PR_BODY_SECTIONS:
        if name.lower() not in spans:
            problems.append(f"no `## {name}` section")
        elif _section_is_empty(spans[name.lower()]):
            problems.append(f"`## {name}` is still the empty template")
    ref = mislinking_ref(body)
    if ref is not None:
        problems.append(
            f"`{ref}` — GitHub links that to issue/PR {ref[1:]}. Say `item {ref[1:]} "
            f"of the work order`, or `issue {ref}` if you really do mean that issue, "
            f"or put it in backticks if it is a literal")
    return problems


def pr_body_decision(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    """Hold `gh pr create` to a body a reviewer can actually review.

    Same reasoning as `pr_title_decision`, one field over: the body is the only place a
    reviewer learns what the diff cannot tell them, and the contract's "a PR body hints,
    it does not explain" was read as licence to ship a thin one (PR 143 — no test
    evidence, no questions, no learnings). Prose alone does not fix that: kn-fe226ab1
    measured a contract bullet changing worker behaviour 0/5.

    Denies rather than rewrites, and never guesses — a body it cannot read (`--fill`,
    an editor, stdin) is not its business.
    """
    if not env.get("JARVIS_WO_ID"):
        return None
    command = (payload.get("tool_input") or {}).get("command", "")
    body = gh_pr_create_body(command, payload.get("cwd") or "")
    if body is None:
        return None
    problems = pr_body_problems(body)
    if not problems:
        return None
    return _deny(
        "This PR body is not ready:\n"
        + "".join(f"  - {p}\n" for p in problems)
        + "The template and the rules for filling it are in your "
          "`open-a-pull-request` skill; this repository's copy, if it has one, is "
          "`.github/pull_request_template.md`."
    )


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
    action = _classify(command, config)
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


def _classify(command: str, config: Any) -> Any:
    """Classify against the LIVE rule base, and count any exemption that fires.

    The rules are in `os.db` because they change — a dismissal in another project may
    already have settled this command's shape. Reading them here is what makes that
    learning reach the one place it matters, the hook that would otherwise block a worker
    mid-turn.

    A database this cannot read falls back to the seeded recognisers, which is the safe
    direction and not an obvious one: the fallback restores every recogniser and no
    exemption, so a broken `os.db` makes the gate over-eager rather than absent. A worker
    pays for a spurious review; nothing ships unreviewed.
    """
    from . import gates
    from .central_store import CentralStore
    from .gate_rules import RuleSet

    try:
        central = CentralStore()
    except Exception:  # noqa: BLE001 — see the docstring
        return gates.classify(command, config, rules=RuleSet.from_seeds())
    try:
        return gates.classify(command, config, rules=RuleSet.load(central),
                              central=central)
    except Exception:  # noqa: BLE001
        return gates.classify(command, config, rules=RuleSet.from_seeds())
    finally:
        central.close()


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
            neo = NeoStore()
            try:
                gates.open_gate(store, grant, neo=neo)
            finally:
                neo.close()
            # Two things open a gate and only one of them is permission. Saying which is
            # which here matters because this string is the audit record of why the
            # command ran: "approved" against a command that was never privileged is the
            # false entry the dismissed verdict exists to keep out of the log.
            verb = ("dismissed as a classifier false positive (nothing was authorised) by"
                    if grant["status"] == "dismissed" else "approved by")
            return _allow(
                f"gate {grant['id']} ({action.kind}) {verb} "
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
                # Which SEAT attempted it, if a subagent did. `JARVIS_WO_ID` is
                # per-session, so the request is filed against the work order either way;
                # this is the only thing that keeps the record from saying the lead ran a
                # command its team ran. `PreToolUse` omits the key for the lead's own
                # calls, so absence is the discriminator, not a sentinel value.
                agent_type=payload.get("agent_type") or None,
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
    a merge or a release by accident. The PR title and body rules are checked next, for
    the same reason in reverse: they must not be reachable around by an auto-approval
    below them.
    """
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        gated = gate_decision(payload, env)
        if gated is not None:
            return gated
        mistitled = pr_title_decision(payload, env)
        if mistitled is not None:
            return mistitled
        unreviewable = pr_body_decision(payload, env)
        if unreviewable is not None:
            return unreviewable
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


# -- surviving compaction ---------------------------------------------------------------
#
# Design and the measurements behind it:
# docs/superpowers/specs/2026-08-10-resume-cost-and-the-cache.md
#
# Two hooks, one flag file. `PreCompact` cannot inject anything and `PostCompact` cannot
# either (`additionalContext` is not accepted on that event), so the re-assertion rides
# the next `PostToolUse` — which is also what puts it INSIDE the turn that was compacted
# rather than at the start of the next one.


def compaction_flag(root: Path, wo_id: str) -> Path:
    return root / ".jarvis" / "compaction" / f"{wo_id}.pending"


def note_compaction(payload: dict[str, Any], root: Path,
                    store: ProjectStore, wo_id: str) -> None:
    """PreCompact: record that it happened and arm the re-assertion."""
    store.add_event(wo_id, "compacted", {
        "trigger": payload.get("trigger"),
        "custom_instructions": payload.get("custom_instructions") or None,
    })
    flag = compaction_flag(root, wo_id)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(payload.get("trigger") or "auto")


def compaction_brief(store: ProjectStore, wo: dict[str, Any], project: str) -> str:
    """What a compacted worker must not have to rediscover.

    Deliberately NOT a summary of the conversation — Claude Code already wrote one, and
    a second model call would only add loss. This is the part a summary cannot be trusted
    to carry: the identifiers and the contract, rendered from the record Jarvis already
    holds, so it is exact by construction.
    """
    wo_id = wo["id"]
    lines = [
        "# Your context was just compacted — re-asserting the parts that must be exact",
        "",
        f"You are the worker for **{wo_id}** in project **{project}**: {wo['title']}",
    ]
    for label, key in (("Branch", "branch"), ("Worktree", "worktree"), ("PR", "pr_url")):
        if wo.get(key):
            lines.append(f"- {label}: `{wo[key]}`")
    lines.append(f"- Status: {wo['status']}")

    pending = store.pending_assumptions(wo_id)
    if pending:
        lines += ["", f"**{len(pending)} assumption(s) already recorded and still "
                      "pending review** — do not record these again:"]
        lines += [f"- {a['content'][:200]}" for a in pending[:10]]

    if wo.get("description"):
        lines += ["", "## The original ask, verbatim", "", wo["description"]]

    lines += ["", *worker_brief_core(wo_id, wo["title"], project)]
    return "\n".join(lines)


def worker_brief_core(wo_id: str, title: str, project: str) -> list[str]:
    from .worker_brief import core_contract
    return core_contract(wo_id, title, project, has_knowledge=True)


def resume_after_compaction(env: dict[str, str], root: Path, store: ProjectStore,
                            wo: dict[str, Any]) -> dict[str, Any] | None:
    """PostToolUse: if a compaction was armed, spend the flag and re-assert the record.

    Spent exactly once — the flag is unlinked before the brief is built, so a failure
    while rendering costs the re-assertion rather than repeating it on every tool call
    for the rest of the work order.
    """
    flag = compaction_flag(root, wo["id"])
    try:
        flag.unlink()
    except OSError:
        return None  # not armed (the common case), or already spent
    store.add_event(wo["id"], "compaction_brief_injected", {})
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": compaction_brief(
                store, wo, env.get("JARVIS_PROJECT", "")),
        },
    }


def _compaction_context(env: dict[str, str], cwd: Path) -> tuple[Path, str] | None:
    """(project root, wo id) for a dispatched worker, or None for anything else."""
    wo_id = env.get("JARVIS_WO_ID")
    if not wo_id:
        return None  # interactive session — Jarvis does not manage its context
    root_env = env.get("JARVIS_PROJECT_PATH")
    root = Path(root_env) if root_env else find_project_root(cwd)
    if root is None or not (root / ".jarvis").is_dir():
        return None
    return root, wo_id


def _pre_compact(payload: dict[str, Any], env: dict[str, str],
                 cwd: Path) -> dict[str, Any] | None:
    ctx = _compaction_context(env, cwd)
    if ctx is None:
        return None
    root, wo_id = ctx
    store = ProjectStore(root)
    try:
        store.get_work_order(wo_id)
        note_compaction(payload, root, store, wo_id)
    except KeyError:
        return None  # adhoc/unknown work order — nothing to re-assert later
    finally:
        store.close()
    return {"wo_id": wo_id, "event": "PreCompact"}


def _post_tool_compaction(env: dict[str, str], cwd: Path) -> dict[str, Any] | None:
    """Hot path: this runs after EVERY tool call, so it costs one `stat` until a
    compaction has actually armed it. The database is not opened otherwise."""
    ctx = _compaction_context(env, cwd)
    if ctx is None:
        return None
    root, wo_id = ctx
    if not compaction_flag(root, wo_id).exists():
        return None
    store = ProjectStore(root)
    try:
        return resume_after_compaction(env, root, store, store.get_work_order(wo_id))
    except KeyError:
        return None
    finally:
        store.close()


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


def _parked_on_the_delegate(store: ProjectStore, wo_id: str) -> str:
    """What Neo is holding for this work order — "" when nothing is.

    Only ever consulted for a `waiting_input` work order, which is the state both waits
    put it in (`ops.ask_question`, `gates.request`). A `running` worker's Notification is
    a real mid-work block until proven otherwise, and swallowing that would strand it.
    """
    if store.get_work_order(wo_id)["status"] != "waiting_input":
        return ""
    from .invariants import awaiting_neo

    question = awaiting_neo(wo_id)
    if question is not None:
        return f"neo question {question['id']} ({question['status']})"
    if store.pending_approvals(wo_id):
        return "a privileged-action gate awaiting a verdict"
    return ""


def handle_hook(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    cwd = Path(payload.get("cwd") or env.get("PWD") or ".")

    if event == "PreToolUse":
        return preflight_decision(payload, env)

    if event == "PostToolUse":
        # The memory mirror is a side effect and runs either way; only one of the two
        # can own the hook's return value, and a pending compaction outranks it.
        captured = capture_memory_write(payload, env)
        return _post_tool_compaction(env, cwd) or captured

    if event == "PreCompact":
        return _pre_compact(payload, env, cwd)

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
            # The same reasoning one status further in. A worker that ended its turn on
            # `jarvis wo ask` or on a gate request is SITTING in `waiting_input` with a
            # live session, so the test above lets it through — and a minute later Claude
            # Code's idle prompt stamps "Claude is waiting for your input" over a work
            # order that is waiting on Neo. It is the delegate's whole purpose that this
            # costs the user nothing (GitHub issue 100), and where Neo has already handed
            # the question back the flag it overwrites is the better one: it names the
            # question and the command that answers it.
            parked = _parked_on_the_delegate(store, wo_id)
            if parked:
                store.add_event(wo_id, "notification_ignored", {
                    "message": payload.get("message"),
                    "reason": f"idle prompt while parked on {parked}",
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
