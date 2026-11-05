"""Morpheus: every shipped PR validated in production (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Iterable, Union

from . import db

if TYPE_CHECKING:  # pragma: no cover
    from .catalog import Catalog, MorpheusConfig
    from .central_store import CentralStore
    from .project_store import ProjectStore

log = logging.getLogger("jarvisd.morpheus")

#: `unverified`: settled with no check passed, never counted as passed
STATES = ("queued", "monitoring", "validated", "unverified", "failed", "timedout", "cancelled")
OPEN_STATES = ("queued", "monitoring")
TERMINAL_STATES = ("validated", "unverified", "failed", "timedout", "cancelled")
#: The terminal states that must cause something
ACTION_STATES = ("failed", "timedout")

#: The PR body heading every change in every onboarded project must carry
SECTION = "How to validate in production"

#: The script contract — Morpheus knows the exit code and nothing else
EXIT_VALIDATED = 0
EXIT_FAILED = 1
EXIT_NOT_YET = 75

#: What an exit 75 names on a `REASON <word>` line (docs/superpowers/specs/2026-11-01-monitoring-orders.md §9)
NOT_DEPLOYED = "not_deployed"
NOT_EXERCISED = "not_exercised"
AMBIGUOUS = "ambiguous"
REASONS = (NOT_DEPLOYED, NOT_EXERCISED, "unreadable", AMBIGUOUS)
#: How long `not_deployed` may hold a deadline, from the first check that said it
NOT_DEPLOYED_CAP_DAYS = 14
#: How an arming session says its script gets its evidence
TRIGGERS = ("probe", "ambient")
_REASON_LINE = re.compile(r"^REASON[ \t]+(\S+)[ \t]*$", re.MULTILINE)
_FAIL_LINE = re.compile(r"^FAIL\b", re.MULTILINE)
#: What a new watch forgets of the last one (§9)
_FRESH_WATCH = {"last_reason": "", "saw_fail": 0, "held_since_ts": None}

#: The os_state key naming the last release whose PRs were queued
RELEASE_KEY = "morpheus.last_release"
#: Per project: the last `pr_merged` event already queued
MERGE_CURSOR_KEY = "morpheus.merge_cursor.{project}"
# Morpheus's own brake, over the fleet's: "paused" holds it, "running" lets it through a fleet pause
PAUSE_KEY = "morpheus.pause"
PAUSE_MODES = ("paused", "running")

#: How long an order whose session failed waits before the next one
REOFFER_SECONDS = 6 * 3600
#: Failed sessions one owed step gets before Morpheus gives up on it (the deadline then acts)
MAX_OFFERS = 3
#: Consecutive killed sessions that cost one attempt (one release restart kills once)
MAX_KILLS = 3
#: How much of a script's output is kept
OUTPUT_TAIL = 2000
#: What a `validation` verdict may ask the OS to do — a waive stays the user's (issue #1254)
VERDICT_ACTIONS = ("requeue", "rewrite", "settle")
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
        resolution=f"waived by the PR: {section.waived}" if waived else "",
        origin="release" if release_tag else "merge", now=now)


def is_monitoring_order_id(x: object) -> bool:
    """`mv-` (older rows) or `mo-` (docs/superpowers/specs/2026-11-01-monitoring-orders.md §1.3)"""
    return isinstance(x, str) and x.startswith(("mv-", "mo-"))


def create(central: CentralStore, *, project: str, title: str, prompt: str = "",
           script: str = "", every_minutes: int | None = None,
           deadline_days: float | None = None, origin: str = "user") -> dict:
    """An order watching no PR, queued: an arm session writes (prompt) or validates (script) it"""
    from .catalog import MorpheusConfig

    prompt, script = (prompt or "").strip(), (script or "").strip()
    if bool(prompt) == bool(script):
        raise ValueError("give exactly one of a prompt (what to watch) or a script")
    if not (title or "").strip():
        raise ValueError("a monitoring order needs a title")
    if every_minutes is not None and int(every_minutes) < 1:
        raise ValueError("every_minutes must be at least 1")
    # Callers holding the catalog pass its timeout (ops.mo_create); this is the bare default
    days = MorpheusConfig().timeout_days if deadline_days is None else deadline_days
    now = db.now()
    item = central.add_morpheus_item(
        project=project, repo="", pr_number=None, pr_url="", pr_title="", wo_id="",
        release_tag="", hints="", script=script, script_source="user" if script else "",
        state="queued", deadline_ts=now + float(days) * SECONDS_PER_DAY, title=title.strip(),
        origin=origin, prompt=prompt, every_minutes=every_minutes, now=now)
    assert item is not None  # no PR, so no uniqueness to collide with
    return item


def cancel(central: CentralStore, mo_id: str) -> dict:
    """Cancel an open order, never judged; a settled one is refused"""
    item = _item_or_raise(central, mo_id)
    if item["state"] not in OPEN_STATES:
        raise ValueError(f"{mo_id} is already {item['state']}")
    central.update_morpheus_item(mo_id, state="cancelled", next_check_ts=None,
                                 resolution="cancelled")
    return central.morpheus_item(mo_id) or {}


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


def set_script(central: CentralStore, item_id: str, script: str, source: str,
               trigger: str = "") -> dict:
    """Give an item its script; a queued item starts monitoring now"""
    item = _item_or_raise(central, item_id)
    script = (script or "").strip()
    if not script:
        raise ValueError("an empty script validates nothing")
    fields: dict[str, Any] = {"script": script, "script_source": source, "trigger_kind": trigger,
                              **_FRESH_WATCH}
    if item["state"] == "queued":
        # Armed: attempts restart (docs/superpowers/specs/2026-11-01-monitoring-orders.md §1.3)
        fields.update(state="monitoring", next_check_ts=db.now(), attempts=0)
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
        action_ts=None, attempts=0, kills=0, last_attempt_ts=None, **_FRESH_WATCH)
    return central.morpheus_item(item_id) or {}


def rewrite(central: CentralStore, cfg: MorpheusConfig, item_id: str, why: str, *,
            resolution: str = "") -> dict:
    """The script is wrong, not the PR: back to Morpheus for a new one, `why` as its brief (#1358)"""
    item = _item_or_raise(central, item_id)
    why = (why or "").strip()
    if not why:
        raise ValueError("say what is wrong with the script")
    now = db.now()
    # A user's script stays for the arm session to revalidate (monitoring-orders §3); last_exit resets
    user = item["script_source"] == "user"
    central.update_morpheus_item(
        item_id, state="queued", script=item["script"] if user else "",
        script_source="user" if user else "", last_exit=None, attempts=0, kills=0,
        last_attempt_ts=None,
        rewrite_brief=(f"{why[:1500]}\n\nThe rejected script:\n"
                       f"{item['script'][:3000] or '(none)'}"),
        deadline_ts=now + cfg.timeout_days * SECONDS_PER_DAY, next_check_ts=None,
        resolution=resolution or f"script sent back to be rewritten: {why[:600]}",
        action="", action_ts=None, trigger_kind="", **_FRESH_WATCH)
    return central.morpheus_item(item_id) or {}


def rewrite_from_investigation(central: CentralStore, cfg: MorpheusConfig, item_id: str,
                               inv_id: str, brief: str, cycles: int) -> tuple[bool, str]:
    """SCRIPT_WRONG verdict: rewrite within MAX_FIX_CYCLES, never MAX_SCRIPT_REWRITES (io-5cc7a40d)"""
    item = _item_or_raise(central, item_id)
    if item["state"] not in ACTION_STATES:
        return True, f"nothing sent back: {item_id} is {item['state']}, decided elsewhere"
    if cycles > MAX_FIX_CYCLES:
        command = f'jarvis morpheus rewrite {item_id} --why "…"'
        central.add_inbox(
            project=item.get("project") or "os", level="warning",
            title=f"PR #{item['pr_number']} {item['state']} production validation — its "
                  f"script is wrong after {cycles} investigations",
            body=f"{item['pr_url']}\n{inv_id}: {brief[:600]}\n{command}")
        return False, f"{cycles} investigations, over the {MAX_FIX_CYCLES} allowed — `{command}`"
    rewrite(central, cfg, item_id, brief,
            resolution=f"script sent back to be rewritten by {inv_id}: {brief[:600]}")
    central.add_inbox(
        project=item.get("project") or "os", level="info",
        title=f"PR #{item['pr_number']} — {inv_id} found its validation script wrong; "
              f"Morpheus is rewriting it",
        body=f"{item['pr_url']}\n{brief[:600]}\njarvis morpheus show {item_id}")
    return True, f"{item_id} sent back to Morpheus for a new script"


def settle_unverified(central: CentralStore, item_id: str, inv_id: str, why: str) -> str:
    """An investigation says nothing is wrong: settled, not passed, nothing on the user (io-5cc7a40d)"""
    item = _item_or_raise(central, item_id)
    if item["state"] not in ACTION_STATES:
        return f"nothing settled: {item_id} is {item['state']}, decided elsewhere"
    central.update_morpheus_item(item_id, state="unverified", next_check_ts=None,
                                 resolution=f"unverified, {inv_id}: {why[:600]}")
    central.add_inbox(
        project=item.get("project") or "os", level="info",
        title=f"PR #{item['pr_number']} — {inv_id} found nothing wrong; its validation "
              f"settled unverified, not passed",
        body=f"{item['pr_url']}\n{why[:600]}\njarvis morpheus show {item_id}")
    return f"{item_id} settled unverified"


def requeue_from_investigation(central: CentralStore, cfg: MorpheusConfig, item_id: str,
                               inv_id: str, why: str, earlier_transients: int) -> str:
    """TRANSIENT verdict: watch again once, without Neo's auto_requeues; a second settles it (Q3599)"""
    item = _item_or_raise(central, item_id)
    if item["state"] not in ACTION_STATES:
        return f"nothing requeued: {item_id} is {item['state']}, decided elsewhere"
    if earlier_transients:
        return settle_unverified(central, item_id, inv_id, why)
    requeue(central, cfg, item_id, resolution=f"re-watched, {inv_id} found it transient: {why[:600]}")
    return f"{item_id} is being watched again"


def _item_or_raise(central: CentralStore, item_id: str) -> dict[str, Any]:
    item = central.morpheus_item(item_id)
    if not item:
        raise KeyError(f"no morpheus item {item_id!r}")
    return item


def _kill_spends_attempt(central: CentralStore, item: dict[str, Any]) -> bool:
    """Count a kill; a kill is no answer, so only MAX_KILLS in a row cost an attempt (kn-1e43fe1a)"""
    kills = int(item.get("kills") or 0) + 1
    central.update_morpheus_item(item["id"], kills=0 if kills >= MAX_KILLS else kills)
    return kills >= MAX_KILLS


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


def exit_reason(result: CheckResult) -> str:
    """What an exit 75 named; a missing or unknown word means ambiguous (§9)"""
    if result.exit != EXIT_NOT_YET:
        return ""
    # Output is stdout then stderr, so the script's last line need not be the output's last
    named = _REASON_LINE.findall(result.output)
    return named[-1] if named and named[-1] in REASONS else AMBIGUOUS


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
    reason = exit_reason(result)
    # Output cut to its tail may have lost a FAIL line, so a full tail cannot count as clean
    saw_fail = bool(item.get("saw_fail") or result.exit == EXIT_FAILED
                    or _FAIL_LINE.search(result.output) or len(result.output) >= OUTPUT_TAIL)
    fields: dict[str, Any] = {"last_check_ts": now, "checks": int(item["checks"]) + 1,
                              "last_check_version": (version or checked_version)(where),
                              "last_exit": result.exit, "last_output": result.output,
                              "last_reason": reason, "saw_fail": int(saw_fail),
                              "next_check_ts": now + int(item.get("every_minutes")
                                                         or cfg.check_every_minutes) * 60}
    held_since = float(item.get("held_since_ts") or now)
    hold_cap = held_since + NOT_DEPLOYED_CAP_DAYS * SECONDS_PER_DAY
    if result.exit == EXIT_VALIDATED:
        fields.update(state="validated", next_check_ts=None, resolution="script exited 0")
    # One exit 1 may be the environment, not the PR, so `failed` needs a second in a row (#1255)
    elif result.exit == EXIT_FAILED and item.get("last_exit") == EXIT_FAILED:
        fields.update(state="failed", next_check_ts=None,
                      resolution="script exited 1 twice in a row")
    elif reason == NOT_DEPLOYED and now < hold_cap:
        # Not deployed starts no clock: the whole window stays for once it is (§9)
        fields.update(held_since_ts=held_since,
                      deadline_ts=max(float(item["deadline_ts"]),
                                      min(now + cfg.timeout_days * SECONDS_PER_DAY, hold_cap)))
    elif now >= float(item["deadline_ts"]):
        if reason == NOT_EXERCISED and not saw_fail:
            fields.update(state="unverified", next_check_ts=None,
                          resolution=f"production never exercised the change within "
                                     f"{cfg.timeout_days:g} day(s) and no check saw a FAIL: "
                                     f"not proof it works")
        else:
            fields.update(state="timedout", next_check_ts=None,
                          resolution=_timeout_reason(item, cfg, result.exit, reason))
    state = str(fields.get("state", item["state"]))
    with db.write_transaction(central.conn):  # the run and the item move together: db.write_transaction
        central.update_morpheus_item(item["id"], **fields)
        central.add_morpheus_run(item["id"], exit=result.exit, output=result.output,
                                 state_after=state, now=now)
    return state


def _timeout_reason(item: dict[str, Any], cfg: MorpheusConfig,
                    last_exit: int | None = None, reason: str = "") -> str:
    if not item.get("script"):
        return f"no script was registered within {cfg.timeout_days:g} day(s)"
    if reason == NOT_DEPLOYED:
        return f"not deployed after {NOT_DEPLOYED_CAP_DAYS} day(s) of holding its deadline"
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


def act(central: CentralStore, catalog: Catalog, item: dict[str, Any], *, cwd: Path,
        call: Callable[..., Any] | None = None, now: float | None = None,
        investigate: Callable[..., tuple[str, str]] | None = None,
        ask_neo: Callable[..., int] | None = None) -> str:
    """Judge the item in one short session, then hand it off once; returns what was recorded (§4)"""
    now = db.now() if now is None else now
    if (item["state"] not in ACTION_STATES or held(central, now)
            or not central.claim_morpheus_action(item["id"], f"{JUDGE_CLAIM}{now:.0f}")):
        return ""
    try:
        return _judge(central, catalog, item, cwd=cwd, call=call, now=now,
                      investigate=investigate, ask_neo=ask_neo)
    except BaseException:
        # A hand-off that crashed is still owed: the claim goes back for the next pass
        central.update_morpheus_item(item["id"], action="", action_ts=None)
        raise


def _judge(central: CentralStore, catalog: Catalog, item: dict[str, Any], *, cwd: Path,
           call: Callable[..., Any] | None, now: float,
           investigate: Callable[..., tuple[str, str]] | None,
           ask_neo: Callable[..., int] | None) -> str:
    from . import claude_cli

    cfg = catalog.os.morpheus
    investigate = investigate or _investigate
    ask_neo = ask_neo or _ask_neo
    attempts = int(item.get("attempts") or 0)
    reply = None
    if attempts < MAX_OFFERS:
        runs = central.morpheus_runs(item["id"], JUDGE_RUNS)
        try:
            reply = run_session(central, catalog, {**item, "runs": runs}, purpose="judge",
                                cwd=cwd, call=call)
        except claude_cli.UsageLimitError:
            central.update_morpheus_item(item["id"], action="", action_ts=None)
            return "usage_held"
        except claude_cli.KilledError as e:
            log.warning("morpheus judge %s killed before it answered: %s", item["id"], e)
            if not _kill_spends_attempt(central, item):
                central.update_morpheus_item(item["id"], action="", action_ts=None)
                return ""
        if reply is not None and item.get("kills"):
            central.update_morpheus_item(item["id"], kills=0)
        if reply is None:
            # No answer is no verdict (kn-40db1828): the claim goes back, one attempt spent
            attempts += 1
            central.update_morpheus_item(item["id"], attempts=attempts, last_attempt_ts=now)
            if attempts < MAX_OFFERS:
                central.update_morpheus_item(item["id"], action="", action_ts=None)
                return ""
    note = (f"the monitoring agent could not judge it in {attempts} session(s)"
            if reply is None else "")
    verdict = reply["verdict"] if reply else "escalate"
    if reply:
        item = {**item, "judge_reason": reply["reason"]}
    if verdict == "rewrite":
        if central.claim_morpheus_script_rewrite(item["id"], MAX_SCRIPT_REWRITES):
            # The judge wrote the script, so no arming session follows
            central.update_morpheus_item(
                item["id"], state="monitoring", script=reply["script"].strip(),
                script_source="agent", last_exit=None, attempts=0, kills=0,
                deadline_ts=now + cfg.timeout_days * SECONDS_PER_DAY, next_check_ts=now,
                resolution=f"script rewritten by the monitoring agent: {reply['reason'][:600]}",
                action="", action_ts=None, trigger_kind="", **_FRESH_WATCH)
            return "rewrite"
        note = f"its {MAX_SCRIPT_REWRITES} automatic script rewrite(s) are spent"
    elif verdict == "confirm":
        route = cfg.on_failed if item["state"] == "failed" else cfg.on_timeout
        if item.get("pr_number") is None:
            note = "it has no pull request to investigate"
        elif route == "investigate":
            inv_id, note = investigate(catalog, item)
            if inv_id:
                action = f"{_action_kind(inv_id)}:{inv_id}"
                central.update_morpheus_item(item["id"], action=action, action_ts=now)
                central.add_inbox(
                    project=item.get("project") or _os_project(catalog), level="warning",
                    title=f"PR #{item['pr_number']} {item['state']} production validation "
                          f"({item['resolution']})",
                    body=f"{item['pr_url']}\n{inv_id} is finding out why.")
                return action
    action = f"neo:{ask_neo(catalog, item, note)}"
    central.update_morpheus_item(item["id"], action=action, action_ts=now)
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
    if item.get("pr_number") is not None:
        subject = f"PR #{item['pr_number']} ({item['pr_url']})"
        what = f"The PR's hints, under `## {SECTION}`:\n{(item.get('hints') or '')[:3000]}"
    else:
        subject = f"Monitoring order {item['id']} ({item.get('title') or 'untitled'})"
        what = f"What it watches:\n{(item.get('prompt') or '')[:3000]}"
    reading = (f"\n\nThe monitoring agent's reading — NOT a diagnosis:\n"
               f"{item['judge_reason'][:1500]}" if item.get("judge_reason") else "")
    return (f"Morpheus: {subject} of project "
            f"{item.get('project') or '?'} {item['state']} its production validation: "
            f"{item['resolution']}. Morpheus only runs the validation script and reads its "
            f"exit code — it has NOT diagnosed anything, and the script itself may be wrong.\n\n"
            f"{what}\n\n"
            f"The script (written by {item['script_source'] or 'nobody yet'}):\n"
            f"{item['script'][:3000] or '(none)'}\n\n"
            f"Last exit {item['last_exit']}, output tail:\n{item['last_output'][-1500:]}"
            f"{reading}")


VALIDATION_QUESTION = """MORPHEUS — a shipped change has no passing production validation.

{why}

Decide whether the user needs to look. If they do, ESCALATE and say what to look at. If
not, answer in two sentences; the answer is recorded on the item.
{actions}"""

#: Every question offers it; a question without the `settle_offered` mark predates it
SETTLE_OFFER = """If the user need not look, add `"action": "settle"`: the item becomes `unverified`, meaning
nothing wrong was found and no check passed. It is not a pass and nobody owes it a move;
the user can watch it again with `jarvis morpheus requeue {id}`. An answer with no action
moves nothing: the item stays {state} and the OS warns the user."""

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
                          (SETTLE_OFFER, offer, REWRITE_SPENT if rewritten else REWRITE_OFFER))
    neo = NeoStore()
    try:
        q = neo.ask(item.get("project") or _os_project(catalog), "",
                    VALIDATION_QUESTION.format(why=why, actions=actions),
                    context=json.dumps({"morpheus_item": item["id"], "settle_offered": True}),
                    kind="validation")
    finally:
        neo.close()
    return int(q["id"])


def reask_answered(central: CentralStore, catalog: Catalog, *,
                   skip: Collection[str] = ()) -> list[str]:
    """Ask once more each row Neo answered before it could settle one; returns the rows asked"""
    from .neo_store import NeoStore

    asked: list[str] = []
    neo = NeoStore()
    try:
        for item in central.morpheus_items(ACTION_STATES):
            kind, _, qid = (item["action"] or "").partition(":")
            if (kind != "neo" or not qid.isdigit() or (item["project"] or "") in skip
                    or (item["resolution"] or "").startswith(CONFIRMED)):
                continue
            q = neo.get(int(qid))
            if (not q or q["status"] != "answered" or q["answered_by"] != "neo"
                    or _settle_offered(q)):
                continue
            try:
                action = f"neo:{_ask_neo(catalog, item)}"
            except Exception:  # noqa: BLE001 — one item must not starve the others
                log.exception("morpheus: could not ask Neo again about %s", item["id"])
                continue
            central.update_morpheus_item(item["id"], action=action, action_ts=db.now())
            asked.append(item["id"])
    finally:
        neo.close()
    return asked


def item_of_question(q: dict[str, Any]) -> str:
    """The item a `validation` question is about, or ''"""
    try:
        return str((json.loads(q.get("context") or "{}") or {}).get("morpheus_item") or "")
    except (ValueError, AttributeError):
        return ""


def _settle_offered(q: dict[str, Any]) -> bool:
    """Whether this question could be answered with `settle`; one that could not is asked again"""
    try:
        return bool((json.loads(q.get("context") or "{}") or {}).get("settle_offered"))
    except (ValueError, AttributeError):
        return False


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
    # Never over the user's own ruling that the failure is real
    settled = (action == "settle" and live and bool(reading)
               and not (item["resolution"] or "").startswith(CONFIRMED))
    if requeued:
        requeue(central, cfg, item_id, resolution=f"re-watched by Neo: {resolution}")
    elif settled:
        central.update_morpheus_item(item_id, state="unverified", next_check_ts=None,
                                     resolution=f"unverified by Neo: {resolution}")
    elif rewritten:
        rewrite(central, cfg, item_id, reading,
                resolution=f"script sent back to be rewritten by Neo: {resolution}")
    else:
        central.update_morpheus_item(item_id, resolution=resolution)
    # Nothing retries this row now: silence would hide it, so the user hears (wo-ea6895e7)
    unmoved = (live and not (escalated or requeued or settled or rewritten)
               and _settle_offered(q) and not (item["resolution"] or "").startswith(CONFIRMED))
    central.add_inbox(
        project=q.get("project") or "os", level="warning" if escalated or unmoved else "info",
        title=(f"PR #{item['pr_number']} {item['state']} production validation"
               + (" — Neo escalated" if escalated else
                  " — Neo is watching it again" if requeued else
                  " — Neo sent its script back to be rewritten" if rewritten else
                  " — Neo settled it unverified, not passed" if settled else
                  " — Neo answered and nothing moved" if unmoved else
                  " — Neo's reading")),
        body=f"{item['pr_url']}\n{reading[:600]}\n"
             + ("the OS is validating it again — its one automatic requeue is now spent\n"
                if requeued else "")
             + ("Morpheus writes a new script, then the OS watches it again — its one "
                "automatic rewrite is now spent\n" if rewritten else "")
             + (f"nobody owes it a move; jarvis morpheus requeue {item_id} watches it again\n"
                if settled else "")
             + (f"it stays {item['state']} and nothing retries it. Yours to move: jarvis morpheus "
                f"requeue|waive|confirm|rewrite {item_id}. A wrong reading is a bug to file\n"
                if unmoved else "")
             + (f"{question_url}\n" if question_url else "")
             + f"jarvis morpheus show {item_id}")


# -- the only LLM: one short session per order (docs/superpowers/specs/2026-11-01-monitoring-orders.md §3)

MORPHEUS_PERSONA = """You are MORPHEUS, the production monitor of the Jarvis agentic OS.

The OS ships changes to production, for every project it runs. Each time you run, you are
given ONE monitoring order: one thing production should show working. You run in a fresh
session from that project's production directory, with read-only tools.

You never run `jarvis`, never change production and never write outside /tmp. You never
judge a result yourself: the OS runs what you return on a schedule and reads its exit code.

Read what you need (the PR with `gh pr view <url>`, the project's code, its logs), then reply
with ONE JSON object and nothing else, in the shape the order asks for."""

#: The exit-code contract every arming prompt opens with: identical text, so it caches
ARM_CONTRACT = """Write the bash script that monitors this order. The script:
- runs from the production directory you are in;
- is READ-ONLY: it inspects production (CLIs with --json, logs, `systemctl --user status`,
  `gh` reads, HTTP GETs) and never changes anything;
- exits 0 when production shows the change working;
- exits 1 ONLY when production shows THIS change's own failure signature: the exact symptom
  it fixed, happening again. Anything the change does not own is NOT a failure: a command
  that errored, an endpoint that was unreachable, a missing file. Those exit 75;
- exits 75 when it can say neither, and then prints as its LAST line `REASON <why>`, one of:
  not_deployed (the change is not in this production yet), not_exercised (it is deployed,
  every read is clean, and nothing has triggered it yet), unreadable (it cannot read the
  environment), ambiguous (it read evidence that settles nothing, or looks wrong without
  being this change's own failure signature). Anything else counts as ambiguous;
- PROBES when it can: a read-only command that exercises the change now (a CLI read, an HTTP
  GET) comes before waiting for traffic, and it waits only when no such probe exists;
- finishes in well under a minute;
- prints ONE line per thing it checks, starting PASS, FAIL or UNKNOWN, naming it in plain
  words a human reads without the script, never a bare internal field name;
- never prints the release that first carried the change: the OS records that already.

Reply {"script": "<the bash script>", "trigger": "probe" if it exercises the change itself or
"ambient" if it can only wait for production to, "notes": "<one line: what it checks>"}.
If what to watch cannot be made mechanical, reply {"refuse": "<why>"} instead."""

#: The os_state key holding the account's usage-window hold: one for every order
USAGE_HOLD_KEY = "morpheus.usage_hold"
#: Tools an order's session may use: reads only, and Bash for `gh` and the CLIs it inspects
SESSION_TOOLS = "Bash,Read,Glob,Grep"


def _arm_prompt(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("pr_title") or "(untitled)"
    what = (item.get("hints") or item.get("prompt")
            or ("(only the user's script below)" if item.get("script_source") == "user"
                else "(the PR gave no hints at all: read the PR)"))
    parts = [ARM_CONTRACT,
             f"## Order {item['id']} — {item.get('project') or '?'}: {title}"
             + (f"\n{item['pr_url']}" if item.get("pr_url") else ""),
             f"What to watch:\n{what[:4000]}"]
    if item.get("rewrite_brief"):
        parts.append("A REWRITE: its previous script was ruled wrong, not the change. Write one "
                     f"that avoids this:\n{item['rewrite_brief']}")
    if item.get("script_source") == "user" and item.get("script"):
        parts.append("The user wrote this script. Return it unchanged if it keeps the contract; "
                     f"change it only where it does not:\n{item['script'][:4000]}")
    return "\n\n".join(parts)


def _arm_reply(data: dict[str, Any]) -> dict[str, str]:
    refuse = data.get("refuse")
    if isinstance(refuse, str) and refuse.strip():
        return {"refuse": refuse.strip()}
    script = data.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("neither a script nor a refusal")
    trigger = str(data.get("trigger") or "").strip().lower()
    return {"script": script, "notes": str(data.get("notes") or ""),
            "trigger": trigger if trigger in TRIGGERS else ""}


#: What a judge session may answer (monitoring-orders §4)
JUDGE_VERDICTS = ("rewrite", "confirm", "escalate")
#: The `action` a judging pass holds while its session runs: `judge:<ts>`
JUDGE_CLAIM = "judge:"
#: How many of the order's last runs the judge is shown
JUDGE_RUNS = 5

#: Opens every judging prompt: identical text, so it caches
JUDGE_CONTRACT = """This monitoring order ended without passing. Read the record below and
production, then say which of three it is:
- the SCRIPT is wrong, not the change: {"verdict": "rewrite", "reason": "<what the script got
  wrong>", "script": "<a new bash script>"}; the new script keeps the rules below;
- the change looks broken in production: {"verdict": "confirm", "reason": "<what production
  shows>"}; the OS files an investigation;
- you cannot tell, or a human must look: {"verdict": "escalate", "reason": "<what to look at>"}.
Your reason is a reading, never a diagnosis.

The rules a script keeps:
""" + ARM_CONTRACT.split("\n\nReply ")[0].split("\n", 1)[1]


def _when(ts: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts))) if ts else "never"


def _judge_prompt(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("pr_title") or "(untitled)"
    what = item.get("hints") or item.get("prompt") or "(none given)"
    runs = "\n".join(f"- {_when(r['ts'])}: exit {r['exit']}\n{(r['output'] or '')[-600:]}"
                     for r in item.get("runs") or ()) or "(none)"
    return "\n\n".join([
        JUDGE_CONTRACT,
        f"## Order {item['id']} — {item.get('project') or '?'}: {title}"
        + (f"\n{item['pr_url']}" if item.get("pr_url") else ""),
        f"What to watch:\n{what[:4000]}",
        f"The script (written by {item.get('script_source') or 'nobody'}):\n"
        f"{(item.get('script') or '(none)')[:4000]}",
        f"Its last runs, newest first:\n{runs}",
        f"It is {item['state']}: {item.get('resolution') or '?'} "
        f"(deadline {_when(item.get('deadline_ts'))}).",
        f"Script rewrites so far: {int(item.get('script_rewrites') or 0)} of "
        f"{MAX_SCRIPT_REWRITES}; automatic requeues so far: "
        f"{int(item.get('auto_requeues') or 0)} of {MAX_AUTO_REQUEUES}."])


def _judge_reply(data: dict[str, Any]) -> dict[str, str]:
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in JUDGE_VERDICTS:
        raise ValueError(f"verdict must be one of {list(JUDGE_VERDICTS)}")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a verdict needs its reason")
    out = {"verdict": verdict, "reason": reason.strip()}
    if verdict == "rewrite":
        script = data.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ValueError("a rewrite needs its script")
        out["script"] = script
    return out


#: purpose: (prompt built from the row, reply validator)
SESSION_PROMPTS: dict[str, tuple[Callable[[dict], str], Callable[[dict], Any]]] = {
    "arm": (_arm_prompt, _arm_reply), "judge": (_judge_prompt, _judge_reply)}


def usage_hold_until(central: CentralStore, now: float | None = None) -> float | None:
    """When the usage window reopens, or None when no hold stands"""
    now = db.now() if now is None else now
    try:
        until = float(central.get_state(USAGE_HOLD_KEY) or 0)
    except ValueError:
        until = 0.0
    return until if now < until else None


def held(central: CentralStore, now: float | None = None) -> bool:
    """Morpheus's brake or the usage hold stands: no session runs, none is owed (Q3138)"""
    return bool(pause_state(central)["paused"] or usage_hold_until(central, now))


def run_session(central: CentralStore, catalog: Catalog, item: dict[str, Any], *,
                purpose: str, cwd: Path, call: Callable[..., Any] | None = None
                ) -> dict[str, Any] | None:
    """One fresh `claude -p` per order (§1.3): reply or None; re-raises kill and usage-limit errors"""
    from . import agent_usage, claude_cli, structured

    if purpose not in SESSION_PROMPTS:
        raise ValueError(f"no session purpose {purpose!r}: {sorted(SESSION_PROMPTS)}")
    build, validate = SESSION_PROMPTS[purpose]
    cfg = catalog.os.morpheus
    call = call or claude_cli.run_headless_result
    try:
        result = call(build(item), MORPHEUS_PERSONA, model=cfg.model, cwd=cwd,
                      timeout=cfg.turn_timeout_seconds, tools=SESSION_TOOLS,
                      records_itself=agent_usage.MORPHEUS,
                      permission_mode=catalog.os.default_permission_mode, persist=True)
    except claude_cli.UsageLimitError as e:
        # BEFORE ClaudeCliError, its base: the window is the account's, so it holds every order
        from .worker_session import RATE_LIMIT_FALLBACK_DELAY

        central.set_state(USAGE_HOLD_KEY,
                          str(e.limit.reset_at or db.now() + RATE_LIMIT_FALLBACK_DELAY))
        log.warning("morpheus %s %s refused by the usage window: %s", purpose, item["id"], e)
        raise
    except claude_cli.KilledError:
        raise
    except claude_cli.ClaudeCliError as e:
        log.warning("morpheus %s %s failed: %s", purpose, item["id"], e)
        return None
    reply = structured.coerce(result.text or "", validate, on_invalid=lambda _raw: None)
    agent_usage.record(agent_usage.MORPHEUS, usage=result, wo_id=item["id"],
                       project=item.get("project") or "", label=purpose,
                       session_id=result.session_id, ok=reply is not None, store=central)
    if result.session_id:
        central.update_morpheus_item(item["id"], last_session_id=result.session_id)
    return reply


def owed_arming(central: CentralStore, *, now: float | None = None,
                skip: Collection[str] = ()) -> list[dict[str, Any]]:
    """Queued orders owed an arming session now, oldest first (§1.3)"""
    now = db.now() if now is None else now
    return [i for i in reversed(central.morpheus_items(("queued",)))
            if (i["project"] or "") not in skip
            and int(i["attempts"] or 0) < MAX_OFFERS
            and now - float(i["last_attempt_ts"] or 0) >= REOFFER_SECONDS]


def arm(central: CentralStore, catalog: Catalog, cwd_for: CwdFor, *,
        now: float | None = None, call: Callable[..., Any] | None = None,
        skip: Collection[str] = ()) -> dict[str, int]:
    """One session per owed order, one at a time, so the shared system prompt stays cached"""
    from . import claude_cli

    now = db.now() if now is None else now
    if central.get_state("morpheus.session"):
        # The long-lived session's record: its offers moved into the rows at migration (§2)
        central.set_state("morpheus.session", "")
    if pause_state(central)["paused"]:
        return {"paused": 1}
    if usage_hold_until(central, now):
        return {"usage_held": 1}
    out = {"armed": 0, "refused": 0, "failed": 0, "killed": 0}
    for item in owed_arming(central, now=now, skip=skip):
        try:
            reply = run_session(central, catalog, item, purpose="arm",
                                cwd=_cwd(cwd_for, item), call=call)
        except claude_cli.UsageLimitError:
            out["usage_held"] = 1
            break
        except claude_cli.KilledError as e:
            out["killed"] += 1
            if _kill_spends_attempt(central, item):
                central.update_morpheus_item(item["id"], last_attempt_ts=now,
                                             attempts=int(item["attempts"] or 0) + 1)
            log.warning("morpheus arm %s killed before it answered: %s", item["id"], e)
            continue
        if reply is None or "refuse" in reply:
            out["failed" if reply is None else "refused"] += 1
            fields: dict[str, Any] = {"attempts": int(item["attempts"] or 0) + 1,
                                      "last_attempt_ts": now}
            if reply is not None:
                fields.update(kills=0, resolution=f"arming refused: {reply['refuse'][:600]}")
            central.update_morpheus_item(item["id"], **fields)
            continue
        user = (item["script_source"] == "user"
                and reply["script"].strip() == (item["script"] or "").strip())
        set_script(central, item["id"], reply["script"], "user" if user else "agent",
                   trigger=reply["trigger"])
        central.update_morpheus_item(item["id"], kills=0, last_attempt_ts=now)
        out["armed"] += 1
    return out


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
    """Morpheus's override, the fleet's pause, and whether sessions are held"""
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


def activity(central: CentralStore, *, now: float | None = None) -> dict[str, Any]:
    """What Morpheus is doing, from the rows and the hold — there is no session (§1.3)"""
    now = db.now() if now is None else now
    hold = usage_hold_until(central, now)
    owed_arm, owed_judge = len(owed_arming(central, now=now)), len(owed_actions(central))
    state = ("paused" if pause_state(central)["paused"] else "usage_held" if hold
             else "arming" if owed_arm else "judging" if owed_judge else "idle")
    return {"state": state, "owed_arm": owed_arm, "owed_judge": owed_judge,
            "usage_hold_until": hold, "max_offers": MAX_OFFERS}


# -- what the surfaces read

def report(central: CentralStore, catalog: Catalog | None = None) -> dict[str, Any]:
    """Counts first, then what Morpheus is doing, then every item"""
    pause = pause_state(central)
    return {"enabled": bool(catalog.os.morpheus.enabled) if catalog else None,
            "counts": {s: central.morpheus_counts().get(s, 0) for s in STATES},
            "paused": pause["paused"], "pause": pause,
            "activity": activity(central),
            "last_release": central.get_state(RELEASE_KEY) or "",
            "items": central.morpheus_items()}
