"""Handler for `jarvis _hook` — invoked by the Claude Code hooks that the OS injects
into every managed project's settings (SessionStart / SubagentStart / Stop /
SessionEnd / Notification).

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

from . import concision
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
                    "Alarms raised", "Learnings", "Test evidence", "Screenshots")

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


#: A markdown image, and the only target form GitHub renders in a body. The target is
#: the first thing after `(` — a `<…>` form or a bare path, before any title string.
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(<[^>\n]+>|[^)\s]+)")
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)


def unrenderable_image(body: str) -> str | None:
    """The first markdown image target in `body` that GitHub will not render, or None.

    S5 of the design doc. kn-72cec521: GitHub resolves a repo-relative path for a link
    and not for an image, so `![x](docs/screenshots/x.png)` looks right when written and
    is a broken icon in review (PR 173).
    """
    scannable = _blank_out(_CODE_SPAN, _blank_out(_HTML_COMMENT, body))
    for m in _MARKDOWN_IMAGE.finditer(scannable):
        target = m.group(1).strip("<>")
        if not _ABSOLUTE_URL.match(target):
            return target
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
    image = unrenderable_image(body)
    if image is not None:
        problems.append(
            f"`![…]({image})` renders broken — GitHub resolves a relative path for a "
            f"link, not for an image. Link it by raw URL at the commit SHA: "
            f"`https://raw.githubusercontent.com/<owner>/<repo>/<sha>/{image}` "
            f"(the SHA from `git rev-parse HEAD`, and push before you post the body)")
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


def finish_summary_decision(payload: dict[str, Any],
                            env: dict[str, str]) -> dict[str, Any] | None:
    """Refuse a `jarvis wo finish --summary` longer than the cap.

    The mechanism half of docs/superpowers/specs/2026-09-19-concision-enforced.md (SS5.2).
    Prose could not do this job: kn-fe226ab1 measured a contract bullet changing worker
    behaviour 0/5, twice, and named a refusing hook as what the class wants instead.
    `pr_body_decision` above is the same shape for the same reason.

    Denies rather than truncating. A hook that edited the summary would be putting words
    the worker never wrote onto a record the user reads as the worker's, and the worker
    is the only party that knows which sentence was load-bearing.

    The denial names the skills on purpose. A worker has no reason to open `i-have-adhd`
    until something tells it its output is wrong; this is the moment it is wrong, and
    the moment it can still act.
    """
    if not env.get("JARVIS_WO_ID"):
        return None
    cap = concision.summary_cap(env)
    if not cap:
        return None  # switched off for this project
    summary = concision.finish_summary((payload.get("tool_input") or {}).get("command", ""))
    if summary is None:
        return None
    words = concision.word_count(summary)
    if words <= cap:
        return None
    return _deny(
        f"This `--summary` is {words} words; the cap is {cap}.\n"
        f"  - The summary is the HEADLINE, not the report. One or two sentences: what "
        f"you built and where it landed.\n"
        f"  - Everything you just cut belongs in the final message of this turn, which "
        f"is captured onto the work order verbatim and is what the user and Neo "
        f"actually read. Nothing is lost by moving it there.\n"
        f"  - Evidence goes in `--evidence`, not in the summary.\n"
        f"Your `i-have-adhd` and `caveman` skills are how to shorten it without "
        f"dropping anything that matters."
    )


#: The transport declaration (`claude_cli.TURN_TRANSPORT_ENV` and its two values) and the
#: crew keys, spelled rather than imported: `import jarvis.claude_cli` costs 56ms against
#: this module's 27ms, and this hook runs on every Bash call and every file write.
#: `tests/test_background_refusal.py` builds its env from the constants and asserts
#: against these, so the two cannot drift silently.
TURN_TRANSPORT_ENV = "JARVIS_TURN_TRANSPORT"
TRANSPORT_HEADLESS = "headless"
REQUIRE_CREW_ENV = "JARVIS_REQUIRE_CREW"
WO_KIND_ENV = "JARVIS_WO_KIND"

#: JSON list of repo-relative paths an installed TOOL owns and rewrites, marked
#: `skip-worktree` in the worker worktree's index by `mark_tool_managed_paths`. Defined in
#: the READER, like `concision.STANDING_PROMPT_ENV`, and written by
#: `dispatch._write_worker_settings` from `project.worktree.tool_managed_paths` — the hook
#: must not import `jarvis.catalog` to learn a value fixed at spawn (~60ms against a 155ms
#: hook process). Absent or unparseable: the mechanism is off for that worker, silently,
#: which is what a work order dispatched before this landed carries.
TOOL_MANAGED_PATHS_ENV = "JARVIS_TOOL_MANAGED_PATHS"

#: Cap on how many of them one `SessionStart` will consider, so the hook's cost cannot
#: grow without limit as the list does.
MAX_TOOL_MANAGED_PATHS = 32

#: Everything the shell backgrounds a job with, other than a bare `&`. In COMMAND
#: position only, so `grep -r nohup src/` is not a detached job.
_BACKGROUNDING_WORD = re.compile(
    r"(?:^|[;&|(\n])\s*(?:\w+=\S+\s+)*(nohup|setsid|disown)\b")

_QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"", re.DOTALL)
_SHELL_COMMENT = re.compile(r"(?:(?<=^)|(?<=\s))#[^\n]*")


def _mask_shell_text(command: str) -> str:
    """The command with quoted spans and comments blanked, positions preserved.

    An `&` inside a string or a comment is prose. This is the whole difficulty of the
    shell half of §4 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md.
    """
    masked = _QUOTED_SPAN.sub(lambda m: " " * len(m.group(0)), command)
    return _SHELL_COMMENT.sub(lambda m: " " * len(m.group(0)), masked)


def backgrounds_through_shell(command: str) -> bool:
    """Whether this command starts a job the shell detaches from the turn.

    NOT matched, and each of these is an ordinary foreground command: `&&`, `&>`, `&>>`,
    `2>&1`. `&` counts only as a statement terminator.
    """
    masked = _mask_shell_text(command)
    if _BACKGROUNDING_WORD.search(masked):
        return True
    for i, char in enumerate(masked):
        if char != "&":
            continue
        before = masked[i - 1] if i else ""
        after = masked[i + 1] if i + 1 < len(masked) else ""
        if "&" in (before, after) or after == ">" or before in "><":
            continue
        return True
    return False


def background_task_decision(payload: dict[str, Any],
                             env: dict[str, str]) -> dict[str, Any] | None:
    """Refuse a job this turn cannot outlive, before it starts.

    §4 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md. The contract
    has forbidden backgrounding in prose since TEMPLATE_VERSION v10 and wo-d81fcc15 did
    it anyway, on two consecutive turns, the second costing 62 hours of wall clock
    (issue #575) — kn-6dcaf055 is the class, `finish_summary_decision` above is the same
    shape for the same reason.

    Conditional on the DECLARED transport, never on a hardcoded belief that a turn is
    one-shot: `spawn_background` is supervisor-owned and the notifications DO arrive
    there. Absent key = not a Jarvis worker = no-op.

    A subagent's calls are covered without a second mechanism: PreToolUse fires on them
    under the parent session.
    """
    if env.get(TURN_TRANSPORT_ENV) != TRANSPORT_HEADLESS:
        return None
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input") or {}
    if not (tool_input.get("run_in_background")
            or backgrounds_through_shell(tool_input.get("command", ""))):
        return None
    return _deny(
        "Re-run this in the FOREGROUND: this turn is one `claude -p` process, ending it "
        "kills whatever you left running, and nothing wakes you when a background job "
        "finishes. Wait for the command here instead — the fix costs you nothing but "
        "the wait, and there is no notification coming."
    )


def crew_edit_decision(payload: dict[str, Any],
                       env: dict[str, str]) -> dict[str, Any] | None:
    """Refuse the LEAD's own file edits, so the delegation is a control and not prose.

    §7 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md. The
    discriminator is the ABSENCE of `agent_type` on the payload (see the comment at the
    gate's `_open_request` call below): PreToolUse carries it for a seat's calls.

    The hole, stated in the spec and worth restating here: Bash is not denied, so a
    heredoc or `sed -i` writes a file anyway. A wall for the tool surface, a speed bump
    for the shell — the lead needs the shell for git, pytest and every `jarvis …`
    command, and a lead that routes around this has decided to rather than forgotten.
    """
    if payload.get("agent_type"):
        return None  # a seat, which is exactly what this rule wants
    if not (env.get("JARVIS_WO_ID") and env.get(TURN_TRANSPORT_ENV)):
        return None
    if env.get(WO_KIND_ENV) != "worker" or env.get(REQUIRE_CREW_ENV) != "1":
        return None
    cwd = payload.get("cwd") or ""
    if "/.claude/worktrees/" not in cwd:
        return None
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    try:
        rel = Path(file_path).resolve().relative_to(Path(cwd).resolve())
    except ValueError:
        return None  # outside the worktree: refused elsewhere, not this rule's business
    if ".jarvis" in rel.parts:
        return None  # generated state the lead owns
    seat = "jarvis-spec-writer" if "specs" in rel.parts else "jarvis-implementer"
    return _deny(
        f"Delegate this write to `{seat}`. You are the LEAD: git, the pull request, "
        f"every `jarvis …` command, the work-order record and REVIEW of what each seat "
        f"produced are yours; the spec and the code are not. Send the seat the full "
        f"context — it inherits none of your briefing — and read what it hands back "
        f"before you commit it."
    )


#: A spec's two load-bearing sections, matched on the HEADING alone.
_SPEC_PROBLEM = re.compile(r"problem|what is broken", re.IGNORECASE)
_SPEC_FIX = re.compile(r"\bfix\b|solution", re.IGNORECASE)


def spec_shape_decision(payload: dict[str, Any],
                        env: dict[str, str]) -> dict[str, Any] | None:
    """Refuse a spec file that has no problem section and no fix section.

    §8 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md. At `jarvis wo
    finish` the spec is committed and in the pull request, so the correction costs a
    validation round and a re-delivery; here it costs one retry inside the same turn.

    `Write` only: an Edit payload carries a fragment, not the document, so the same test
    there would refuse every legitimate incremental edit to a conforming spec.
    """
    # kind == worker, so a PLANNER's design doc is untouched: a feature spec is cut into
    # sections a child work order each implements and is validated by `plans` on its own
    # terms (§8 — the user's ruling that feature orders are a different shape).
    if (not env.get("JARVIS_WO_ID") or payload.get("tool_name") != "Write"
            or env.get(WO_KIND_ENV) != "worker"):
        return None
    tool_input = payload.get("tool_input") or {}
    path = Path(tool_input.get("file_path") or "")
    if "specs" not in path.parts or path.suffix != ".md":
        return None
    headings = [m.group(1) for m in _HEADING.finditer(tool_input.get("content") or "")]
    missing = [name for name, pattern in (("problem", _SPEC_PROBLEM), ("fix", _SPEC_FIX))
               if not any(pattern.search(h) for h in headings)]
    if not missing:
        return None
    return _deny(
        f"This spec has no {' and no '.join(missing)} section. A spec states WHAT IS "
        f"BROKEN with evidence — ids, `file:line`, measured facts — and only then HOW "
        f"it is fixed: the mechanism, where it lives, and why there. Name the ROOT "
        f"CAUSE, and say plainly if you are fixing a symptom on purpose. Add the "
        f"headings and write the sections; do not retitle what is already there."
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
        return _resolve_gate(action, wo_id, env, payload, config)
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


def _case_deadline(config: Any) -> str:
    """The sentence that turns the hold into a deadline the worker can see.

    ABANDONED, not refused — spec 2026-09-12 §4.
    """
    return (f"If you do neither within "
            f"{int(config.case_ttl_seconds // 60)} minutes the request is closed as "
            f"ABANDONED — nobody will have reviewed it, nothing will be decided, and the "
            f"command stays blocked.")


def _resolve_gate(action: Any, wo_id: str, env: dict[str, str],
                  payload: dict[str, Any], config: Any) -> dict[str, Any]:
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
        if prior is not None and prior["status"] == gates.AWAITING_CASE:
            return _deny(
                f"Gate `{action.kind}`: request {prior['id']} for this exact command is "
                f"recorded but NOT under review, and retrying the command will not start "
                f"one. It is waiting for you — for a case, or for a contest.\n\n"
                f"{gates.exits_advice(wo_id, action.command, action.kind, prior['id'])}"
                f"\n\n{_case_deadline(config)} Then END YOUR TURN."
            )
        if prior is not None and prior["status"] == "pending":
            return _deny(
                f"Gate `{action.kind}`: approval request {prior['id']} for this exact "
                f"command is already under review. END YOUR TURN — the verdict arrives "
                f"as your next user turn. Do not retry in a loop.\n\n"
                f"If you have not made the case yet, `jarvis gate request` attaches one "
                f"to request {prior['id']} — it never files a second."
            )
        if prior is not None and prior["status"] == "denied":
            return _deny(
                f"Gate `{action.kind}`: this command was DENIED "
                f"(request {prior['id']}, by {prior['decided_by']}): "
                f"{prior['decision_reason'] or 'no reason recorded'}. Do not retry it "
                f"as-is. Address the reason, then `jarvis gate request` afresh."
            )

        wo = store.get_work_order(wo_id)
        # A request already on this work order for the same KIND, whatever string it was
        # filed under — an approval is keyed to the exact command, so the case for this
        # action is routinely on a row this one will not match (kn-237185ed). The
        # placeholder says so instead of asserting the worker never made one (#233).
        same_kind = next((a["id"] for a in store.list_approvals(wo_id)
                          if a["kind"] == action.kind), None)
        neo = NeoStore()
        try:
            # HELD, not queued. You ran the command instead of arguing for it, so there is
            # no case to review yet — and handing a reviewer the placeholder now would get
            # it decided before yours could arrive (GitHub issue 185).
            approval, _ = gates.file_request(
                store, neo, env.get("JARVIS_PROJECT", ""), wo, action,
                justification=gates.no_case_justification(action.command, same_kind),
                hold=True,
                # Which SEAT attempted it, if a subagent did. `JARVIS_WO_ID` is
                # per-session, so the request is filed against the work order either way;
                # this is the only thing that keeps the record from saying the lead ran a
                # command its team ran. `PreToolUse` omits the key for the lead's own
                # calls, so absence is the discriminator, not a sentinel value.
                agent_type=payload.get("agent_type") or None,
            )
        finally:
            neo.close()
        # Both exits, every time, with the diagnosis first — spec 2026-09-12 §3.
        prior_abandoned = (prior is not None and prior["status"] == "expired"
                           and prior["closed_as"] == "abandoned")
        return _deny(
            f"Gate `{action.kind}`: {action.summary} needs approval, so this attempt was "
            f"blocked and request {approval['id']} was recorded.\n\n"
            f"NOBODY IS REVIEWING IT YET, and retrying the command will not change that. "
            + ("The request carries no case and no reviewer is shown one.\n\n"
               if gates.is_no_case(approval["justification"])
               and approval["justification"] != gates.NO_CASE_JUSTIFICATION else
               "You ran it rather than asking, so the request carries no case and no "
               "reviewer is shown one.\n\n")
            + ("A previous request for this exact command was ABANDONED — it timed out "
               "with no case and no contest. Do not do that again: take one of the two "
               "exits below.\n\n" if prior_abandoned else "")
            + f"{gates.exits_advice(wo_id, action.command, action.kind, approval['id'])}"
            f"\n\n"
            f"Either one starts the review. {_case_deadline(config)}\n\n"
            f"Then END YOUR TURN — the verdict arrives as your next user turn, and the "
            f"retry will go through if it is approved or dismissed."
        )
    finally:
        store.close()


def under_review_decision(payload: dict[str, Any],
                          env: dict[str, str]) -> dict[str, Any] | None:
    """Narrow the session to reading and `jarvis …` while a request is under review.

    §2 of docs/superpowers/specs/2026-09-12-a-gate-that-holds.md: matching the exact
    command string again is not a control, because the goal has other routes to it.

    Only `pending`, never `awaiting_case` — a held request is one the worker still has
    to argue, and the evidence a case is made of comes from running things.

    Fails OPEN, unlike `gate_decision`. An unreadable database here would block every
    tool call in every worker session; the privileged command itself stays blocked
    regardless, because the gate that judges it fails closed.

    It opens the store on every Bash call and every file write, and unlike
    `_post_tool_compaction` there is no flag file to keep it to one `stat`: a request
    goes pending in another process (Neo's drain, the user's `jarvis gate request`), so
    anything cached in the worktree would be a stale copy of the fact that matters. The
    cost is in line with the path it sits on — `gate_decision` above it already opens
    `os.db` for the rule base on every Bash call.
    """
    ctx = _worker_context(env, Path(payload.get("cwd") or "."))
    if ctx is None:
        return None
    root, wo_id = ctx
    try:
        store = ProjectStore(root)
        try:
            pending = store.pending_approvals(wo_id)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 — see the docstring
        return None
    if not pending:
        return None

    if payload.get("tool_name") == "Bash":
        from .gate_rules import reads_only

        command = (payload.get("tool_input") or {}).get("command", "")
        if is_jarvis_command_chain(command) or reads_only(command):
            return None
    request = pending[0]
    return _deny(
        f"Gate `{request['kind']}`: approval request {request['id']} is under review, "
        f"so this session may only read and run `jarvis …` commands until the verdict "
        f"lands. END YOUR TURN — the verdict arrives as your next user turn, and this "
        f"call goes through then. Retrying, or reaching the same outcome another way, "
        f"is the thing the gate exists to stop."
    )


def held_request_turn_block(store: ProjectStore, wo_id: str, payload: dict[str, Any],
                            env: dict[str, str]) -> dict[str, Any] | None:
    """Stop: refuse to end a turn that would leave a gate request unargued.

    §1 of the spec above. `awaiting_case` and NOT `pending`, and the asymmetry is the
    whole design: a held request is cleared by one command in this turn, so the block
    terminates, while a pending one is cleared only by a queued verdict that
    `Daemon.deliver_messages` will not deliver into a turn still in flight.

    One continuation, not a loop: `stop_hook_active` says Claude is already going again
    because of this hook, and a worker that ignored the reason twice is better parked
    than spun.
    """
    from . import gates
    from .invariants import TERMINAL_STATUSES

    if payload.get("stop_hook_active"):
        return None
    held = store.held_approvals(wo_id)
    # The SAME set the sweeper reads (`check_no_orphan_gate_requests`), not a second
    # spelling of it — kn-d4d5a967. A status that settles a work order there and not
    # here would leave a dead one held at every turn boundary by a request already
    # superseded.
    if not held or store.get_work_order(wo_id)["status"] in TERMINAL_STATUSES:
        return None

    request = held[0]
    config = gates.GateConfig.from_json(env.get("JARVIS_GATES"))
    store.add_event(wo_id, "gate_turn_held", {
        "session_id": payload.get("session_id", ""),
        "approval_id": request["id"],
    })
    return {
        "decision": "block",
        "reason": (
            f"You may not end this turn: gate request {request['id']} "
            f"({request['kind']}) is recorded but NOT under review, and ending here "
            f"leaves it that way — no reviewer is shown it and nothing else can close "
            f"it. Take one of the two exits now, in this turn.\n\n"
            # BOTH exits, from the same renderer as every other block (spec
            # 2026-09-12-contesting-a-gate-match.md §3). A hold that named only the
            # request would push a worker whose command performs no privileged action
            # into writing a false case — which is the failure that spec exists to stop,
            # and holding the turn would make it harder to walk away from.
            f"{gates.exits_advice(wo_id, request['command'], request['kind'], request['id'])}"
            # `from_json` always returns a config, falling back to the default TTL, so
            # this reads the same clock `Daemon.refuse_unargued_gates` runs on.
            f"\n\n{_case_deadline(config)} "
            f"Then end the turn — the verdict arrives as your next user turn."
        ),
    }


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

    `under_review_decision` sits between the two, and the position is deliberate on both
    sides: after `gate_decision`, so a command a live grant covers still runs; before
    everything else, so the narrowing is not reachable around either.
    """
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        gated = gate_decision(payload, env)
        if gated is not None:
            return gated
        narrowed = under_review_decision(payload, env)
        if narrowed is not None:
            return narrowed
        mistitled = pr_title_decision(payload, env)
        if mistitled is not None:
            return mistitled
        unreviewable = pr_body_decision(payload, env)
        if unreviewable is not None:
            return unreviewable
        # BEFORE the auto-allow below, which would otherwise wave every `jarvis …`
        # through and make the cap unreachable — the same ordering argument this
        # docstring already makes for the two PR checks.
        overlong = finish_summary_decision(payload, env)
        if overlong is not None:
            return overlong
        # Before the auto-allow for the same reason as the cap above it (§4 of
        # docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md).
        detached = background_task_decision(payload, env)
        if detached is not None:
            return detached
        if is_jarvis_command_chain(tool_input.get("command", "")):
            return _allow("jarvis contract command")
        return None

    if tool in ("Edit", "Write", "NotebookEdit") and env.get("JARVIS_WO_ID"):
        narrowed = under_review_decision(payload, env)
        if narrowed is not None:
            return narrowed
        # After the narrowing (a narrowed session is the stricter state) and before the
        # worktree auto-allow, which would otherwise make both unreachable. §7 and §8 of
        # docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md.
        undelegated = crew_edit_decision(payload, env)
        if undelegated is not None:
            return undelegated
        shapeless = spec_shape_decision(payload, env)
        if shapeless is not None:
            return shapeless
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


# -- the prompt prefix, and what moves it -------------------------------------------------
#
# Finding 4 of docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md, whose
# own con is answered below rather than argued with.
#
# THIS IS A PROXY AND `invariants.check_prefix_stable` IS THE MEASUREMENT. A cache
# boundary is a fact the RESPONSE reports, and no hook sits in that path: what this
# records is that an INPUT to the prompt prefix changed between two turns of one
# conversation, which is a reason to expect a re-write, not an observation of one. When
# the two disagree, `check_prefix_stable` wins — it reads the accounting the API itself
# returned, through `usage._usage_of`'s boundary classification. This is the early
# warning, arriving on the turn it happened rather than on the next doctor run over a
# 30-day cohort; that earliness is the whole of what it adds.
#
# AND IT IS NOT A DETECTOR, which is a shortfall against what finding 4 promised action 1
# would be — "a regression is caught at the moment it happens". Two gaps, both structural
# rather than fixable here (Neo q363):
#   · IT CANNOT TELL A REGRESSION FROM AN EDIT. Every ingredient it watches also moves for
#     good reasons — the CLI updates itself, a project's CLAUDE.md gets a new rule — and
#     nothing in a digest distinguishes those from `includeGitInstructions` being flipped
#     back on. It reports that the prefix moved; whether it should have is a judgement,
#     and this makes no claim on it.
#   · NOTHING IS PAGED. A `prefix_drift` event lands on ONE work order's timeline, which
#     is read when somebody opens that work order. The fleet-level "the prefix has got
#     worse" judgement is still INV-PREFIX-DRIFT's, on the doctor's cadence and over its
#     cohort. So what arrives at the moment it happens is the EVIDENCE, and the verdict
#     still arrives later.
# Reading this as the detector finding 4 asked for is therefore a mistake, and it is the
# expensive kind: a fleet that believes it is watched stops looking.
#
# WHAT IT COSTS, since the finding's second con is that it runs on every session for a
# condition that changes rarely: nothing per session that was not already being spent.
# `SessionStart` already runs `jarvis _hook` (`assets/settings.base.json`), so there is no
# new process, and the work added inside it is four small reads and a hash against a
# ~155ms process baseline (`scripts/bench_hook_cost.py` measures both halves).
#
# WHAT IT DOES NOT COVER, so that the ingredient list is not read as exhaustive:
#   · THE MCP TOOL SET — 35% of the post-fix prefix re-writes by volume (finding 4), and
#     deliberately absent. Tool definitions render at position 0, so a server connecting
#     MID-TURN re-writes the entire conversation (kn-f94abf34 (2)); SessionStart runs
#     before that connect, so a fingerprint taken here cannot see the case that costs the
#     money. Finding 4 action 3 is what settles MCP.
#   · (no longer true, kept because the reasoning is) A PROJECT'S STANDING
#     `append_system_prompt` from the catalog was uncovered while reading it meant
#     importing `jarvis.catalog` — ~60ms against a 155ms hook, a 39% tax on every session
#     to watch a field nothing but a human edit moves. It now rides in the worker settings
#     file for the `SubagentStart` hook (`concision.STANDING_PROMPT_ENV`), so the
#     `worker_settings` digest sees it at no extra cost. The work order's own override was
#     always covered, because that row is already loaded.

#: Bounds on the memory-file walk, so the hook's cost cannot grow with someone's rules
#: directory. Exceeding either is not an error: the digest simply covers what fitted, and
#: the same bound applies on both sides of every comparison.
PREFIX_MEMORY_FILE_CAP = 32
PREFIX_MEMORY_BYTE_CAP = 256 * 1024

#: The ingredients, in the order a reader should check them when one moves — which is the
#: order finding 4 puts them in, commonest cause first.
PREFIX_INGREDIENTS = ("cli_version", "git_briefing", "worker_settings", "memory")

#: What an ingredient reads when it could not be established. Drift INTO or OUT OF it is
#: never reported: "I could not tell" and "it changed" are different claims, and a hook
#: that conflates them cries wolf every time a file is briefly unreadable.
PREFIX_UNKNOWN = "?"

_VERSION_DIR = re.compile(r"\d+\.\d+\.\d+")


def _digest(*chunks: bytes) -> str:
    import hashlib

    h = hashlib.sha256()
    for chunk in chunks:
        h.update(len(chunk).to_bytes(8, "big"))  # length-prefixed: no chunk-boundary ties
        h.update(chunk)
    return h.hexdigest()[:16]


def claude_cli_version(env: dict[str, str]) -> str:
    """Which Claude Code built the prompt — the first suspect when the prefix moves.

    Read off disk rather than from `claude --version`: a subprocess would cost more than
    everything else in this hook put together, for a string that is already spelled out
    in the install layout. The updater's own receipt is the fallback, and anything else
    reports unknown rather than guessing.

    TWO LAYOUTS, because getting this wrong is silent. The native installer symlinks
    `claude` at a file whose NAME is the version — verified on this machine, where
    `~/.local/share/claude/versions/2.1.272` is the 218MB executable itself and not a
    directory holding one. The other plausible shape puts the binary INSIDE a
    version-named directory (`…/versions/2.1.272/claude`), where the answer is the
    parent's name. Reading only the first would leave the second at `?` for ever, which
    `prefix_drift` skips on both sides — so the ingredient would be dead and every
    fingerprint-to-fingerprint test would still pass (review round 2).
    """
    import shutil

    exe = shutil.which("claude", path=env.get("PATH") or os.defpath)
    if exe:
        try:
            resolved = Path(exe).resolve()
        except OSError:
            resolved = None
        for candidate in ((resolved.name, resolved.parent.name) if resolved else ()):
            if _VERSION_DIR.fullmatch(candidate):
                return candidate
    receipt = Path.home() / ".claude" / ".last-update-result.json"
    try:
        version = json.loads(receipt.read_text()).get("version_to")
    except (OSError, ValueError, AttributeError):
        return PREFIX_UNKNOWN
    return version if isinstance(version, str) and version else PREFIX_UNKNOWN


def memory_files(root: Path, cwd: Path) -> list[Path]:
    """The CLAUDE.md-shaped files Claude Code loads into the prompt, nearest first.

    The worktree and the directories above it up to the project root, then the user's own
    — which is the CLI's own resolution order, and the order that makes a truncated walk
    truncate the least relevant end.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            found.append(resolved)

    here = cwd.resolve() if cwd.exists() else cwd
    # Both sides resolved, or the stop never matches and the walk climbs out of the
    # project: a worker's cwd is `<root>/.claude/worktrees/<id>`, which is a symlinked
    # path on some checkouts and a plain one on others.
    stop = root.resolve() if root.exists() else root
    for candidate in (here, *here.parents):
        add(candidate / "CLAUDE.md")
        if candidate == stop:
            break
    user = Path.home() / ".claude"
    add(user / "CLAUDE.md")
    for rule in sorted(user.glob("rules/*.md")):
        add(rule)
    return found[:PREFIX_MEMORY_FILE_CAP]


def _memory_digest(root: Path, cwd: Path) -> str:
    chunks: list[bytes] = []
    budget = PREFIX_MEMORY_BYTE_CAP
    for path in memory_files(root, cwd):
        try:
            body = path.read_bytes()[:budget]
        except OSError:
            continue
        chunks += [str(path).encode(), body]  # the path too: a file APPEARING is drift
        budget -= len(body)
        if budget <= 0:
            break
    return _digest(*chunks) if chunks else PREFIX_UNKNOWN


def prefix_fingerprint(root: Path, cwd: Path, wo: dict[str, Any],
                       env: dict[str, str]) -> dict[str, str]:
    """One token per ingredient of this work order's prompt prefix.

    A version is recorded VERBATIM and everything else as a digest, because the drift
    report is read by a person: "2.1.271 -> 2.1.272" is the answer, and a pair of hashes
    is only the question restated.

    Compared within ONE work order and never across two. That is not a simplification —
    it is the cache's own scope. A prefix that differs between two conversations costs
    nothing, since each writes its own entry; a prefix that moves between turn 4 and
    turn 5 of one conversation re-writes turns 1-4. Comparing across work orders would
    report the per-work-order lines in the settings file as drift on every dispatch.
    """
    from .worker_brief import git_briefing

    settings = (root / ".jarvis" / "worker-settings" / f"{wo['id']}.json")
    try:
        settings_bytes = settings.read_bytes()
    except OSError:
        settings_bytes = b""
    briefing = git_briefing(wo.get("model"))
    standing = wo.get("append_system_prompt") or ""
    return {
        "cli_version": claude_cli_version(env),
        "git_briefing": _digest(briefing.encode(), standing.encode()),
        "worker_settings": (_digest(settings_bytes) if settings_bytes
                            else PREFIX_UNKNOWN),
        "memory": _memory_digest(root, cwd),
    }


def prefix_baseline(root: Path, wo_id: str) -> Path:
    return root / ".jarvis" / "prefix" / f"{wo_id}.json"


#: How many recorded fingerprints `unreadable_ingredients` reads. It runs only when
#: INV-PREFIX-DRIFT is already firing, so the cap is about a pathological directory rather
#: than about a hot path.
PREFIX_BASELINE_SCAN_CAP = 50


def unreadable_ingredients(root: Path) -> set[str]:
    """Which ingredients this machine could not establish at all, over recent fingerprints.

    THE FAILURE THIS EXISTS FOR IS SILENT, AND IT MISDIRECTS. `cli_version` is the only
    ingredient parsed out of the environment rather than hashed from bytes, so on a
    machine whose install layout this does not recognise it is `?` for ever — and `?` is
    skipped on both sides of every comparison by design, so a CLI upgrade is simply never
    reported. That is not silence: INV-PREFIX-DRIFT tells the reader that a crossing with
    nothing named points at MCP, so a dead ingredient actively sends them to the wrong
    suspect. Naming it is what turns that back into a known unknown (review round 1).
    """
    found: set[str] = set()
    directory = root / ".jarvis" / "prefix"
    try:
        recorded = sorted(directory.iterdir())[:PREFIX_BASELINE_SCAN_CAP]
    except OSError:
        return found
    for path in recorded:
        try:
            fingerprint = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(fingerprint, dict):
            continue
        found |= {name for name in PREFIX_INGREDIENTS
                  if fingerprint.get(name) == PREFIX_UNKNOWN}
    return found


def prefix_drift(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    """Which ingredients moved. An ingredient unknown on either side is not one of them."""
    return tuple(
        name for name in PREFIX_INGREDIENTS
        if before.get(name, PREFIX_UNKNOWN) != after.get(name, PREFIX_UNKNOWN)
        and PREFIX_UNKNOWN not in (before.get(name, PREFIX_UNKNOWN),
                                   after.get(name, PREFIX_UNKNOWN))
    )


def note_prefix(payload: dict[str, Any], env: dict[str, str], root: Path,
                store: ProjectStore, wo: dict[str, Any]) -> tuple[str, ...]:
    """SessionStart: record this turn's fingerprint, and the drift since the last one.

    Returns the ingredients that moved, so the caller can say so; `()` covers both "the
    prefix held" and "there was nothing yet to compare against", which read the same from
    out here and need no distinction — the first turn of a conversation writes a cold
    entry either way.

    An EARLY WARNING and not a measurement: `invariants.check_prefix_stable` reads the
    cache accounting the API itself returned and is the number that wins when the two
    disagree — see the section comment above for why nothing here can see a boundary.

    Records, and does not alarm. An ingredient changing is ORDINARY: an edit to a
    project's CLAUDE.md legitimately moves the prefix for every worker in it, so a proxy
    that raised its own violation here would fire on routine edits and would be a second
    prefix-drift verdict standing beside `invariants.check_prefix_stable`'s with no rule
    saying which to believe (Neo q363, and kn-376c88eb is that failure in general).
    """
    wo_id = wo["id"]
    cwd = Path(payload.get("cwd") or env.get("PWD") or ".")
    current = prefix_fingerprint(root, cwd, wo, env)
    path = prefix_baseline(root, wo_id)
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        loaded = None
    before: dict[str, str] = loaded if isinstance(loaded, dict) else {}

    changed = prefix_drift(before, current) if before else ()
    if changed:
        store.add_event(wo_id, "prefix_drift", {
            "changed": list(changed),
            "before": {name: before[name] for name in changed if name in before},
            "after": {name: current[name] for name in changed},
            "session_id": payload.get("session_id") or "",
            "source": payload.get("source") or "",
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current))
    return changed


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
    """PreCompact: record that it happened and arm the re-assertion.

    THE FLAG IS ARMED WHICHEVER WAY THE COMPACTION CAME, and that is the half that
    matters: a worker resumed after the OS compacted for it (`worker_session.compact`)
    needs its identifiers re-asserted exactly as much as one Claude Code compacted
    mid-turn — more so, since a `/compact` turn does no tool call of its own and the
    brief therefore lands at the start of the NEXT turn.

    The EVENT is only written for a compaction the OS did not ask for. A headless
    worker cannot type `/compact`, so `manual` here means Jarvis sent it, and
    `worker_session._record_compaction` already writes that one — with what it achieved
    and what it cost, which this side cannot see.
    """
    if payload.get("trigger") != "manual":
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


def _worker_context(env: dict[str, str], cwd: Path) -> tuple[Path, str] | None:
    """(project root, wo id) for a dispatched worker, or None for anything else."""
    wo_id = env.get("JARVIS_WO_ID")
    if not wo_id:
        return None  # interactive session — Jarvis governs dispatched workers only
    root_env = env.get("JARVIS_PROJECT_PATH")
    root = Path(root_env) if root_env else find_project_root(cwd)
    if root is None or not (root / ".jarvis").is_dir():
        return None
    return root, wo_id


def _pre_compact(payload: dict[str, Any], env: dict[str, str],
                 cwd: Path) -> dict[str, Any] | None:
    ctx = _worker_context(env, cwd)
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
    ctx = _worker_context(env, cwd)
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
    """What this work order is parked on instead of the user — "" when nothing is.

    Only ever consulted for a `waiting_input` work order, which is the state every wait
    puts it in: `ops.ask_question`, and `gates.file_request` down both its roads — the
    argued request `jarvis gate request` files, and the held one this hook files itself
    when a worker runs the command first. A `running` worker's Notification is a real
    mid-work block until proven otherwise, and swallowing that would strand it.

    The returned reason is recorded verbatim on the `notification_ignored` event, so it
    must name WHICH wait: "parked" and "parked on something a reviewer is holding" are
    different facts to whoever reads that timeline afterwards.
    """
    if store.get_work_order(wo_id)["status"] != "waiting_input":
        return ""
    from .invariants import awaiting_neo

    question = awaiting_neo(wo_id)
    if question is not None:
        return f"neo question {question['id']} ({question['status']})"
    if store.pending_approvals(wo_id):
        return "a privileged-action gate awaiting a verdict"
    if store.held_approvals(wo_id):
        # A fourth reader of the held state, beyond the three kn-30036661 lists as the
        # complete set. Held is with the WORKER, not the user: nobody is reviewing it,
        # and the OS refuses it on a timer if nobody ever argues it.
        return "a privileged-action gate awaiting the worker's case"
    return ""


def _subagent_start(payload: dict[str, Any],
                    env: dict[str, str]) -> dict[str, Any] | None:
    """Re-deliver to a Task subagent the instructions it does not inherit.

    Measured on Claude Code 2.1.278: a subagent inherits CLAUDE.md, the skill listing,
    the parent's `--settings` (so `permissions.allow`/`deny` still apply to its tool
    calls) and the hooks; it does NOT inherit the parent's `--append-system-prompt`, its
    `--agent` persona, or anything `SessionStart` injected -- `SessionStart` never fires
    for a subagent at all. What is lost is instruction, not authorisation.

    No database and no catalog, for `finish_summary_decision`'s reason: both halves of
    the text are fixed at spawn, one static and one already in the environment, and a
    hook that opened the project DB to serve them would pay for it on every Task call.
    `JARVIS_WO_ID` is therefore the whole test of "is this a worker" -- a session the
    user opened themselves gets nothing.
    """
    if not env.get("JARVIS_WO_ID"):
        return None
    return {"wo_id": env["JARVIS_WO_ID"], "event": "SubagentStart",
            "agent_type": payload.get("agent_type"),
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": concision.subagent_context(env),
            }}


def _git_index(tree: Path, *args: str) -> tuple[int, str] | None:
    """One git command against a worktree's index: `(exit status, stdout)`, or `None`
    when git could not be run at all.

    `landing._git` is deliberately not reused: that helper's contract is "one READ-ONLY
    git command" and this writes the index. `import subprocess` is lazy because
    `hooks.py` does not import it at module level and this module's import cost is
    measured (see `TOOL_MANAGED_PATHS_ENV`).

    The exit status is returned rather than folded into `None` so the recorded event can
    name it: a failure must land on the record, and "git failed" without the code is not
    a fact anyone can act on.
    """
    import subprocess

    try:
        out = subprocess.run(["git", "-C", str(tree), *args],
                             capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.returncode, out.stdout


def mark_tool_managed_paths(env: dict[str, str], root: Path, store: ProjectStore,
                            wo: dict[str, Any], cwd: Path) -> None:
    """Tell THIS work order's worktree that the tool-managed files are not its to report.

    `git update-index --skip-worktree` means "the worktree copy of this path is not
    mine": the tool keeps rewriting it, the worker keeps reading it, and `git status`,
    `git add -A` and `git commit -a` all stop seeing it — so the churn can no longer be
    staged into a pull request about something else. Spec docs/superpowers/specs/
    2026-09-25-serena-config-churn-and-tool-managed-files.md.

    Nothing happens on the shared checkout, by design: a schema upgrade there is the
    user's to see and commit, and hiding it from `git status` would hide it from the only
    person who can land it. Nothing happens for another work order's worktree either.

    Idempotent — every turn's `SessionStart` re-runs the same steps, and a path already
    carrying the flag is neither re-marked nor recorded.
    """
    raw = env.get(TOOL_MANAGED_PATHS_ENV)
    if not raw:
        return
    try:
        configured = json.loads(raw)
    except ValueError:
        return
    if not isinstance(configured, list):
        return

    # Both conditions, in this order: the cheap text test first (hooks.py:441), then the
    # identity of the worktree. Both sides resolved — a worker's cwd is a symlinked path
    # on some checkouts and a plain one on others, which is why `find_project_root`
    # resolves too.
    worktree = wo.get("worktree")
    if not worktree or "/.claude/worktrees/" not in str(cwd):
        return
    expected = root / ".claude" / "worktrees" / str(worktree)
    try:
        if cwd.resolve() != expected.resolve():
            return
    except OSError:
        return

    marked: list[str] = []
    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}
    for path in [str(p) for p in configured[:MAX_TOOL_MANAGED_PATHS]]:
        # `-v` for the same call that answers "does git track it": the flag letter is what
        # makes "already marked" a state this can pass over without recording it again.
        listed = _git_index(cwd, "ls-files", "-v", "--", path)
        if listed is None or listed[0] != 0:
            failed[path] = "ls-files: git unavailable" if listed is None \
                else f"ls-files: exit {listed[0]}"
            continue
        line = listed[1].strip()
        if not line:
            skipped[path] = "untracked"   # update-index would exit non-zero anyway
            continue
        if line.split(" ", 1)[0] == "S":
            continue                      # already marked: the first event said so
        if not (cwd / path).exists():
            # `skip-worktree` on an absent path makes git present the index version as
            # the truth — a confusing state to create for a file the tool has not written.
            skipped[path] = "absent"
            continue
        result = _git_index(cwd, "update-index", "--skip-worktree", "--", path)
        if result is None or result[0] != 0:
            failed[path] = "update-index: git unavailable" if result is None \
                else f"update-index: exit {result[0]}"
            continue
        marked.append(path)

    # Every DISTINCT reason lands once. The one case that records nothing is "every
    # configured path is already marked", which is not a new reason — `SessionStart` fires
    # once per TURN, and a row per turn would make the record a hook log (timeline.py:19).
    if marked or skipped or failed:
        store.add_event(wo["id"], "tool_managed_paths",
                        {"marked": marked, "skipped": skipped, "failed": failed})


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

    if event == "SubagentStart":
        return _subagent_start(payload, env)

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
            # ...and the prefix fingerprint, which wants exactly this moment: once per
            # turn, before the turn's first API call. A failure here is never the
            # session's problem — the signal is an early warning and the measurement is
            # elsewhere, so a broken read costs a data point and nothing else.
            try:
                note_prefix(payload, env, root, store, wo)
            except Exception:  # noqa: BLE001
                pass
            # ...and the tool-managed files, marked `skip-worktree` in this worktree's
            # index. AFTER the two above: the git calls cost tens of milliseconds and
            # nothing already in this branch may be delayed by them. Never fatal, for
            # `note_prefix`' reason and more sharply — a worker's session must not end
            # because git is unhappy about a file it was not going to commit anyway.
            try:
                mark_tool_managed_paths(env, root, store, wo, cwd)
            except Exception:  # noqa: BLE001
                pass
            # ...and the house style, which is the whole point of this event for
            # concision (spec 2026-09-19 SS5.1). SessionStart fires once per turn,
            # before the turn's first API call, which is the only place a rule can
            # reach EVERY byte the turn generates without depending on the model
            # electing to load a skill — the failure SS1 measured, 0 invocations in 142
            # sessions. `additionalContext` is the same channel the compaction brief
            # already rides on `PostToolUse`.
            #
            # It appends after the cached conversation rather than editing the system
            # prompt, so it does not move the prefix `note_prefix` just fingerprinted.
            return {"wo_id": wo_id, "event": event,
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": concision.house_style(),
                    }}

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
            # means for the work order is settled from the turn row, not from here — with
            # one exception, which is the only mechanism the runtime offers for holding a
            # session at a turn boundary.
            blocked = held_request_turn_block(store, wo_id, payload, env)
            if blocked is not None:
                return blocked

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
        # `decision` is the Stop hook's own shape — it has no `hookSpecificOutput`, and
        # printing nothing is what makes a block advisory.
        if result and ("hookSpecificOutput" in result or "decision" in result):
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
