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
from typing import TYPE_CHECKING, Any, Callable, Collection, Iterable, Union

from . import db

if TYPE_CHECKING:  # pragma: no cover
    from .catalog import Catalog, MorpheusConfig
    from .central_store import CentralStore
    from .project_store import ProjectStore

log = logging.getLogger("jarvisd.morpheus")

STATES = ("queued", "monitoring", "validated", "failed", "timedout")
OPEN_STATES = ("queued", "monitoring")
TERMINAL_STATES = ("validated", "failed", "timedout")
#: The terminal states that must cause something
ACTION_STATES = ("failed", "timedout")

#: The PR body heading every change in every onboarded project must carry
SECTION = "How to validate in production"

#: The script contract — Morpheus knows the exit code and nothing else
EXIT_VALIDATED = 0
EXIT_FAILED = 1

#: The os_state key holding Morpheus's session
SESSION_KEY = "morpheus.session"
#: The os_state key naming the last release whose PRs were queued
RELEASE_KEY = "morpheus.last_release"
#: Per project: the last `pr_merged` event already queued
MERGE_CURSOR_KEY = "morpheus.merge_cursor.{project}"
# Morpheus's own brake, over the fleet's: "paused" holds it, "running" lets it through a fleet pause
PAUSE_KEY = "morpheus.pause"
PAUSE_MODES = ("paused", "running")

#: How long an item offered to Morpheus waits before it is offered again
REOFFER_SECONDS = 6 * 3600
#: How many times an item is offered before Morpheus gives up on it (the deadline then acts)
MAX_OFFERS = 3
#: Consecutive killed turns tolerated before the session holds back (one release restart kills once)
MAX_KILLS = 3
#: How long that hold lasts when no new release lifts it first
KILL_HOLD_SECONDS = 6 * 3600
#: How much of a script's output is kept
OUTPUT_TAIL = 2000
#: What a `validation` verdict may ask the OS to do — a waive stays the user's (issue #1254)
VERDICT_ACTIONS = ("requeue", "rewrite")
#: How many times Neo's own verdict may put one item back on the watch list
MAX_AUTO_REQUEUES = 1
#: Investigate -> fix -> re-validate cycles one item may run before the user decides (#1344)
MAX_FIX_CYCLES = 3
#: How many times Neo's own verdict may send one item's script back to be rewritten (#1358)
MAX_SCRIPT_REWRITES = 1

SECONDS_PER_DAY = 86400

#: Where an item's script runs: one directory, or a function of the item
CwdFor = Union[Path, Callable[[dict], Path]]


# -- the PR section: hints, never a script

_FENCE = re.compile(r"^\s{0,3}(```|~~~)\s*([\w+-]*)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: `None — <reason>` (or `None.`), and not prose that merely starts with the word
_WAIVED = re.compile(r"^\s*[-*]?\s*none\s*(?:[—–:.-]+\s*(.*))?\Z", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Section:
    """What a PR said about validating it"""

    #: The hints, comments stripped — commands in them are hints too, never run as given
    text: str
    #: The reason, when the PR says `None — <reason>`; "" otherwise
    waived: str = ""

    @property
    def empty(self) -> bool:
        return not any(ln.strip() not in ("", "-", "*") for ln in self.text.splitlines())


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
    """The hints, and a waiver if the PR declared one"""
    text = _HTML_COMMENT.sub("", raw or "").strip()
    w = _WAIVED.match(text)
    waived = ((w.group(1) or "").strip() or "no reason given").splitlines()[0][:500] if w else ""
    return Section(text=text, waived=waived)


def section_problem(body: str) -> str | None:
    """Why this PR body fails the rule, or None"""
    raw = find_section(body)
    if raw is None:
        return (f"no `## {SECTION}` section — give hints: what production activity shows "
                f"this change working (Morpheus writes the check from them), or "
                f"`None — <reason>`")
    if parse_section(raw).empty:
        return f"`## {SECTION}` is still the empty template"
    return None


# -- which PRs reached production

_MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b")
_SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
_WO_PREFIX = re.compile(r"^\[(wo-[0-9a-f]{8})\]")
_PR_URL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")


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


def parse_pr_url(url: str) -> tuple[str, int] | None:
    """(owner/name, number) of a GitHub PR URL"""
    m = _PR_URL.search(url or "")
    return (m.group(1), int(m.group(2))) if m else None


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
            project: str, repo: str, release_tag: str = "", now: float | None = None
            ) -> dict[str, Any] | None:
    """Queue one PR; None when it was already queued (one item per PR, ever)"""
    now = db.now() if now is None else now
    raw = find_section(pr.body)
    section = parse_section(raw or "")
    waived = raw is not None and bool(section.waived)
    return central.add_morpheus_item(
        project=project, repo=repo, pr_number=pr.number, pr_url=pr.url,
        pr_title=pr.title[:300], wo_id=wo_of_title(pr.title), release_tag=release_tag,
        hints=section.text[:8000], script="", script_source="",
        state="validated" if waived else "queued",
        deadline_ts=now + cfg.timeout_days * SECONDS_PER_DAY,
        resolution=f"waived by the PR: {section.waived}" if waived else "", now=now)


def enqueue_release(central: CentralStore, cfg: MorpheusConfig, repo: Path, tag: str, *,
                    project: str, from_ref: str | None = None, to_ref: str | None = None,
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
        item = enqueue(central, cfg, pr, project=project, repo=slug, release_tag=tag)
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


def ensure_release_queued(central: CentralStore, cfg: MorpheusConfig, repo: Path, *,
                          project: str, **kwargs: Any) -> dict[str, Any] | None:
    """The daemon's backstop for the OS itself: queue the running release once, if shipit did not"""
    tag = running_release(repo)
    if not tag or central.get_state(RELEASE_KEY) == tag:
        return None
    return enqueue_release(central, cfg, repo, tag, project=project, **kwargs)


def queue_merges(central: CentralStore, cfg: MorpheusConfig, project: str,
                 store: ProjectStore, *,
                 fetch: Callable[[str, int], PullRequestBody] | None = None) -> list[str]:
    """Every other project ships on merge: queue each PR the OS saw merge since the cursor"""
    key = MERGE_CURSOR_KEY.format(project=project)
    raw = central.get_state(key)
    if raw is None:
        # First sight of this project: start from now, never backfill its history
        central.set_state(key, str(db.now()))
        return []
    fetch = fetch or (lambda s, n: fetch_pr(s, n))
    cursor, queued = float(raw or 0), []
    for merged in store.merged_prs_since(cursor):
        parsed = parse_pr_url(merged["pr_url"])
        if parsed:
            try:
                item = enqueue(central, cfg, fetch(*parsed), project=project,
                               repo=parsed[0])
            except Exception as e:  # noqa: BLE001 — retried next pass: the cursor stays put
                log.warning("morpheus: could not queue %s: %s", merged["pr_url"], e)
                break
            if item:
                queued.append(item["id"])
        cursor = float(merged["ts"])
        central.set_state(key, str(cursor))
    return queued


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


#: How a confirmed item's resolution starts: one fix order per item
CONFIRMED = "confirmed failed by the user"


def confirm(central: CentralStore, item_id: str, fix: str,
            file: Callable[[dict[str, Any]], str]) -> tuple[dict, str]:
    """The user rules the failure real; `file` opens the fix order. (item, order id)"""
    item = _item_or_raise(central, item_id)
    if item["state"] not in ACTION_STATES:
        raise ValueError(f"{item_id} is {item['state']}: only a failed or timed-out check "
                         f"can be confirmed")
    if (item["resolution"] or "").startswith(CONFIRMED):
        raise ValueError(f"{item_id} is already {item['resolution']}")
    if not (fix or "").strip():
        raise ValueError("say what needs fixing")
    order = file(item)
    central.update_morpheus_item(
        item_id, resolution=f"{CONFIRMED}; fix filed as {order}: {fix.strip()}"[:1000])
    return central.morpheus_item(item_id) or {}, order


def fix_description(item: dict[str, Any], fix: str) -> str:
    """A confirmed failure's fix order: the user's words, then what Morpheus saw"""
    return (f"{fix.strip()}\n\nPR {item['pr_url']} {item['state']} its production "
            f"validation ({item['resolution']}); the user confirmed the failure is real.\n"
            f"The check: `jarvis morpheus show {item['id']}`. The OS watches it again "
            f"once this order's fix reaches production.\n\n"
            f"Last check output:\n{(item.get('last_output') or '—')[-1500:]}")


_FIX_ORDER = re.compile(r"fix filed as (wo-[0-9a-f]{8})")


def requeue_fixed(central: CentralStore, cfg: MorpheusConfig,
                  carrier: Callable[[str], str],
                  investigation_fix: Callable[[str], str] | None = None) -> list[str]:
    """Re-watch each item once its fix reaches production (#1307, #1344)"""
    # An item exists for an order only once its PR reached production: released (the OS) or merged
    shipped: dict[str, dict[str, Any]] = {}
    for i in central.morpheus_items():
        if i["wo_id"]:
            shipped.setdefault(i["wo_id"], i)
    requeued = []
    for item in central.morpheus_items(ACTION_STATES):
        m = _FIX_ORDER.search(item["resolution"] or "")
        kind, _, inv_id = (item.get("action") or "").partition(":")
        via = ""
        if (item["resolution"] or "").startswith(CONFIRMED) and m:
            fix = m.group(1)
        elif kind == "investigation" and inv_id and investigation_fix:
            fix, via = investigation_fix(inv_id), f" (filed by {inv_id})"
        else:
            continue
        if not fix:
            continue
        fix = carrier(fix)
        landed = shipped.get(fix)
        if not landed or landed["id"] == item["id"]:
            continue
        requeue(central, cfg, item["id"],
                resolution=f"re-watched: its fix {fix}{via} reached production in PR "
                           f"#{landed['pr_number']}")
        requeued.append(item["id"])
    return requeued


def requeue(central: CentralStore, cfg: MorpheusConfig, item_id: str, *,
            resolution: str = "") -> dict:
    """Watch it again with a fresh deadline (and a fresh action)"""
    item = _item_or_raise(central, item_id)
    now = db.now()
    central.update_morpheus_item(
        item_id, state="monitoring" if item["script"] else "queued",
        deadline_ts=now + cfg.timeout_days * SECONDS_PER_DAY,
        next_check_ts=now if item["script"] else None, resolution=resolution, action="",
        action_ts=None)
    _forget_offer(central, item_id)
    return central.morpheus_item(item_id) or {}


def rewrite(central: CentralStore, cfg: MorpheusConfig, item_id: str, why: str, *,
            resolution: str = "") -> dict:
    """The script is wrong, not the PR: back to Morpheus for a new one, `why` as its brief (#1358)"""
    item = _item_or_raise(central, item_id)
    why = (why or "").strip()
    if not why:
        raise ValueError("say what is wrong with the script")
    now = db.now()
    # last_exit goes too: the old script's exit 1 must not count toward the new one's two-in-a-row
    central.update_morpheus_item(
        item_id, state="queued", script="", script_source="", last_exit=None,
        rewrite_brief=(f"{why[:1500]}\n\nThe rejected script:\n"
                       f"{item['script'][:3000] or '(none)'}"),
        deadline_ts=now + cfg.timeout_days * SECONDS_PER_DAY, next_check_ts=None,
        resolution=resolution or f"script sent back to be rewritten: {why[:600]}",
        action="", action_ts=None)
    _forget_offer(central, item_id)
    return central.morpheus_item(item_id) or {}


def rewrite_from_investigation(central: CentralStore, cfg: MorpheusConfig, item_id: str,
                               inv_id: str, brief: str) -> tuple[bool, str]:
    """SCRIPT_WRONG verdict: rewrite under the cap; (False, …) = cap spent, user owes it (#1374)"""
    item = _item_or_raise(central, item_id)
    if item["state"] not in ACTION_STATES:
        return True, f"nothing sent back: {item_id} is {item['state']}, decided elsewhere"
    if not central.claim_morpheus_script_rewrite(item_id, MAX_SCRIPT_REWRITES):
        command = f'jarvis morpheus rewrite {item_id} --why "…"'
        central.add_inbox(
            project=item.get("project") or "os", level="warning",
            title=f"PR #{item['pr_number']} {item['state']} production validation — its "
                  f"script is wrong and its one automatic rewrite is spent",
            body=f"{item['pr_url']}\n{inv_id}: {brief[:600]}\n{command}")
        return False, f"its one automatic rewrite is spent — `{command}`"
    rewrite(central, cfg, item_id, brief,
            resolution=f"script sent back to be rewritten by {inv_id}: {brief[:600]}")
    central.add_inbox(
        project=item.get("project") or "os", level="info",
        title=f"PR #{item['pr_number']} — {inv_id} found its validation script wrong; "
              f"Morpheus is rewriting it",
        body=f"{item['pr_url']}\n{brief[:600]}\njarvis morpheus show {item_id}")
    return True, f"{item_id} sent back to Morpheus for a new script; its one rewrite is spent"


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


def checked_version(repo: Path) -> str:
    """What production ran at a check: its tag, else tag-distance or commit; '' outside git"""
    try:
        return _git(repo, "describe", "--tags", "--always", "HEAD").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _cwd(cwd: CwdFor, item: dict[str, Any]) -> Path:
    return cwd(item) if callable(cwd) else cwd


def check(central: CentralStore, cfg: MorpheusConfig, item: dict[str, Any], cwd: CwdFor,
          *, now: float | None = None,
          run: Callable[[str, Path, int], CheckResult] = run_script,
          version: Callable[[Path], str] | None = None) -> str:
    """Run an item's script once and move it by the exit codes alone; returns its state"""
    now = db.now() if now is None else now
    where = _cwd(cwd, item)
    result = run(item["script"], where, cfg.script_timeout_seconds)
    fields: dict[str, Any] = {"last_check_ts": now, "checks": int(item["checks"]) + 1,
                              "last_check_version": (version or checked_version)(where),
                              "last_exit": result.exit, "last_output": result.output,
                              "next_check_ts": now + cfg.check_every_minutes * 60}
    if result.exit == EXIT_VALIDATED:
        fields.update(state="validated", next_check_ts=None, resolution="script exited 0")
    # One exit 1 may be the environment, not the PR, so `failed` needs a second in a row (#1255)
    elif result.exit == EXIT_FAILED and item.get("last_exit") == EXIT_FAILED:
        fields.update(state="failed", next_check_ts=None,
                      resolution="script exited 1 twice in a row")
    elif now >= float(item["deadline_ts"]):
        fields.update(state="timedout", next_check_ts=None,
                      resolution=_timeout_reason(item, cfg, result.exit))
    state = str(fields.get("state", item["state"]))
    with db.write_transaction(central.conn):  # the run and the item move together: db.write_transaction
        central.update_morpheus_item(item["id"], **fields)
        central.add_morpheus_run(item["id"], exit=result.exit, output=result.output,
                                 state_after=state, now=now)
    return state


def _timeout_reason(item: dict[str, Any], cfg: MorpheusConfig,
                    last_exit: int | None = None) -> str:
    if not item.get("script"):
        return f"no script was registered within {cfg.timeout_days:g} day(s)"
    if last_exit == EXIT_FAILED:
        return (f"one exit 1 within {cfg.timeout_days:g} day(s), "
                "never confirmed by a second")
    return (f"no exit 0 or 1 within {cfg.timeout_days:g} day(s) "
            f"(last exit {item.get('last_exit')})")


def sweep(central: CentralStore, cfg: MorpheusConfig, cwd: CwdFor, *,
          now: float | None = None,
          run: Callable[[str, Path, int], CheckResult] = run_script,
          skip: Collection[str] = ()) -> dict[str, int]:
    """Run every due script and expire every overdue item — no model, ever"""
    now = db.now() if now is None else now
    moved = {s: 0 for s in STATES}
    for item in central.morpheus_items(OPEN_STATES):
        if (item["project"] or "") in skip:
            # Offboarded: left as it is, never failed (docs/superpowers/specs/2026-10-28-offboard-a-project.md §6.2)
            continue
        if item["state"] == "monitoring" and item["script"]:
            due = item["next_check_ts"]
            if due is None or float(due) <= now:
                moved[check(central, cfg, item, cwd, now=now, run=run)] += 1
        elif now >= float(item["deadline_ts"]):
            central.update_morpheus_item(item["id"], state="timedout", next_check_ts=None,
                                         resolution=_timeout_reason(item, cfg))
            moved["timedout"] += 1
    return moved


# -- failed and timed out hand off: Morpheus never diagnoses

def owed_actions(central: CentralStore) -> list[dict[str, Any]]:
    """Terminal items that still owe their hand-off"""
    return [i for i in central.morpheus_items(ACTION_STATES) if not i["action"]]


def act(central: CentralStore, catalog: Catalog, item: dict[str, Any], *,
        investigate: Callable[..., tuple[str, str]] | None = None,
        ask_neo: Callable[..., int] | None = None) -> str:
    """Hand the item off exactly once; returns what was recorded"""
    cfg = catalog.os.morpheus
    route = cfg.on_failed if item["state"] == "failed" else cfg.on_timeout
    investigate = investigate or _investigate
    ask_neo = ask_neo or _ask_neo
    note = ""
    if route == "investigate":
        inv_id, note = investigate(catalog, item)
        if inv_id:
            action = f"{_action_kind(inv_id)}:{inv_id}"
            if central.claim_morpheus_action(item["id"], action):
                central.add_inbox(
                    project=item.get("project") or _os_project(catalog), level="warning",
                    title=f"PR #{item['pr_number']} {item['state']} production validation "
                          f"({item['resolution']})",
                    body=f"{item['pr_url']}\n{inv_id} is finding out why.")
            return action
    qid = ask_neo(catalog, item, note)
    action = f"neo:{qid}"
    central.claim_morpheus_action(item["id"], action)
    return action


def _action_kind(order_id: str) -> str:
    """An `io-` id is the improvement order a PR with no work order gets (#1253)"""
    return "improvement" if order_id.startswith("io-") else "investigation"


def _os_project(catalog: Catalog) -> str:
    from . import schedule

    return schedule.os_project((p.name, p.path) for p in catalog.projects) or "os"


def _investigate(catalog: Catalog, item: dict[str, Any]) -> tuple[str, str]:
    """(order id, '') or ('', why not) — through the steward, like every OS-filed order"""
    from . import ops, steward

    wo_id = item.get("wo_id") or ""
    if not wo_id:
        return _improve(catalog, item)
    try:
        project, _path, _wo = ops.find_work_order(wo_id, item.get("project") or None)
    except Exception as e:  # noqa: BLE001
        return "", f"its work order {wo_id} could not be found ({e})"
    cycles = ops.morpheus_investigations(project, item["id"])
    if len(cycles) >= MAX_FIX_CYCLES:
        # The loop guard: fixes keep landing and the validation keeps failing
        from .central_store import CentralStore

        note = (f"{len(cycles)} investigate-fix-revalidate cycles ({', '.join(cycles)}) "
                f"did not make it pass — the user decides: `jarvis morpheus waive "
                f"{item['id']} --reason \"…\"`, `jarvis morpheus confirm {item['id']} "
                f"--fix \"…\"`, or `jarvis morpheus requeue {item['id']}`")
        central = CentralStore()
        try:
            central.add_inbox(project=project, level="critical",
                              title=f"PR #{item['pr_number']} still fails production "
                                    f"validation after {len(cycles)} fixes",
                              body=f"{item['pr_url']}\n{note}")
        finally:
            central.close()
        return "", note
    decision = steward.gate(
        steward.Candidate("morpheus", "critical", project,
                          f"PR #{item['pr_number']} {item['state']} production validation",
                          evidence=(item.get("last_output") or item["resolution"],),
                          parent_wo_id=wo_id, payload={"morpheus_item": item["id"]}),
        catalog=catalog)
    if not decision.allowed:
        return "", f"the steward did not dispatch it ({decision.outcome}: {decision.reason})"
    live = ops.live_investigation(project, wo_id)
    if live:
        return live, ""
    inv =ops.create_investigation_order(project, wo_id, why=_why(item), origin="jarvis",
                                         stamp=decision.stamp, morpheus_item=item["id"])
    return str(inv["id"]), ""


def _improve(catalog: Catalog, item: dict[str, Any]) -> tuple[str, str]:
    """An investigation needs an order to investigate; a hand-opened PR is its own evidence (#1253)"""
    from . import ops, steward

    project = item.get("project") or _os_project(catalog)
    title = f"PR #{item['pr_number']} {item['state']} production validation"
    decision = steward.gate(
        steward.Candidate("morpheus", "critical", project, title,
                          evidence=(item.get("last_output") or item["resolution"],),
                          payload={"morpheus_item": item["id"]}),
        catalog=catalog)
    if not decision.allowed:
        return "", f"the steward did not dispatch it ({decision.outcome}: {decision.reason})"
    try:
        io = ops.create_improvement_order(project, title, description=_why(item),
                                          refs=(item["pr_url"],), origin="morpheus")
    except Exception as e:  # noqa: BLE001 — Neo still gets the item
        return "", f"the improvement order could not be filed ({e})"
    return str(io["id"]), ""


def _why(item: dict[str, Any]) -> str:
    """The facts Morpheus has, and nothing it does not: it ran a script and read an exit code"""
    return (f"Morpheus: PR #{item['pr_number']} ({item['pr_url']}) of project "
            f"{item.get('project') or '?'} {item['state']} its production validation: "
            f"{item['resolution']}. Morpheus only runs the validation script and reads its "
            f"exit code — it has NOT diagnosed anything, and the script itself may be wrong.\n\n"
            f"The PR's hints, under `## {SECTION}`:\n{item['hints'][:3000]}\n\n"
            f"The script (written by {item['script_source'] or 'nobody yet'}):\n"
            f"{item['script'][:3000] or '(none)'}\n\n"
            f"Last exit {item['last_exit']}, output tail:\n{item['last_output'][-1500:]}")


VALIDATION_QUESTION = """MORPHEUS — a shipped change has no passing production validation.

{why}

Decide whether the user needs to look. If they do, ESCALATE and say what to look at. If
not, answer in two sentences; the answer is recorded on the item.
{actions}"""

#: The item still has its automatic requeue
REQUEUE_OFFER = """If the failure was transient — production had not exercised the change yet, a network or
environment hiccup — add `"action": "requeue"` to your JSON and the OS watches it again
with a fresh deadline. That is available ONCE per item, and this item still has it.
Ruling the change fine is the USER'S: recommend `jarvis morpheus waive {id} --reason "…"`,
never a waive of your own; ruling it broken is theirs too: `jarvis morpheus confirm {id} --fix "…"`."""

#: It has spent it, so saying `requeue` would only look like an action
REQUEUE_SPENT = """This item has already spent its one automatic requeue, so `"action": "requeue"` is
refused and it stays {state}. The user re-watches it with `jarvis morpheus requeue {id}`
rules it fine with `jarvis morpheus waive {id} --reason "…"`, or rules it broken and files
the fix with `jarvis morpheus confirm {id} --fix "…"`."""

#: The item still has its automatic script rewrite (#1358)
REWRITE_OFFER = """If the SCRIPT is wrong rather than the PR — it keys on the wrong field, counts the wrong
thing, or cannot see what the hints describe — add `"action": "rewrite"` instead: Morpheus
writes a new script with your answer as its brief, and the OS watches it with a fresh
deadline. Your answer must then say what the script got wrong and what the right check is.
That is available ONCE per item, and this item still has it."""

#: It has spent it
REWRITE_SPENT = """This item has already spent its one automatic script rewrite, so `"action": "rewrite"` is
refused. If the new script is wrong too, ESCALATE: the user sends it back with
`jarvis morpheus rewrite {id} --why "…"`."""


def _ask_neo(catalog: Catalog, item: dict[str, Any], note: str = "") -> int:
    from .neo_store import NeoStore

    why = _why(item) + (f"\n\nNo investigation was filed: {note}." if note else "")
    spent = int(item.get("auto_requeues") or 0) >= MAX_AUTO_REQUEUES
    offer = REQUEUE_SPENT if spent else REQUEUE_OFFER
    rewritten = int(item.get("script_rewrites") or 0) >= MAX_SCRIPT_REWRITES
    actions = "\n\n".join(text.format(id=item["id"], state=item["state"]) for text in
                          (offer, REWRITE_SPENT if rewritten else REWRITE_OFFER))
    neo = NeoStore()
    try:
        q = neo.ask(item.get("project") or _os_project(catalog), "",
                    VALIDATION_QUESTION.format(why=why, actions=actions),
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


def verdict_action(verdict: dict[str, Any]) -> str:
    """What a `validation` verdict asks the OS to do, or '' for anything else it said"""
    action = str(verdict.get("action") or "").strip().lower()
    return action if action in VERDICT_ACTIONS else ""


def stale_question(item: dict[str, Any] | None,
                   q: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, why) if the item this `validation` question asks about was decided elsewhere"""
    item_id = item_of_question(q)
    if not item_id:
        return None
    if item is None:
        return ("SUPERSEDED — the Morpheus item is gone", f"{item_id} no longer exists")
    # A requeue clears `action`, and a later failure asks a NEW question: either way not this one
    if item["state"] not in ACTION_STATES or item.get("action") != f"neo:{q['id']}":
        return (f"SUPERSEDED — {item_id} is {item['state']}",
                f"{item_id} was already decided: it is {item['state']}")
    return None


def record_neo_verdict(central: CentralStore, cfg: MorpheusConfig, q: dict[str, Any],
                       verdict: dict[str, Any], question_url: str = "") -> None:
    """Neo ruled on a `validation` question: record it, act on it, and tell the user"""
    item_id = item_of_question(q)
    item = central.morpheus_item(item_id) if item_id else None
    if not item:
        log.warning("validation verdict for question %s names no item", q.get("id"))
        return
    reading = (verdict.get("answer") or verdict.get("reason") or "").strip()
    resolution = f"{item['resolution']} — Neo: {reading[:600]}".strip(" —")
    escalated = bool(verdict.get("escalate"))
    action = "" if escalated else verdict_action(verdict)
    # The state guard catches the user waiving between the question and the verdict
    live = item["state"] in ACTION_STATES
    requeued = (action == "requeue" and live
                and central.claim_morpheus_auto_requeue(item_id, MAX_AUTO_REQUEUES))
    rewritten = (action == "rewrite" and live and bool(reading)
                 and central.claim_morpheus_script_rewrite(item_id, MAX_SCRIPT_REWRITES))
    if requeued:
        requeue(central, cfg, item_id, resolution=f"re-watched by Neo: {resolution}")
    elif rewritten:
        rewrite(central, cfg, item_id, reading,
                resolution=f"script sent back to be rewritten by Neo: {resolution}")
    else:
        central.update_morpheus_item(item_id, resolution=resolution)
    central.add_inbox(
        project=q.get("project") or "os", level="warning" if escalated else "info",
        title=(f"PR #{item['pr_number']} {item['state']} production validation"
               + (" — Neo escalated" if escalated else
                  " — Neo is watching it again" if requeued else
                  " — Neo sent its script back to be rewritten" if rewritten else
                  " — Neo's reading")),
        body=f"{item['pr_url']}\n{reading[:600]}\n"
             + ("the OS is validating it again — its one automatic requeue is now spent\n"
                if requeued else "")
             + ("Morpheus writes a new script, then the OS watches it again — its one "
                "automatic rewrite is now spent\n" if rewritten else "")
             + (f"{question_url}\n" if question_url else "")
             + f"jarvis morpheus show {item_id}")


# -- the only LLM: Morpheus's session turns hints into scripts

MORPHEUS_PERSONA = """You are MORPHEUS, the production validator of the Jarvis agentic OS.

The OS ships pull requests to production, for every project it runs. Each PR says, under
`## How to validate in production`, HINTS about what production activity shows it working.
Your only job is to turn those hints into a SCRIPT the OS then runs mechanically on a
schedule. You never run the validation, never judge a result and never diagnose a failure:
the OS reads the exit code, and finding out why something failed is someone else's job.

For each item you are given, spawn ONE subagent (Task tool), in parallel. Each subagent:
  1. reads the item's hints (and the PR or the project's code if it must:
     `gh pr view <url>`, the project path given with the item);
  2. writes a bash script to /tmp/morpheus-<item id>.sh that:
     - runs from the project's production directory, the one given with the item;
     - is READ-ONLY: it inspects production (CLIs with --json, logs, `systemctl --user
       status`, `gh` reads, HTTP GETs) and never changes anything;
     - exits 0 when production shows the change working;
     - exits 1 ONLY when production shows THIS PR's own failure signature — the exact
       symptom the PR fixed, happening again. Anything the PR does not own is NOT a
       failure: a command that errored, an endpoint that was unreachable, a missing
       file, a reading stale for its own reason. Those exit 75, and so does every
       check that could not read what it needed to judge;
     - exits 75 for everything else, including production not having exercised the
       change yet;
     - finishes in well under a minute;
     - prints ONE line per hint it checks, starting `PASS`, `FAIL` or `UNKNOWN` (the
       last for a check that exits 75), naming the hint in plain words a human reads
       without the script — never a bare internal field name like `wedged=false`;
     - never prints the release that first carried the PR on its own: the OS already
       records that, and the check's own version, beside the output;
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


def wanting_scripts(central: CentralStore, *, now: float | None = None,
                    skip: Collection[str] = ()) -> list[dict]:
    """Queued items Morpheus should be asked about now"""
    now = db.now() if now is None else now
    offers = session_state(central).get("offers") or {}
    out = []
    for item in central.morpheus_items(("queued",)):
        if (item["project"] or "") in skip:
            continue
        seen = offers.get(item["id"]) or {}
        if int(seen.get("n") or 0) >= MAX_OFFERS:
            continue
        if now - float(seen.get("ts") or 0) < REOFFER_SECONDS:
            continue
        out.append(item)
    return out


def build_prompt(items: list[dict[str, Any]], dirs: dict[str, Path]) -> str:
    blocks = [f"### {i['id']} — {i.get('project') or '?'} PR #{i['pr_number']}: "
              f"{i['pr_title']}\n{i['pr_url']}\nproduction directory: "
              f"{dirs.get(i.get('project') or '', '(unknown)')}\n\n"
              f"{(i['hints'] or '(the PR gave no hints at all: read the PR)')[:4000]}"
              + (f"\n\nA REWRITE: its previous script was ruled wrong, not the PR. Write one "
                 f"that avoids this:\n{i['rewrite_brief']}" if i.get("rewrite_brief") else "")
              for i in items]
    return (f"{len(items)} shipped PR(s) need a validation script.\n\n"
            + "\n\n".join(blocks))


def context_share(usage: dict[str, Any] | None) -> float | None:
    """How full the session's window was at the end of a turn"""
    if not usage:
        return None
    peak, window = usage.get("context_peak") or 0, usage.get("context_window") or 0
    return (peak / window) if peak and window else None


def kill_hold(central: CentralStore, state: dict[str, Any], now: float) -> float:
    """The live hold after MAX_KILLS kills in a row — 0 once it expires or a new release lands"""
    until = float(state.get("kill_hold_until") or 0)
    if not until or now >= until:
        return 0.0
    # A release restart is the usual cause of the kills AND the usual fix, so it lifts the hold (#1161)
    if (central.get_state(RELEASE_KEY) or "") != (state.get("kill_hold_release") or ""):
        return 0.0
    return until


def wake(central: CentralStore, catalog: Catalog, cwd: Path, *, paused: bool = False,
         dirs: dict[str, Path] | None = None, now: float | None = None,
         call: Callable[..., Any] | None = None, skip: Collection[str] = ()) -> str:
    """One pass of Morpheus's session: compact if due, else write scripts if any are wanted"""
    from . import agent_usage, claude_cli

    cfg = catalog.os.morpheus
    now = db.now() if now is None else now
    if paused:
        return "paused"
    call = call or claude_cli.run_session_turn
    dirs = dirs or {}
    state = session_state(central)
    hold = float(state.get("usage_hold_until") or 0)
    if now < hold:
        # The account's window is spent: any turn would be refused, so none is attempted (#1171)
        return "usage_held"
    if hold:
        state.pop("usage_hold_until", None)
    if kill_hold(central, state, now):
        # Every turn is dying before it answers: retrying each wake only burns resume context
        return "kill_held"
    if state.get("kill_hold_until"):
        if (central.get_state(RELEASE_KEY) or "") != (state.get("kill_hold_release") or ""):
            # A release lifted it: the count starts over, or one restart re-arms the hold (#52)
            state["kills"] = 0
        state.pop("kill_hold_until", None)
        state.pop("kill_hold_release", None)
        _save_session(central, state)
    items = wanting_scripts(central, now=now, skip=skip)
    if not state.get("compact_due") and not items:
        return "idle"
    session_id = state.get("session_id") or str(uuid.uuid4())
    resume = bool(state.get("turns"))
    if state.get("compact_due") and resume:
        prompt, kind, batch = claude_cli.COMPACT_PROMPT, "compact", []
    else:
        prompt, kind, batch = build_prompt(items, dirs), "scripts", items
    add_dirs = sorted({dirs[i["project"]] for i in batch if i.get("project") in dirs})
    state.update(session_id=session_id, last_turn_ts=now, last_turn_kind=kind,
                 turn_started_ts=now)
    # Persisted before the call: this is what another process reads as "a turn is in flight"
    _save_session(central, state)
    try:
        result = call(prompt, session_id, resume=resume, cwd=cwd, model=cfg.model,
                      append_system_prompt=MORPHEUS_PERSONA,
                      permission_mode=catalog.os.default_permission_mode,
                      add_dirs=add_dirs, timeout=cfg.turn_timeout_seconds,
                      previous=state.get("last_usage") if resume else None)
    except claude_cli.UsageLimitError as e:
        # BEFORE ClaudeCliError, its base: the window refused the call, so it costs no offer (#1171)
        from .worker_session import RATE_LIMIT_FALLBACK_DELAY

        state.pop("turn_started_ts", None)
        state["last_error"] = str(e)[:500]
        state["usage_limits"] = int(state.get("usage_limits") or 0) + 1
        state["usage_hold_until"] = e.limit.reset_at or now + RATE_LIMIT_FALLBACK_DELAY
        if not resume:
            state.pop("session_id", None)
        _save_session(central, state)
        log.warning("morpheus turn refused by the usage window, held until %s: %s",
                    state["usage_hold_until"], e)
        return "usage_limit"
    except claude_cli.KilledError as e:
        # BEFORE ClaudeCliError, its base: a kill is not an answer, so it costs no offer (#1161)
        state.pop("turn_started_ts", None)
        state["last_error"] = str(e)[:500]
        state["kills"] = kills = int(state.get("kills") or 0) + 1
        if not resume:
            state.pop("session_id", None)
        if kills >= MAX_KILLS:
            state["kill_hold_until"] = now + KILL_HOLD_SECONDS
            state["kill_hold_release"] = central.get_state(RELEASE_KEY) or ""
            state["last_error"] = f"held after {kills} killed turns in a row: {str(e)[:400]}"
            central.add_inbox(
                project=_os_project(catalog), level="warning",
                title=f"Morpheus held after {kills} killed turns in a row",
                body="No session turn answered, so no PR gets a validation script. It retries "
                     f"when the next release deploys, or in {KILL_HOLD_SECONDS // 3600}h.\n"
                     "jarvis morpheus status")
            log.warning("morpheus held after %s killed turns in a row: %s", kills, e)
        else:
            log.warning("morpheus turn killed before it answered: %s", e)
        _save_session(central, state)
        return "killed"
    except claude_cli.ClaudeCliError as e:
        state.pop("turn_started_ts", None)
        state["last_error"] = str(e)[:500]
        state["errors"] = int(state.get("errors") or 0) + 1
        if not resume:
            # The CLI may have half-created the session: the next first turn mints a fresh id
            state.pop("session_id", None)
        _mark_offered(state, batch, now, error=True)
        _save_session(central, state)
        log.warning("morpheus turn failed: %s", e)
        return "error"
    state.pop("turn_started_ts", None)
    agent_usage.record(agent_usage.MORPHEUS, usage=result, label=kind,
                       session_id=session_id, store=central)
    # The cumulative reading the next resumed turn subtracts (#1462)
    if isinstance(result.usage, dict) and isinstance(result.usage.get("reported"), dict):
        state["last_usage"] = {"usage_v": result.usage.get("usage_v"),
                               "reported": result.usage["reported"]}
    state["turns"] = int(state.get("turns") or 0) + 1
    state["last_error"] = ""
    state["kills"] = 0
    state.pop("kill_hold_until", None)
    state.pop("kill_hold_release", None)
    share = context_share(result.usage)
    if kind == "compact":
        state["compactions"] = int(state.get("compactions") or 0) + 1
        state["compact_due"] = False
    else:
        _mark_offered(state, batch, now)
        state["last_reply"] = (result.text or "")[:2000]
        state["compact_due"] = share is not None and share >= cfg.compact_at
    if share is not None:
        state["context_share"] = round(share, 4)
        state["context_peak"] = (result.usage or {}).get("context_peak")
        state["context_window"] = (result.usage or {}).get("context_window")
    _save_session(central, state)
    return kind


def set_pause(central: CentralStore, mode: str | None, reason: str = "",
              now: float | None = None) -> dict[str, Any]:
    """Pin Morpheus paused or running whatever the fleet does; None follows the fleet again"""
    if mode is None:
        central.set_state(PAUSE_KEY, "")
    elif mode in PAUSE_MODES:
        central.set_state(PAUSE_KEY, json.dumps({
            "mode": mode, "reason": reason, "since": db.now() if now is None else now}))
    else:
        raise ValueError(f"pause mode must be one of {PAUSE_MODES}, not {mode!r}")
    return pause_state(central)


def pause_state(central: CentralStore) -> dict[str, Any]:
    """Morpheus's override, the fleet's pause, and whether session turns are held"""
    from . import fleet

    try:
        pin = json.loads(central.get_state(PAUSE_KEY) or "{}")
    except ValueError:
        # A brake that cannot be read is still a brake
        pin = {"mode": "paused", "reason": "(unreadable morpheus pause record)"}
    mode = pin.get("mode") if pin.get("mode") in PAUSE_MODES else ""
    fleet_paused = fleet.load_pause(central) is not None
    paused = mode == "paused" or (mode != "running" and fleet_paused)
    return {"paused": paused, "override": mode, "reason": str(pin.get("reason") or ""),
            "since": pin.get("since"), "fleet_paused": fleet_paused}


def _mark_offered(state: dict[str, Any], items: list[dict], now: float,
                  error: bool = False) -> None:
    offers = state.setdefault("offers", {})
    for item in items:
        seen = offers.get(item["id"]) or {}
        offers[item["id"]] = {"n": int(seen.get("n") or 0) + 1, "ts": now,
                              "cause": "error" if error else "offer"}


def _offer_cause(seen: dict[str, Any]) -> str | None:
    """None for an entry written before the cause was recorded — unknown, not "an offer" (#1171)"""
    if seen.get("cause"):
        return str(seen["cause"])
    if "error" in seen:
        return "error" if seen["error"] else "offer"
    return None


def activity(central: CentralStore, *, timeout: float | None = None,
             now: float | None = None) -> dict[str, Any]:
    """What the session is DOING — never its pause mode: in flight, idle, or holding items back"""
    from .catalog import MorpheusConfig

    now = db.now() if now is None else now
    state = session_state(central)
    offers = state.get("offers") or {}
    limit = float(timeout or MorpheusConfig().turn_timeout_seconds)
    started = float(state.get("turn_started_ts") or 0)
    # A marker older than the turn's own timeout is a crashed daemon, not work in flight
    in_flight = bool(started) and now - started <= limit
    held = []
    for item in central.morpheus_items(("queued",)):
        seen = offers.get(item["id"]) or {}
        n, ts = int(seen.get("n") or 0), float(seen.get("ts") or 0)
        if n >= MAX_OFFERS:
            held.append({"id": item["id"], "offers": n, "next_offer_ts": None,
                         "exhausted": True, "cause": _offer_cause(seen)})
        elif now - ts < REOFFER_SECONDS:
            held.append({"id": item["id"], "offers": n, "next_offer_ts": ts + REOFFER_SECONDS,
                         "exhausted": False, "cause": _offer_cause(seen)})
    usage_hold = float(state.get("usage_hold_until") or 0)
    return {"usage_hold_until": usage_hold if now < usage_hold else None,
            "kill_hold_until": kill_hold(central, state, now) or None,
            "kills": int(state.get("kills") or 0), "max_kills": MAX_KILLS,
            "turn_in_flight": in_flight, "turn_kind": state.get("last_turn_kind") or "",
            "turn_started_ts": started if in_flight else None,
            "abandoned_turn_ts": started if started and not in_flight else None,
            "due": len(wanting_scripts(central, now=now)),
            "held": held, "max_offers": MAX_OFFERS}


# -- what the surfaces read

def report(central: CentralStore, catalog: Catalog | None = None) -> dict[str, Any]:
    """Counts first, then the session, then every item"""
    state = session_state(central)
    state.pop("offers", None)
    pause = pause_state(central)
    timeout = catalog.os.morpheus.turn_timeout_seconds if catalog else None
    return {"enabled": bool(catalog.os.morpheus.enabled) if catalog else None,
            "counts": {s: central.morpheus_counts().get(s, 0) for s in STATES},
            "session": state, "paused": pause["paused"], "pause": pause,
            "activity": activity(central, timeout=timeout),
            "last_release": central.get_state(RELEASE_KEY) or "",
            "items": central.morpheus_items()}
