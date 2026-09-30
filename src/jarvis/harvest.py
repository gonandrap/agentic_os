"""What a dead turn left on disk, read off it while it is still there.

A worker turn whose `claude` process exits without writing its result JSON used to lose
everything the turn did: the no-result branch of `worker_session._reap` recorded one
sentence of transport diagnosis and settled. Every other settlement in the OS
(`ops.finish` -> `Authored.record`, the success reap -> `background.record`) writes down
what it saw while it could still see it. This is that branch doing the same.

Mechanically, with NO MODEL CALL: the daemon runs it inside its tick for every failed
turn, and a settlement that depends on the network cannot be the thing that records why
the network broke. Three git reads, one local checkpoint commit, one `turn_harvested`
event.

WHY IT IS NOT IN `worker_session`: the harvest needs `landing`, and `landing.py` imports
`worker_session` — a module-level import back would be a cycle. `_reap` imports this
LAZILY inside the branch, which is that module's existing idiom. Layering: an adapter,
below `ops`/`daemon`, above the stores.

THE THINGS THIS NEVER DOES, all three settled and none of them reopenable here:
`work_orders.result_summary` is not written (Neo q1131 — machine prose there makes a
failed order read as delivered to the panel, to Neo's autoreview, to search and to the
settler); nothing is pushed, no branch is made and no pull request is opened (Neo q1132);
and it runs on the `turn_failed` outcome alone, never on a pause, because a pause resumes
the same session and a checkpoint commit under a live worker surprises it mid-task.

Spec: docs/specs/2026-09-30-harvesting-a-dead-turn.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import background, branchproof, db, landing

log = logging.getLogger("jarvis.harvest")

#: The event kind. `timeline.TURN_HARVESTED` is the same string and is what renderers
#: import; this is here so the writer and the reader below agree without importing it.
EVENT = "turn_harvested"

#: Payload version, so a reader can tell a shape it does not know from one it does.
VERSION = 1

#: How much of the worker's last message rides in the payload. `wo_events` is a SQLite
#: row and the transcript is where the rest lives while it lives.
SAID_CHARS = 2000

#: The machine handle on the checkpoint commit: a later harvest skips a HEAD that already
#: carries it, and the relaunched worker can find its own checkpoint with `git log --grep`.
TRAILER = "Jarvis-Checkpoint"

#: Identity for the checkpoint, passed with `-c` so nothing is written to any config.
_IDENTITY = ("-c", "user.name=Jarvis", "-c", "user.email=jarvis@localhost")

CHECKPOINT_MESSAGE = """\
WIP: Jarvis checkpoint of {wo_id} turn {seq}

This turn's process ended without writing a result. The OS committed what was in
the worktree so it is not lost. Nobody reviewed it. Amend, reset or rewrite freely.

{trailer}: {wo_id}/{seq}"""


def collect(store: Any, wo: dict[str, Any], turn: dict[str, Any], *,
            said: str) -> dict[str, Any]:
    """Read the worktree, checkpoint what is uncommitted, and return the payload.

    `said` is passed IN rather than read here: `worker_session._last_assistant_message`
    already walks the `hook:Stop` events, and importing that module would be the cycle
    this one exists to avoid (spec §2).

    EVERY FAILURE RECORDS RATHER THAN RAISES — `unreadable` and `checkpoint_skipped`
    carry the reason verbatim and the payload is returned in all cases. The one caller
    wraps this anyway; that belt is in `_reap` so an ImportError is caught too.
    """
    wo_id = str(wo["id"])
    seq = turn["seq"]
    worktree = landing.worktree_of(store.project_path, wo)
    authored = landing.authored(worktree)
    head = _head_at_launch(store, wo_id, seq)
    since, turn_commits = _turn_commits(worktree, head, authored)
    upstream, unpushed = _pushed(worktree)
    # WHAT THE TURN LEFT RUNNING, through its ONE writer: `background.record` is the only
    # writer of the event `pending_orphan`, `resume_note` and `timeline.VOID_MESSAGE` all
    # read, so the payload here carries the count and never a second copy of the list.
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
    """`collect`, then the one event. Written even when there was nothing to harvest —
    "nothing was there" is an answer and the timeline says so (spec §7)."""
    payload = collect(store, wo, turn, said=said)
    store.add_event(str(wo["id"]), EVENT, payload)
    return payload


def of_turn(store: Any, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The harvest against this work order's LATEST turn, if that turn has one.

    Keyed on the turn, `background.pending_orphan`'s rule and for its reason: the next
    turn is the retry, and once it has run the brief has been given. Nothing has to be
    cleared.
    """
    turn = store.latest_turn(wo["id"])
    if turn is None:
        return None
    for event in reversed(store.events_of_kind(wo["id"], EVENT)):
        payload = db.from_json(event.get("payload"), {}) or {}
        if payload.get("seq") == turn["seq"]:
            return payload
    return None


def retry_brief(store: Any, wo: dict[str, Any]) -> str:
    """The relaunch prose, or "" when there is no harvest or it found nothing.

    "" is what makes `ops.retry` fall back to `RETRY_NOTE`: the OS never opens a turn
    with an empty claim that something was saved (spec §6, §7).
    """
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


#: §6's opening. Says who is speaking, because the user did not write it —
#: `background.RESUME_NOTE`'s rule.
BRIEF_HEAD = (
    "[Jarvis] The OS is relaunching this work order because the user asked for it. Its "
    "last turn ended without writing a result. Here is what the OS found on disk, so you "
    "do not have to re-derive it:")

BRIEF_TAIL = "Verify this against the worktree, then carry on. Do not start again."


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _head_at_launch(store: Any, wo_id: str, seq: int) -> str:
    """The sha `worker_session._launch` recorded on `turn_started`, or "".

    Absent for a turn launched before this shipped, or one whose worktree git could not
    read — which is why the payload says WHICH question it answered rather than leaving
    a reader to guess (spec §2).
    """
    for event in reversed(store.events_of_kind(wo_id, "turn_started")):
        payload = db.from_json(event.get("payload"), {}) or {}
        if payload.get("seq") == seq:
            return str(payload.get("head") or "")
    return ""


def _turn_commits(worktree: Path | None, head: str,
                  authored: landing.Authored) -> tuple[str, list[str]]:
    """(`since`, the shas). "turn" when the launch sha is on the record, else "order"."""
    if worktree is None or not worktree.is_dir():
        return ("turn" if head else "order"), []
    span = f"{head}..HEAD" if head else (f"{authored.base}..HEAD"
                                         if authored.base else "")
    if not span:
        return "order", []
    out = branchproof.run(worktree, "rev-list", "--abbrev-commit", span)
    return ("turn" if head else "order"), (out or "").split()


def _pushed(worktree: Path | None) -> tuple[str, int]:
    """What the branch's upstream is and how far ahead of it HEAD is. Read-only: `gh` is
    `Daemon.poll_pull_requests`' business and it runs on its own cadence (spec §2)."""
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
    """The recorded pull request first, then one the worker named in its last words —
    `landing.pr_urls_in`'s Mode A (issue #232)."""
    recorded = str(wo.get("pr_url") or "")
    if recorded:
        return recorded
    found = landing.pr_urls_in(said)
    return found[0] if found else ""


def _checkpoint(worktree: Path | None, wo_id: str, seq: int,
                authored: landing.Authored) -> tuple[str, str]:
    """(sha, reason it was skipped). A LOCAL commit, never a push and never a branch.

    `--no-verify` because a project's pre-commit hook can reject or hang and the daemon
    is not the place to run a project's test suite; `add -A` because `Authored`'s own
    `--untracked-files=all` rule is what catches the worker that wrote a new package and
    never staged it. `.gitignore` still applies. NOT A STASH: the stash stack is shared
    across every worktree and another session may pop it (spec §3).
    """
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
    if branchproof.run(worktree, *_IDENTITY, "commit", "--no-verify", "--no-gpg-sign",
                       "-m", message) is None:
        # `branchproof.run` logs git's stderr and returns None; the reason a reader gets
        # here is the refusal itself, not the log line (spec §7).
        return "", "git refused the checkpoint commit — see the daemon log"
    sha = branchproof.run(worktree, "rev-parse", "--short", "HEAD")
    return (sha or "").strip(), ""


#: A half-finished operation git owns. Committing on top of one would resolve it for the
#: worker, which is the one thing a checkpoint must not decide (spec §7).
_INTERRUPTED = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD")


def _interrupted(worktree: Path) -> str:
    for name in _INTERRUPTED:
        path = branchproof.run(worktree, "rev-parse", "--git-path", name)
        if path and (worktree / path.strip()).exists():
            return "a rebase or merge is in progress"
    return ""
