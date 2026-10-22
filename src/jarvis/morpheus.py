"""Morpheus: every shipped PR validated in production (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from . import db

if TYPE_CHECKING:  # pragma: no cover
    from .catalog import Catalog, MorpheusConfig
    from .central_store import CentralStore

log = logging.getLogger("jarvisd.morpheus")

STATES = ("queued", "monitoring", "validated", "failed", "timedout")
OPEN_STATES = ("queued", "monitoring")
TERMINAL_STATES = ("validated", "failed", "timedout")
#: The terminal states that must cause something
ACTION_STATES = ("failed", "timedout")

#: The PR body heading every change must carry
SECTION = "How to validate in production"

#: The script contract
EXIT_VALIDATED = 0
EXIT_FAILED = 1

#: The os_state key holding Morpheus's session
SESSION_KEY = "morpheus.session"
#: The os_state key naming the last release whose PRs were queued
RELEASE_KEY = "morpheus.last_release"

#: How long an item offered to Morpheus waits before it is offered again
REOFFER_SECONDS = 6 * 3600
#: How many times an item is offered before Morpheus gives up on it (the deadline then acts)
MAX_OFFERS = 3
#: How much of a script's output is kept
OUTPUT_TAIL = 2000

SECONDS_PER_DAY = 86400


# -- the PR section

_FENCE = re.compile(r"^\s{0,3}(```|~~~)\s*([\w+-]*)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_LANGS = ("bash", "sh", "shell")
#: `None — <reason>` (or `None.`), and not prose that merely starts with the word
_WAIVED = re.compile(r"^\s*[-*]?\s*none\s*(?:[—–:.-]+\s*(.*))?\Z", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Section:
    """What a PR said about validating it"""

    #: The section's text, comments stripped
    text: str
    #: The first fenced bash/sh block, or ""
    script: str = ""
    #: The reason, when the PR says `None — <reason>`; "" otherwise
    waived: str = ""

    @property
    def empty(self) -> bool:
        return not _meaningful(self.text)


def _meaningful(text: str) -> bool:
    """Whether anything is left once template scaffolding is gone"""
    return any(ln.strip() not in ("", "-", "*") for ln in text.splitlines())


def find_section(body: str) -> str | None:
    """The raw text under the validation heading, or None when the body has no such heading"""
    # Comments go first, keeping their line breaks: a template's comment may itself show a ``` fence
    text = _HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), body or "")
    lines = text.splitlines()
    fence = ""
    start: int | None = None
    level = 0
    for i, line in enumerate(lines):
        m = _FENCE.match(line)
        if m:
            if not fence:
                fence = m.group(1)
            elif line.strip().startswith(fence):
                fence = ""
            continue
        if fence:
            continue
        h = _HEADING.match(line)
        if not h:
            continue
        if start is not None and len(h.group(1)) <= level:
            return "\n".join(lines[start:i])
        if start is None and h.group(2).strip().lower() == SECTION.lower():
            start, level = i + 1, len(h.group(1))
    return None if start is None else "\n".join(lines[start:])


def parse_section(raw: str) -> Section:
    """Split a section into prose, the script, and a waiver"""
    text = _HTML_COMMENT.sub("", raw or "").strip()
    script, block, lang, fence = "", [], "", ""
    for line in text.splitlines():
        m = _FENCE.match(line)
        if not fence and m:
            fence, lang, block = m.group(1), m.group(2).lower(), []
            continue
        if fence and line.strip().startswith(fence):
            if lang in _SCRIPT_LANGS and not script:
                script = "\n".join(block).strip()
            fence = ""
            continue
        if fence:
            block.append(line)
    waived = ""
    w = _WAIVED.match(text)
    if w and not script:
        waived = ((w.group(1) or "").strip() or "no reason given").splitlines()[0][:500]
    return Section(text=text, script=script, waived=waived)


def section_problem(body: str) -> str | None:
    """Why this PR body fails the rule, or None"""
    raw = find_section(body)
    if raw is None:
        return (f"no `## {SECTION}` section — say how production proves this change "
                f"works: hints, a ```bash script (exit 0 validated, 1 failed, anything "
                f"else not yet), or `None — <reason>`")
    if parse_section(raw).empty:
        return f"`## {SECTION}` is still the empty template"
    return None


# -- the release: which PRs it carries

_MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b")
_SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
_WO_PREFIX = re.compile(r"^\[(wo-[0-9a-f]{8})\]")


def pr_numbers(subjects: Iterable[str]) -> list[int]:
    """PR numbers named by first-parent commit subjects, oldest first, deduplicated"""
    seen: list[int] = []
    for subject in subjects:
        m = _MERGE_SUBJECT.match(subject) or _SQUASH_SUBJECT.search(subject)
        if m and int(m.group(1)) not in seen:
            seen.append(int(m.group(1)))
    return seen


def wo_of_title(title: str) -> str:
    """The work order a PR title names (`[wo-1234abcd] …`), or ''"""
    m = _WO_PREFIX.match(title or "")
    return m.group(1) if m else ""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, timeout=60).stdout


def range_subjects(repo: Path, from_ref: str, to_ref: str) -> list[str]:
    """First-parent subjects of `from..to`, oldest first; the whole history when from is ''"""
    span = f"{from_ref}..{to_ref}" if from_ref else to_ref
    out = _git(repo, "log", "--first-parent", "--reverse", "--format=%s", span)
    return [ln for ln in out.splitlines() if ln.strip()]


def previous_tag(repo: Path, tag: str) -> str:
    """The `jarvis-*` tag released before `tag`, by version order, or ''"""
    tags = [t for t in _git(repo, "tag", "--list", "jarvis-*", "--sort=v:refname").split()
            if t]
    if tag not in tags:
        return tags[-1] if tags else ""
    i = tags.index(tag)
    return tags[i - 1] if i else ""


@dataclass(frozen=True)
class PullRequestBody:
    number: int
    url: str
    title: str
    body: str


def fetch_pr(repo_slug: str, number: int, cwd: Path | None = None) -> PullRequestBody:
    """One PR's title and body over `gh`"""
    from .bugreport import gh_bin

    url = f"https://github.com/{repo_slug}/pull/{number}"
    out = subprocess.run([gh_bin(), "pr", "view", url, "--json", "number,title,body,url"],
                         capture_output=True, text=True, timeout=60,
                         cwd=str(cwd) if cwd else None)
    if out.returncode != 0:
        raise RuntimeError(f"`gh pr view {url}` failed: "
                           f"{(out.stderr or out.stdout).strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    return PullRequestBody(number=int(data.get("number") or number),
                           url=str(data.get("url") or url),
                           title=str(data.get("title") or ""),
                           body=str(data.get("body") or ""))


def repo_slug(repo: Path) -> str:
    """`owner/name` of the repository's origin, or ''"""
    from . import github

    found = github.origin_repo(repo)
    return f"{found[0]}/{found[1]}" if found else ""


# -- the queue

def enqueue(central: CentralStore, cfg: MorpheusConfig, pr: PullRequestBody, *,
            repo: str, release_tag: str = "", now: float | None = None
            ) -> dict[str, Any] | None:
    """Queue one PR in the state its section earns; None when it was already queued"""
    now = db.now() if now is None else now
    raw = find_section(pr.body)
    section = parse_section(raw or "")
    if raw is None or section.empty:
        state, script, source, resolution = "queued", "", "", ""
    elif section.waived:
        state, script, source = "validated", "", ""
        resolution = f"waived by the PR: {section.waived}"
    elif section.script:
        state, script, source, resolution = "monitoring", section.script, "pr", ""
    else:
        state, script, source, resolution = "queued", "", "", ""
    return central.add_morpheus_item(
        repo=repo, pr_number=pr.number, pr_url=pr.url, pr_title=pr.title[:300],
        wo_id=wo_of_title(pr.title), release_tag=release_tag,
        hints=(raw or "").strip()[:8000], script=script, script_source=source,
        state=state, deadline_ts=now + cfg.timeout_days * SECONDS_PER_DAY,
        resolution=resolution, now=now)


def enqueue_release(central: CentralStore, cfg: MorpheusConfig, repo: Path, tag: str, *,
                    from_ref: str | None = None, to_ref: str | None = None,
                    fetch: Callable[[str, int], PullRequestBody] | None = None,
                    slug: str | None = None) -> dict[str, Any]:
    """Queue every PR a release carries — idempotent, so shipit and the daemon may both call it"""
    from_ref = previous_tag(repo, tag) if from_ref is None else from_ref
    to_ref = to_ref or tag
    slug = repo_slug(repo) if slug is None else slug
    fetch = fetch or (lambda s, n: fetch_pr(s, n, repo))
    numbers = pr_numbers(range_subjects(repo, from_ref, to_ref))
    queued, skipped, errors = [], [], []
    for n in numbers:
        try:
            pr = fetch(slug, n)
        except Exception as e:  # noqa: BLE001 — one unreadable PR must not drop the rest
            errors.append(f"#{n}: {e}")
            continue
        item = enqueue(central, cfg, pr, repo=slug, release_tag=tag)
        (queued if item else skipped).append(item["id"] if item else n)
    central.set_state(RELEASE_KEY, tag)
    log.info("morpheus: release %s (%s..%s) — %d queued, %d already known, %d unreadable",
             tag, from_ref or "∅", to_ref, len(queued), len(skipped), len(errors))
    return {"release": tag, "from": from_ref, "to": to_ref, "prs": numbers,
            "queued": queued, "already_queued": skipped, "errors": errors}


def running_release(repo: Path) -> str:
    """The `jarvis-*` tag a production checkout sits on, or '' (dev checkouts sit on a branch)"""
    try:
        return _git(repo, "describe", "--tags", "--exact-match", "--match", "jarvis-*",
                    "HEAD").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def ensure_release_queued(central: CentralStore, cfg: MorpheusConfig, repo: Path,
                          **kwargs: Any) -> dict[str, Any] | None:
    """The daemon's backstop: queue the running release once, if shipit did not"""
    tag = running_release(repo)
    if not tag or central.get_state(RELEASE_KEY) == tag:
        return None
    return enqueue_release(central, cfg, repo, tag, **kwargs)


def set_script(central: CentralStore, item_id: str, script: str, source: str) -> dict:
    """Give an item its script; a queued item starts monitoring now"""
    item = _item_or_raise(central, item_id)
    script = (script or "").strip()
    if not script:
        raise ValueError("an empty script validates nothing")
    fields: dict[str, Any] = {"script": script, "script_source": source}
    if item["state"] == "queued":
        fields.update(state="monitoring", next_check_ts=db.now())
    central.update_morpheus_item(item_id, **fields)
    return central.morpheus_item(item_id) or {}


def waive(central: CentralStore, item_id: str, reason: str) -> dict:
    """The user rules the change validated"""
    _item_or_raise(central, item_id)
    if not (reason or "").strip():
        raise ValueError("say why it counts as validated")
    central.update_morpheus_item(item_id, state="validated",
                                 resolution=f"waived by the user: {reason.strip()}",
                                 next_check_ts=None)
    return central.morpheus_item(item_id) or {}


def requeue(central: CentralStore, cfg: MorpheusConfig, item_id: str) -> dict:
    """Watch it again with a fresh deadline (and a fresh action)"""
    item = _item_or_raise(central, item_id)
    now = db.now()
    central.update_morpheus_item(
        item_id, state="monitoring" if item["script"] else "queued",
        deadline_ts=now + cfg.timeout_days * SECONDS_PER_DAY,
        next_check_ts=now if item["script"] else None, resolution="", action="",
        action_ts=None)
    _forget_offer(central, item_id)
    return central.morpheus_item(item_id) or {}


def _item_or_raise(central: CentralStore, item_id: str) -> dict[str, Any]:
    item = central.morpheus_item(item_id)
    if not item:
        raise KeyError(f"no morpheus item {item_id!r}")
    return item


# -- the mechanical half: running scripts

@dataclass(frozen=True)
class CheckResult:
    exit: int | None
    output: str


def run_script(script: str, cwd: Path, timeout: int) -> CheckResult:
    """Run one validation script; a timeout or a crash is "not yet", never a verdict"""
    try:
        proc = subprocess.run(["bash", "-c", script], cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout,
                              env={**os.environ, "JARVIS_MORPHEUS": "1"})
    except subprocess.TimeoutExpired:
        return CheckResult(None, f"timed out after {timeout}s")
    except OSError as e:
        return CheckResult(None, f"could not run: {e}")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return CheckResult(proc.returncode, out[-OUTPUT_TAIL:])


def check(central: CentralStore, cfg: MorpheusConfig, item: dict[str, Any], cwd: Path,
          *, now: float | None = None,
          run: Callable[[str, Path, int], CheckResult] = run_script) -> str:
    """Run an item's script once and move it; returns its state afterwards"""
    now = db.now() if now is None else now
    result = run(item["script"], cwd, cfg.script_timeout_seconds)
    fields: dict[str, Any] = {"last_check_ts": now, "checks": int(item["checks"]) + 1,
                              "last_exit": result.exit, "last_output": result.output,
                              "next_check_ts": now + cfg.check_every_minutes * 60}
    if result.exit == EXIT_VALIDATED:
        fields.update(state="validated", next_check_ts=None,
                      resolution="the validation script passed in production")
    elif result.exit == EXIT_FAILED:
        fields.update(state="failed", next_check_ts=None,
                      resolution="the validation script reported the change broken")
    elif now >= float(item["deadline_ts"]):
        fields.update(state="timedout", next_check_ts=None,
                      resolution=_timeout_reason(item, cfg))
    central.update_morpheus_item(item["id"], **fields)
    return str(fields.get("state", item["state"]))


def _timeout_reason(item: dict[str, Any], cfg: MorpheusConfig) -> str:
    if not item.get("script"):
        return (f"no validation script existed within {cfg.timeout_days:g} day(s) "
                f"— nothing ever checked this change")
    return (f"{cfg.timeout_days:g} day(s) passed and the script never reached a verdict "
            f"(last exit {item.get('last_exit')})")


def sweep(central: CentralStore, cfg: MorpheusConfig, cwd: Path, *,
          now: float | None = None,
          run: Callable[[str, Path, int], CheckResult] = run_script) -> dict[str, int]:
    """Run every due script and expire every overdue item — no model, ever"""
    now = db.now() if now is None else now
    moved = {s: 0 for s in STATES}
    for item in central.morpheus_items(OPEN_STATES):
        if item["state"] == "monitoring" and item["script"]:
            due = item["next_check_ts"]
            if due is None or float(due) <= now:
                moved[check(central, cfg, item, cwd, now=now, run=run)] += 1
        elif now >= float(item["deadline_ts"]):
            central.update_morpheus_item(item["id"], state="timedout", next_check_ts=None,
                                         resolution=_timeout_reason(item, cfg))
            moved["timedout"] += 1
    return moved


# -- failed and timed out must act

def owed_actions(central: CentralStore) -> list[dict[str, Any]]:
    """Terminal items that still owe their action"""
    return [i for i in central.morpheus_items(ACTION_STATES) if not i["action"]]


def act(central: CentralStore, catalog: Catalog, item: dict[str, Any], *,
        investigate: Callable[..., tuple[str, str]] | None = None,
        ask_neo: Callable[..., int] | None = None) -> str:
    """Take an item's action exactly once; returns what was recorded"""
    cfg = catalog.os.morpheus
    route = cfg.on_failed if item["state"] == "failed" else cfg.on_timeout
    investigate = investigate or _investigate
    ask_neo = ask_neo or _ask_neo
    note = ""
    if route == "investigate":
        inv_id, note = investigate(catalog, item)
        if inv_id:
            action = f"investigation:{inv_id}"
            if central.claim_morpheus_action(item["id"], action):
                central.add_inbox(
                    project=_project_of(catalog), level="warning",
                    title=f"PR #{item['pr_number']} {item['state']} production validation",
                    body=f"{item['pr_url']}\n{item['resolution']}\n"
                         f"{inv_id} is investigating {item['wo_id']}.")
            return action
    qid = ask_neo(catalog, item, note)
    action = f"neo:{qid}"
    central.claim_morpheus_action(item["id"], action)
    return action


def _project_of(catalog: Catalog) -> str:
    from . import schedule

    return schedule.os_project((p.name, p.path) for p in catalog.projects) or "os"


def _investigate(catalog: Catalog, item: dict[str, Any]) -> tuple[str, str]:
    """(investigation id, '') or ('', why not) — through the steward, like every OS-filed order"""
    from . import ops, steward

    wo_id = item.get("wo_id") or ""
    if not wo_id:
        return "", "the PR names no work order to investigate"
    try:
        project, _path, _wo = ops.find_work_order(wo_id, None)
    except Exception as e:  # noqa: BLE001
        return "", f"its work order {wo_id} could not be found ({e})"
    decision = steward.gate(
        steward.Candidate("morpheus", "critical", project,
                          f"PR #{item['pr_number']} {item['state']} production validation",
                          evidence=(item.get("last_output") or item["resolution"],),
                          parent_wo_id=wo_id),
        catalog=catalog)
    if not decision.allowed:
        return "", f"the steward did not dispatch it ({decision.outcome}: {decision.reason})"
    live = ops.live_investigation(project, wo_id)
    if live:
        return live, ""
    inv = ops.create_investigation_order(project, wo_id, why=_why(item), origin="jarvis")
    return str(inv["id"]), ""


def _why(item: dict[str, Any]) -> str:
    return (f"Morpheus: PR #{item['pr_number']} ({item['pr_url']}), shipped in "
            f"{item['release_tag'] or 'a release'}, {item['state']} its production "
            f"validation. {item['resolution']}.\n\n"
            f"The PR said, under `## {SECTION}`:\n{item['hints'][:3000]}\n\n"
            f"The script ({item['script_source'] or 'none'}):\n{item['script'][:3000]}\n\n"
            f"Last exit {item['last_exit']}, output tail:\n{item['last_output'][-1500:]}")


VALIDATION_QUESTION = """MORPHEUS — a shipped change did not validate in production.

{why}

Decide for the user. If this needs them (the change is likely broken in production, or
nothing can tell), ESCALATE and say what they should look at. If it does not (the script
was wrong, the code path is rare and nothing suggests harm), answer with your reading in
two sentences; that answer is recorded on the item and the item stays {state}. They can
re-watch it with `jarvis morpheus requeue {id}` or rule it fine with
`jarvis morpheus waive {id} --reason "…"`."""


def _ask_neo(catalog: Catalog, item: dict[str, Any], note: str = "") -> int:
    from .neo_store import NeoStore

    why = _why(item) + (f"\n\nNo investigation was filed: {note}." if note else "")
    neo = NeoStore()
    try:
        q = neo.ask(_project_of(catalog), "", VALIDATION_QUESTION.format(
            why=why, state=item["state"], id=item["id"]),
            context=json.dumps({"morpheus_item": item["id"]}), kind="validation")
    finally:
        neo.close()
    return int(q["id"])


def item_of_question(q: dict[str, Any]) -> str:
    """The item a `validation` question is about, or ''"""
    try:
        return str((json.loads(q.get("context") or "{}") or {}).get("morpheus_item") or "")
    except (ValueError, AttributeError):
        return ""


def record_neo_verdict(central: CentralStore, q: dict[str, Any],
                       verdict: dict[str, Any], question_url: str = "") -> None:
    """Neo ruled on a `validation` question: record it, and tell the user what it means"""
    item_id = item_of_question(q)
    item = central.morpheus_item(item_id) if item_id else None
    if not item:
        log.warning("validation verdict for question %s names no item", q.get("id"))
        return
    reading = (verdict.get("answer") or verdict.get("reason") or "").strip()
    central.update_morpheus_item(
        item_id, resolution=f"{item['resolution']} — Neo: {reading[:600]}".strip(" —"))
    escalated = bool(verdict.get("escalate"))
    central.add_inbox(
        project=q.get("project") or "os", level="warning" if escalated else "info",
        title=(f"PR #{item['pr_number']} {item['state']} production validation"
               + (" — Neo escalated" if escalated else " — Neo's reading")),
        body=f"{item['pr_url']}\n{reading[:600]}\n"
             + (f"{question_url}\n" if question_url else "")
             + f"jarvis morpheus show {item_id}")


# -- the only LLM: Morpheus's session writes scripts

MORPHEUS_PERSONA = """You are MORPHEUS, the production validator of the Jarvis agentic OS.

The OS ships pull requests to production. Each one says, under `## How to validate in
production`, how production activity proves it works. When a PR gave only HINTS, your job
is to turn them into a SCRIPT the OS runs mechanically on a schedule. You never run the
validation yourself, and you never judge the result — the OS does, from the exit code.

For each item you are given, spawn ONE subagent (Task tool), in parallel. Each subagent:
  1. reads the PR's hints (and the PR or the code if it must: `gh pr view <url>`);
  2. writes a bash script to /tmp/morpheus-<item id>.sh that:
     - is READ-ONLY: it inspects production (the `jarvis` CLI with --json, logs under
       $JARVIS_HOME, `systemctl --user status`, `gh` reads) and never changes anything;
     - exits 0 when production shows the change working, 1 when it shows it broken,
       and any other code (use 75) when production has not exercised it yet;
     - finishes in well under a minute;
  3. registers it: `jarvis morpheus script <item id> --file /tmp/morpheus-<item id>.sh`.
If the hints cannot be made mechanical, the subagent registers a script that exits 75 and
prints why — the item then times out and a human decides.

Reply with one line per item: its id and what the script checks. Nothing else."""


def session_state(central: CentralStore) -> dict[str, Any]:
    raw = central.get_state(SESSION_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save_session(central: CentralStore, state: dict[str, Any]) -> None:
    central.set_state(SESSION_KEY, json.dumps(state))


def _forget_offer(central: CentralStore, item_id: str) -> None:
    state = session_state(central)
    if item_id in (state.get("offers") or {}):
        state["offers"].pop(item_id)
        _save_session(central, state)


def wanting_scripts(central: CentralStore, *, now: float | None = None) -> list[dict]:
    """Queued items Morpheus should be asked about now"""
    now = db.now() if now is None else now
    offers = session_state(central).get("offers") or {}
    out = []
    for item in central.morpheus_items(("queued",)):
        seen = offers.get(item["id"]) or {}
        if int(seen.get("n") or 0) >= MAX_OFFERS:
            continue
        if now - float(seen.get("ts") or 0) < REOFFER_SECONDS:
            continue
        out.append(item)
    return out


def build_prompt(items: list[dict[str, Any]]) -> str:
    blocks = [f"### {i['id']} — PR #{i['pr_number']}: {i['pr_title']}\n{i['pr_url']}\n\n"
              f"{(i['hints'] or '(the PR gave no hints at all: read the PR)')[:4000]}"
              for i in items]
    return (f"{len(items)} shipped PR(s) need a validation script.\n\n"
            + "\n\n".join(blocks))


def context_share(usage: dict[str, Any] | None) -> float | None:
    """How full the session's window was at the end of a turn"""
    if not usage:
        return None
    peak, window = usage.get("context_peak") or 0, usage.get("context_window") or 0
    return (peak / window) if peak and window else None


def wake(central: CentralStore, catalog: Catalog, cwd: Path, *, paused: bool = False,
         now: float | None = None, call: Callable[..., Any] | None = None) -> str:
    """One pass of Morpheus's session: compact if due, else write scripts if any are wanted"""
    from . import agent_usage, claude_cli

    cfg = catalog.os.morpheus
    now = db.now() if now is None else now
    if paused:
        return "paused"
    call = call or claude_cli.run_session_turn
    state = session_state(central)
    items = wanting_scripts(central, now=now)
    if not state.get("compact_due") and not items:
        return "idle"
    session_id = state.get("session_id") or str(uuid.uuid4())
    resume = bool(state.get("turns"))
    if state.get("compact_due") and resume:
        prompt, kind = claude_cli.COMPACT_PROMPT, "compact"
    else:
        prompt, kind = build_prompt(items), "scripts"
    state.update(session_id=session_id, last_turn_ts=now, last_turn_kind=kind)
    try:
        result = call(prompt, session_id, resume=resume, cwd=cwd, model=cfg.model,
                      append_system_prompt=MORPHEUS_PERSONA,
                      permission_mode=catalog.os.default_permission_mode,
                      timeout=cfg.turn_timeout_seconds)
    except claude_cli.ClaudeCliError as e:
        state["last_error"] = str(e)[:500]
        state["errors"] = int(state.get("errors") or 0) + 1
        if not resume:
            # The CLI may have half-created the session: the next first turn mints a fresh id
            state.pop("session_id", None)
        if kind == "scripts":
            _mark_offered(state, items, now)
        _save_session(central, state)
        log.warning("morpheus turn failed: %s", e)
        return "error"
    agent_usage.record(agent_usage.MORPHEUS, usage=result, label=kind,
                       session_id=session_id, store=central)
    state["turns"] = int(state.get("turns") or 0) + 1
    state["last_error"] = ""
    share = context_share(result.usage)
    if kind == "compact":
        state["compactions"] = int(state.get("compactions") or 0) + 1
        state["compact_due"] = False
    else:
        _mark_offered(state, items, now)
        state["last_reply"] = (result.text or "")[:2000]
        state["compact_due"] = share is not None and share >= cfg.compact_at
    if share is not None:
        state["context_share"] = round(share, 4)
        state["context_peak"] = (result.usage or {}).get("context_peak")
        state["context_window"] = (result.usage or {}).get("context_window")
    _save_session(central, state)
    return kind


def _mark_offered(state: dict[str, Any], items: list[dict], now: float) -> None:
    offers = state.setdefault("offers", {})
    for item in items:
        seen = offers.get(item["id"]) or {}
        offers[item["id"]] = {"n": int(seen.get("n") or 0) + 1, "ts": now}


# -- what the surfaces read

def report(central: CentralStore, catalog: Catalog | None = None) -> dict[str, Any]:
    """Counts first, then the session, then every item"""
    from . import fleet

    state = session_state(central)
    state.pop("offers", None)
    paused = fleet.load_pause(central) is not None
    return {"enabled": bool(catalog.os.morpheus.enabled) if catalog else None,
            "counts": {s: central.morpheus_counts().get(s, 0) for s in STATES},
            "session": state, "paused": paused,
            "last_release": central.get_state(RELEASE_KEY) or "",
            "items": central.morpheus_items()}
