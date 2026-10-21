"""What a dead turn left on disk, read off it while it is still there (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md)"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import background, branchproof, db, landing

log = logging.getLogger("jarvis.harvest")

#: The event kind
EVENT = "turn_harvested"

#: Payload version, so a reader can tell a shape it does not know from one it does
VERSION = 1

#: How much of the worker's last message rides in the payload
SAID_CHARS = 2000

#: How much of GIT'S OWN STDERR rides in `checkpoint_skipped` (spec §7). git writes multi-line errors
SKIP_CHARS = 200

#: The machine handle on the checkpoint commit: a later harvest skips a HEAD that already carries it
TRAILER = "Jarvis-Checkpoint"

#: Identity for the checkpoint, passed with `-c` so nothing is written to any config
_IDENTITY = ("-c", "user.name=Jarvis", "-c", "user.email=jarvis@localhost")

CHECKPOINT_MESSAGE = """\
WIP: Jarvis checkpoint of {wo_id} turn {seq}

This turn's process ended without writing a result. The OS committed what was in
the worktree so it is not lost. Nobody reviewed it. Amend, reset or rewrite freely.

{trailer}: {wo_id}/{seq}"""


def collect(store: Any, wo: dict[str, Any], turn: dict[str, Any], *,
            said: str) -> dict[str, Any]:
    """Read the worktree, checkpoint what is uncommitted, and return the payload"""
    wo_id = str(wo["id"])
    seq = turn["seq"]
    worktree = landing.worktree_of(store.project_path, wo)
    authored = landing.authored(worktree)
    head = _head_at_launch(store, wo_id, seq)
    since, turn_commits = _turn_commits(worktree, head, authored)
    upstream, unpushed = _pushed(worktree)
    # WHAT THE TURN LEFT RUNNING, through its ONE writer: `background.record` is the only writer of the...
    jobs = background.orphaned_in_turn(store, wo_id, turn)
    if jobs:
        background.record(store, wo_id, turn, jobs, None)  # a failed turn recorded no reply
    checkpoint, skipped = _checkpoint(worktree, wo_id, seq, authored)
    clipped = (said or "")[:SAID_CHARS]
    return {
        "seq": seq, "version": VERSION,
        "empty": not (clipped or authored.commits or authored.dirty or jobs
                      or turn_commits or checkpoint),
        "since": since, "said": clipped, "authored": authored.record(),
        "turn_commits": turn_commits,
        "checkpoint": checkpoint, "checkpoint_skipped": skipped,
        "detached": _detached(authored), "upstream": upstream, "unpushed": unpushed,
        "pr_url": _pr_url(wo, clipped), "jobs": len(jobs),
        "unreadable": authored.unreadable,
    }


def write(store: Any, wo: dict[str, Any], turn: dict[str, Any], *,
          said: str) -> dict[str, Any] | None:
    """`collect`, then the one event"""
    payload = collect(store, wo, turn, said=said)
    store.add_event(str(wo["id"]), EVENT, payload)
    return payload


def of_turn(store: Any, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The harvest against this work order's LATEST turn, if that turn has one"""
    turn = store.latest_turn(wo["id"])
    if turn is None:
        return None
    for event in reversed(store.events_of_kind(wo["id"], EVENT)):
        payload = db.from_json(event.get("payload"), {}) or {}
        if payload.get("seq") == turn["seq"]:
            return payload
    return None


def retry_brief(store: Any, wo: dict[str, Any]) -> str:
    """The relaunch prose, or "" when there is no harvest or it found nothing"""
    payload = of_turn(store, wo)
    if not payload or payload.get("empty"):
        return ""
    lines = [BRIEF_HEAD]
    said = str(payload.get("said") or "")
    if said:
        lines.append(f'- you last said: "{said}"')
    authored = payload.get("authored") or {}
    commits = int(authored.get("commits") or 0)
    if commits:
        made = len(payload.get("turn_commits") or ())
        lines.append(
            f"- `{authored.get('branch') or 'its branch'}` carries {_n(commits, 'commit')}"
            f" over `{authored.get('base') or 'its base'}`"
            + (f", {made} of them made in that turn" if made else ""))
    dirty = list(authored.get("dirty") or ())
    checkpoint = str(payload.get("checkpoint") or "")
    if dirty and checkpoint:
        lines.append(
            f"- {_n(len(dirty), 'uncommitted file')} was committed for you as a WIP "
            f"checkpoint {checkpoint} (trailer `{TRAILER}: {wo['id']}/{payload['seq']}`)"
            f" — amend, reset or rewrite it freely")
    elif dirty:
        lines.append(f"- {_n(len(dirty), 'uncommitted file')} is still uncommitted in the "
                     f"worktree: {', '.join(dirty[:10])}"
                     + (f" ({payload.get('checkpoint_skipped')})"
                        if payload.get("checkpoint_skipped") else ""))
    if payload.get("pr_url"):
        lines.append(f"- a pull request is already open: {payload['pr_url']}")
    orphan = background.pending_orphan(store, wo)
    if orphan:
        jobs = background.jobs_of(orphan)
        if jobs:
            lines.append(f"- {_n(len(jobs), 'background job')} died with the turn: "
                         f"{background.labels(jobs)}")
    if payload.get("unreadable"):
        lines.append(f"- the OS could not read the worktree: {payload['unreadable']}")
    lines.append(BRIEF_TAIL)
    return "\n".join(lines)


#: §6's opening
BRIEF_HEAD = (
    "[Jarvis] The OS is relaunching this work order because the user asked for it. Its "
    "last turn ended without writing a result. Here is what the OS found on disk, so you "
    "do not have to re-derive it:")

BRIEF_TAIL = "Verify this against the worktree, then carry on. Do not start again."


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _head_at_launch(store: Any, wo_id: str, seq: int) -> str:
    """The sha `worker_session._launch` recorded on `turn_started`, or "\""""
    for event in reversed(store.events_of_kind(wo_id, "turn_started")):
        payload = db.from_json(event.get("payload"), {}) or {}
        if payload.get("seq") == seq:
            return str(payload.get("head") or "")
    return ""


def _turn_commits(worktree: Path | None, head: str,
                  authored: landing.Authored) -> tuple[str, list[str]]:
    """(`since`, the shas)"""
    if worktree is None or not worktree.is_dir():
        return ("turn" if head else "order"), []
    span = f"{head}..HEAD" if head else (f"{authored.base}..HEAD"
                                         if authored.base else "")
    if not span:
        return "order", []
    out = branchproof.run(worktree, "rev-list", "--abbrev-commit", span)
    return ("turn" if head else "order"), (out or "").split()


def _pushed(worktree: Path | None) -> tuple[str, int]:
    """What the branch's upstream is and how far ahead of it HEAD is"""
    if worktree is None or not worktree.is_dir():
        return "", 0
    upstream = branchproof.run(worktree, "rev-parse", "--abbrev-ref", "HEAD@{upstream}")
    if not upstream:
        return "", 0
    count = branchproof.run(worktree, "rev-list", "--count", "@{upstream}..HEAD")
    ahead = (count or "").strip()
    return upstream.strip(), int(ahead) if ahead.isdigit() else 0


def _detached(authored: landing.Authored) -> bool:
    return authored.branch == "HEAD"


def _pr_url(wo: dict[str, Any], said: str) -> str:
    """The recorded pull request first, then one the worker named in its last words (#232)"""
    recorded = str(wo.get("pr_url") or "")
    if recorded:
        return recorded
    found = landing.pr_urls_in(said)
    return found[0] if found else ""


def _checkpoint(worktree: Path | None, wo_id: str, seq: int,
                authored: landing.Authored) -> tuple[str, str]:
    """(sha, reason it was skipped)"""
    if worktree is None or not worktree.is_dir():
        return "", ""
    if not authored.dirty:
        return "", ""
    interrupted = _interrupted(worktree)
    if interrupted:
        return "", interrupted
    trailer = f"{TRAILER}: {wo_id}/{seq}"
    if trailer in (branchproof.run(worktree, "log", "-1", "--format=%B") or ""):
        return "", "this turn was already checkpointed"
    if branchproof.run(worktree, *_IDENTITY, "add", "-A") is None:
        return "", "git could not stage the worktree"
    message = CHECKPOINT_MESSAGE.format(wo_id=wo_id, seq=seq, trailer=TRAILER)
    out, stderr = branchproof.attempt(worktree, *_IDENTITY, "commit", "--no-verify",
                                      "--no-gpg-sign", "-m", message)
    if out is None:
        # spec §7: git's own words, collapsed to one line and clipped
        reason = " ".join(stderr.split())[:SKIP_CHARS]
        return "", (f"git refused the checkpoint commit: {reason}" if reason
                    else "git refused the checkpoint commit — see the daemon log")
    sha = branchproof.run(worktree, "rev-parse", "--short", "HEAD")
    return (sha or "").strip(), ""


#: A half-finished operation git owns
_INTERRUPTED = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD")


def _interrupted(worktree: Path) -> str:
    for name in _INTERRUPTED:
        path = branchproof.run(worktree, "rev-parse", "--git-path", name)
        if path and (worktree / path.strip()).exists():
            return "a rebase or merge is in progress"
    return ""
