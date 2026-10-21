"""The one place in the OS that writes to GitHub: merging a pull request the panel read (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md)"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from .project_store import validation_standing
from .provenance import (
    CONTEXT_LIMIT,
    DESCRIPTION_LIMIT,
    TITLE_LIMIT,
    WO_DESCRIPTION,
    WO_DESCRIPTION_WHOSE,
    WO_TITLE,
    WO_TITLE_WHOSE,
    Borrowed,
    borrowed_context,
    borrowed_sections,
)

log = logging.getLogger(__name__)

#: The gate every merge here rides
GATE_KIND = "auto_merge"

#: EVERY `gh` SUBCOMMAND THIS MODULE MAY RUN
WRITE_VERBS = (("pr", "merge"),)

#: A grant covers ONE merge
GRANT_USES = 1

#: A merge is one API round trip
MERGE_TIMEOUT = 60

#: Why a hold was recorded, as a stable token (#263)
HELD_DISABLED = "disabled"
HELD_STATUS = "status"
HELD_ASSUMPTIONS = "assumptions"
#: ITS OWN CODE, never folded into `HELD_ASSUMPTIONS`: the dedupe key above would drop the second of... (docs/superpowers/specs/2026-09-24-a-planner-assumption-holds-its-feature.md §2.4)
HELD_PLAN_ASSUMPTIONS = "plan_assumptions"
HELD_NOT_PASSED = "not_passed"
HELD_SHA_UNRECORDED = "sha_unrecorded"
HELD_SHA_MOVED = "sha_moved"
HELD_PR_CLOSED = "pr_closed"
HELD_NOT_MERGEABLE = "not_mergeable"
#: Three worlds `github.PullRequest.checks_green` is false in (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §5)
HELD_CHECKS_FAILED = "checks_failed"
HELD_CHECKS_RUNNING = "checks_running"
HELD_CHECKS_NONE = "checks_none"
HELD_MERGE_STATE_UNCLEAN = "merge_state_unclean"
#: The default branch is broken
HELD_BASE_RED = "base_red"
#: The base moved and this head does not contain it
HELD_BASE_MOVED = "base_moved"

#: WHY the merge command did not succeed on a pull request that merged anyway (§5.5)
CLEANUP = "cleanup"
UNFINISHED = "unfinished"

#: The event each cause writes
AFTER_MERGE_EVENT = {CLEANUP: "automerge_cleanup_failed",
                     UNFINISHED: "automerge_command_unfinished"}


class AutoMergeRefused(Exception):
    """NOTHING WAS ATTEMPTED"""


class MergeFailed(AutoMergeRefused):
    """The merge RAN and GitHub refused it"""


class StaleBase(AutoMergeRefused):
    """The base moved under the judged commit"""


@dataclass(frozen=True)
class BaseBehind:
    """The pull request's base has a commit this head does not contain"""

    base: str
    base_oid: str


@dataclass(frozen=True)
class BaseRed:
    """The default branch of this project is red, as plain data `decide` can read (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §2)"""

    base: str
    workflow: str
    run_url: str
    head_sha: str
    wo_id: str = ""
    fix_wo_id: str = ""
    issue_url: str = ""
    issue_os_filed: bool = False


@dataclass(frozen=True)
class Decision:
    """Armed, or held with a reason a person can read"""

    armed: bool
    code: str
    reason: str
    #: The commit the panel judged, when there is one
    judged_sha: str = ""
    #: The commit at the head of the pull request as this tick's `gh pr view` saw it
    head_sha: str = ""
    round_id: int = 0
    round_n: int = 0


def _held(code: str, reason: str, **fields: Any) -> Decision:
    return Decision(armed=False, code=code, reason=reason, **fields)


def hold_payload(decision: Decision) -> dict[str, Any]:
    """The `automerge_held` event payload, in ONE place.

    `Daemon._note_automerge_held` writes through this and the tests build their rows
    through it, so the payload's shape has a single definition: a hand-written key in a
    test could otherwise go on passing after the writer renamed one. HERE rather than in
    `daemon`, because every reader of these keys — `ops.automerge_state`,
    `ops._automerge_hold_is_stale`, `ops.force_validation_state` — is reading this
    module's `Decision` back out, and `daemon` is not importable from a pure test of it.
    """
    return {"code": decision.code, "reason": decision.reason,
            "judged_sha": decision.judged_sha, "head_sha": decision.head_sha,
            "round": decision.round_n}


def hold_is_live(state: dict[str, Any] | None) -> bool:
    """Is this `ops.automerge_state` row a hold the OS is making NOW, by the record?

    The first two conditions of §3 of docs/superpowers/specs/2026-10-20-a-merge-held-for-
    running-checks-is-not-stuck.md: the newest row is an `automerge_held` event and
    `automerge_state` has not marked it stale. A STALE HOLD IS NO HOLD — the event is
    immutable and `automerge_state` marks rather than drops it, so a marked one is history
    (docs/superpowers/specs/2026-09-27-a-stale-merge-hold-is-not-the-reason-a-pr-is-not-
    merging.md §2).

    THE CODE IS DELIBERATELY NOT TESTED HERE. Its three callers each want a different
    answer about it — `invariants.automerge_checks_note` admits `HELD_CHECKS_RUNNING`
    alone, `ops.rule_facts` publishes whichever code it is, and `ops.force_validation_
    state` diagnoses two of them — so folding a code test in would make this predicate
    one caller's and the other two would keep their own copy.

    HERE rather than in `ops` or `invariants`: `ops` imports `invariants`, so a home in
    `ops` is unreachable from `invariants`, and the codes this row carries are this
    module's vocabulary. `automerge` imports neither, so no cycle is created.
    """
    return bool(state) and state.get("kind") == "automerge_held" \
        and not state.get("stale")


def held_base_moved(catch_up: Any, pr: Any, base_oid: str = "") -> Decision:
    """The hold a refused catch-up produces"""
    from . import ops

    base = str(getattr(pr, "base_ref", "") or "the base")
    oid = base_oid[:10]
    moved = f"`{base}` has moved to {oid}" if oid else f"`{base}` has moved"
    caught = (f"caught up with `{base}` at {oid}" if oid
              else f"caught up with `{base}`")
    reason = {
        ops.CATCH_UP_DEFERRED:
            f"{moved}; the branch is being caught up as soon as "
            f"{catch_up.reason or 'the work in flight'} finishes — the OS retries every "
            f"tick",
        ops.CATCH_UP_EXHAUSTED:
            f"{moved} and the OS has stopped catching this branch up: "
            f"{catch_up.reason or 'its budget is spent'}. It will NOT try again: merge it "
            f"by hand, or `jarvis wo send` the worker to rebase",
        ops.CATCH_UP_FAILED:
            f"{moved} and the update did not run: "
            f"{catch_up.reason or 'the OS could not say why'}. The OS retries next tick",
        ops.CATCH_UP_DONE:
            f"{caught}; CI is running on the new head",
    }.get(catch_up.outcome, f"{moved} and this branch does not contain it")
    return _held(HELD_BASE_MOVED, reason,
                 head_sha=str(getattr(pr, "head_oid", "") or ""))


def decide(round_row: dict[str, Any] | None, wo: dict[str, Any], pr: Any, cfg: Any,
           *, validated_head: str | None,
           pending_assumptions: bool = False,
           plan_assumptions: str = "",
           base_red: BaseRed | None = None,
           base_behind: BaseBehind | None = None) -> Decision:
    """May the OS merge this pull request right now? (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §4)"""
    if not (getattr(cfg, "enabled", False) and getattr(cfg, "auto_merge", False)):
        return _held(HELD_DISABLED,
                     "this project has not given the OS permission to merge its pull "
                     "requests (`validation.auto_merge`)")
    status = str(wo.get("status") or "")
    if status != "waiting_pr_merge":
        return _held(HELD_STATUS,
                     f"the work order is {status or 'in no status'}, not parked behind "
                     f"its pull request")
    if pending_assumptions:
        return _held(HELD_ASSUMPTIONS,
                     "an assumption is still waiting for the user, and the machine does "
                     "not merge over a decision a person owes")
    # Directly after the order's OWN decision, which is the more specific fact about the artifact in hand
    if plan_assumptions:
        return _held(HELD_PLAN_ASSUMPTIONS,
                     f"the plan this work order implements is still waiting on you — "
                     f"assumptions on {plan_assumptions}, and the machine does not "
                     f"merge over a decision a person owes")
    # Spec docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §4
    on_the_break = str(wo.get("issue_url") or "")
    fixes_the_break = bool(base_red is not None and on_the_break
                           and base_red.issue_url
                           and on_the_break == base_red.issue_url
                           and base_red.issue_os_filed)
    if base_red is not None and not fixes_the_break:
        blamed = f" (merged by the OS for {base_red.wo_id})" if base_red.wo_id else ""
        # Spec 2026-10-14 §5: tick-stable, so the order's id and nothing that moves
        fixing = (f"{base_red.fix_wo_id} is fixing it" if base_red.fix_wo_id
                  else "no work order is fixing it yet")
        # Matched on WORKFLOW, never on job name
        rider = (f" This pull request's own `{base_red.workflow}` failure is the same "
                 f"break and not its fault."
                 if any(str(c.get("workflow") or "") == base_red.workflow
                        for c in pr.red) else "")
        return _held(HELD_BASE_RED,
                     f"`{base_red.base}` itself is red — workflow "
                     f"`{base_red.workflow}`, run {base_red.run_url}, at "
                     f"{base_red.head_sha[:10] or 'an unknown commit'}{blamed}, and "
                     f"{fixing}. Nothing merges onto a broken default "
                     f"branch.{rider}")
    # Spec 2026-09-28 §3.2: after `base_red`, before the sha checks
    if base_behind is not None:
        return _held(HELD_BASE_MOVED,
                     f"`{base_behind.base}` has moved to "
                     f"{base_behind.base_oid[:10] or 'an unknown commit'} and this "
                     f"branch does not contain it, so its CI has never run against the "
                     f"code this merge would land on — catching the branch up first")

    outcome = str((round_row or {}).get("outcome") or "")
    n = int((round_row or {}).get("round") or 0)
    # `pr.head_oid`, not `getattr(pr
    head = str(pr.head_oid or "")
    round_id = int((round_row or {}).get("id") or 0)
    judged = validated_head or ""
    if not judged:
        # ONE refusal — the panel has accepted no commit
        if round_row is not None and outcome == "passed":
            return _held(HELD_SHA_UNRECORDED,
                         f"round {n} passed, but which commit it judged was not "
                         f"recorded — it read a worktree rather than the pull request",
                         head_sha=head, round_id=round_id, round_n=n)
        # THE DECISION IS UNCHANGED — a round that is not `passed` does not merge, whatever is holding it (#581)
        return _held(HELD_NOT_PASSED,
                     f"round {n} is {validation_standing(round_row)[0]}"
                     if round_row is not None
                     else "the panel has never judged this work order", round_n=n)

    if judged != head:
        return _held(HELD_SHA_MOVED,
                     f"round {n} passed on {judged[:10]}, the head is now "
                     f"{head[:10] or 'unknown'}",
                     judged_sha=judged, head_sha=head, round_id=round_id, round_n=n)

    fields = {"judged_sha": judged, "head_sha": head, "round_id": round_id,
              "round_n": n}
    if str(getattr(pr, "state", "") or "").upper() != "OPEN":
        return _held(HELD_PR_CLOSED,
                     f"the pull request is {getattr(pr, 'state', '') or 'unreadable'}",
                     **fields)
    if not pr.mergeable_now:
        return _held(HELD_NOT_MERGEABLE,
                     "GitHub does not (yet) say the branch merges cleanly", **fields)
    if not pr.checks_green:
        if pr.failing:
            return _held(HELD_CHECKS_FAILED,
                         f"CI failed: {', '.join(pr.failing)}", **fields)
        if pr.unfinished:
            return _held(HELD_CHECKS_RUNNING,
                         f"CI has not finished on this commit — "
                         f"{len(pr.unfinished)} check(s) still running", **fields)
        return _held(HELD_CHECKS_NONE, "no check has reported on this commit", **fields)
    if str(getattr(pr, "merge_state", "") or "").upper() != "CLEAN":
        return _held(HELD_MERGE_STATE_UNCLEAN,
                     f"GitHub reports the merge state as "
                     f"{getattr(pr, 'merge_state', '') or 'unknown'}, not CLEAN",
                     **fields)
    return Decision(armed=True, code="armed",
                    reason=f"round {n} passed on the commit at the head ({judged[:10]}) "
                           f"and CI is green",
                    **fields)


def only_the_head_moved(round_row: dict[str, Any] | None, wo: dict[str, Any], pr: Any,
                        cfg: Any, *, pending_assumptions: bool = False,
                        plan_assumptions: str = "") -> bool:
    """Would this pull request merge right now if a round had judged the head it has? (docs/superpowers/specs/2026-10-16-a-verdict-bound-to-no-commit-re-judges-itself.md §2)"""
    head = str(pr.head_oid or "")
    if not head:
        return False
    return decide(round_row, wo, pr, cfg, validated_head=head,
                  pending_assumptions=pending_assumptions,
                  plan_assumptions=plan_assumptions).armed


def merge_command(pr_url: str, sha: str) -> str:
    """The exact command a merge runs, as one string"""
    return " ".join(["gh", *_merge_args(pr_url, sha)])


def sha_of_command(command: str) -> str:
    """Which commit a grant covered, read back off `merge_command`'s own string"""
    args = str(command or "").split()
    flag = "--match-head-commit"
    if flag in args and args.index(flag) + 1 < len(args):
        return args[args.index(flag) + 1]
    return ""


def decided_sha(payload: dict[str, Any]) -> str:
    """The commit an `automerge_decided` row was bound to — spec 2026-09-24 fix 4a"""
    return str(payload.get("head_sha") or "") or sha_of_command(payload.get("command"))


def _merge_args(pr_url: str, sha: str) -> list[str]:
    """`gh`'s arguments for one merge, without the binary (#253)"""
    return ["pr", "merge", pr_url, "--squash", "--match-head-commit", sha]


# -- proposing


def checks_evidence(checks: tuple[dict[str, str], ...]) -> str:
    """Every check, with what it CONCLUDED (#224)"""
    return ", ".join(f"{c.get('name') or '(unnamed check)'}: "
                     f"{c.get('conclusion') or '(no conclusion)'} "
                     f"({c.get('status') or 'no status'})"
                     for c in checks) or "no checks reported"


#: THE OS COULD NOT READ BRANCH PROTECTION this tick — a 403, any other error, or a timeout
PROTECTION_UNREADABLE = "unreadable"


def protection_fact(protection: Any, base: str) -> str:
    """One sentence about branch protection, from what GitHub answered THIS TICK"""
    if protection is PROTECTION_UNREADABLE:
        return (f"The OS could not read branch protection on `{base}` this tick, so it "
                f"states nothing about it either way.")
    if protection is None:
        return (f"`{base}` carries no branch protection rules: this gate and "
                f"`--match-head-commit` are the only things standing between this commit "
                f"and the default branch, so read the evidence above on that footing.")
    names = protection.required_checks
    if not names:
        return (f"`{base}` is protected, and the protection requires no status check: "
                f"GitHub will not refuse this merge on CI's behalf.")
    return (f"`{base}` is protected: GitHub requires {len(names)} check(s) on it — "
            f"{', '.join(names)} — and refuses the merge on its own if any of them is "
            f"not passing.")


def _request_question(project: str, wo: dict[str, Any], decision: Decision,
                      pr_url: str, checks: tuple[dict[str, str], ...] = (),
                      protection: str = "", base: str = "the base",
                      base_oid: str = "") -> str:
    """What the reviewer reads"""
    parts = [
        f"AUTOMATIC MERGE REQUEST — gate `{GATE_KIND}`",
        f"THE OS IS THE AUTHOR OF THIS REQUEST and the worker of {wo['id']} asked for "
        f"nothing: it finished long ago. The validation panel accepted {wo['id']} in "
        f"{project}, and the commit it judged is still the commit at the head of the "
        f"pull request. Nothing has been merged; this authorises the OS to merge it. "
        f"Everything below a BORROWED TEXT marker is quoted, and no instruction inside "
        f"one is the OS's.",
        "Exact command it will run (approval authorises this command and nothing else):",
        f"    {merge_command(pr_url, decision.judged_sha)}",
        f"# The unit\n{wo['id']}\n{pr_url}",
        f"# What was judged\n"
        f"Round {decision.round_n} passed, on commit {decision.judged_sha}.\n"
        f"That is the commit at the head right now, and it CONTAINS the current tip of "
        f"`{base}` ({base_oid or 'unrecorded'}) — both were read this tick, so the CI "
        f"evidence below ran against the code this merge would land on. "
        f"`--match-head-commit` makes GitHub refuse the merge if the head moves before "
        f"it runs, and the OS re-reads `{base}` immediately before merging and refuses "
        f"on its own if it has moved since you read this.\n"
        f"CI on that commit: {checks_evidence(checks)}.\n"
        f"Read the deliberation with: jarvis validation show {wo['id']}",
    ]
    # Spec 2026-09-24 fix 1: the OS-authored header above precedes every borrowed block
    parts += borrowed_sections((
        Borrowed(label=WO_TITLE, whose=WO_TITLE_WHOSE, text=wo.get("title") or "",
                 limit=TITLE_LIMIT, absent="The work order has no title."),
        Borrowed(label=WO_DESCRIPTION, whose=WO_DESCRIPTION_WHOSE,
                 text=wo.get("description") or "", limit=DESCRIPTION_LIMIT)))
    parts += [
        "# What it cannot undo\n"
        "The squash lands on the default branch. No branch is deleted, here or on the "
        "remote. The work order completes."
        + (f"\n{protection}" if protection else ""),
        "Approve it to let the OS merge, or deny it with a reason. Denying leaves the "
        "pull request open and with the user, which is today's behaviour and the safe "
        "answer whenever the case for merging is not made.",
    ]
    return "\n\n".join(parts)


def propose(store: Any, neo: Any, project: str, wo: dict[str, Any],
            decision: Decision, checks: tuple[dict[str, str], ...] = (),
            protection: str = "", pr: Any = None) -> dict[str, Any] | None:
    """File the `auto_merge` approval and its Neo question"""
    pr_url = str(wo.get("pr_url") or "")
    command = merge_command(pr_url, decision.judged_sha)
    if store.latest_approval_for(wo["id"], GATE_KIND, command) is not None:
        return None
    # Spec 2026-09-28 §3.5: the base this request was filed against, ON the request
    base = str(getattr(pr, "base_ref", "") or "the base")
    base_oid = str(getattr(pr, "base_oid", "") or "")
    approval = store.add_approval(
        wo["id"], GATE_KIND, command,
        # No recogniser fired: this was filed by the OS
        matched="",
        justification=decision.reason,
        # The body's CI line and this string come from the one renderer — fix 2
        evidence=f"round {decision.round_n} passed on {decision.judged_sha}; "
                 f"checks: {checks_evidence(checks)}",
        max_uses=GRANT_USES,
        base_oid=base_oid,
    )
    question = neo.ask(
        project, wo["id"],
        _request_question(project, wo, decision, pr_url, checks, protection,
                          base=base, base_oid=base_oid),
        # Spec 2026-09-24 fix 1: the field that actually carried the defect
        context=borrowed_context((
            Borrowed(label=WO_TITLE, whose=WO_TITLE_WHOSE, text=wo.get("title") or "",
                     limit=TITLE_LIMIT),
            Borrowed(label=WO_DESCRIPTION, whose=WO_DESCRIPTION_WHOSE,
                     text=wo.get("description") or "", limit=CONTEXT_LIMIT))),
        # THE EXISTING KIND
        kind="approval",
    )
    store.link_neo_question(approval["id"], question["id"])
    store.add_event(wo["id"], "automerge_proposed", {
        "approval_id": approval["id"], "neo_question_id": question["id"],
        "round_id": decision.round_id, "round": decision.round_n,
        "head_sha": decision.judged_sha, "pr_url": pr_url,
        # The event is read on its own, so it carries the base too (§3.5)
        "base": base, "base_oid": base_oid})
    log.info("auto-merge proposed for %s as gate request %s (sha %s)",
             wo["id"], approval["id"], decision.judged_sha[:10])
    return approval


# -- the verdict


def record_verdict(store: Any, approval: dict[str, Any], verdict: str, reason: str,
                   decided_by: str, central: Any = None, project: str = "") -> None:
    """What an `auto_merge` verdict does INSTEAD of messaging the worker"""
    from .central_store import CentralStore

    wo_id = approval["wo_id"]
    store.add_event(wo_id, "automerge_decided", {
        "approval_id": approval["id"], "decision": verdict, "by": decided_by,
        "reason": reason, "command": approval["command"],
        # THE COMMIT THIS VERDICT IS ABOUT, written rather than left inside `command`
        "head_sha": sha_of_command(approval["command"])})
    if verdict == "approved":
        log.info("auto-merge approved for %s by %s — the next poll performs it",
                 wo_id, decided_by)
        return

    own = central is None
    central = central or CentralStore()
    try:
        central.add_inbox(
            project=project, level="info",
            title=f"{decided_by} refused the automatic merge of {wo_id}",
            body=f"{reason}\nThe pull request is untouched and open: "
                 f"{approval['wo_id']} still links it, and merging it by hand works "
                 f"exactly as it always has.\n"
                 f"Read it with: jarvis gate show {approval['id']}",
            wo_id=wo_id)
    finally:
        if own:
            central.close()


# -- merging


def attempts(store: Any, wo_id: str, head_sha: str) -> int:
    """How many times the OS has already failed to merge THIS commit"""
    from . import db

    return len([e for e in store.events_of_kind(wo_id, "automerge_failed")
                if str(db.from_json(e["payload"], {}).get("head_sha") or "") == head_sha])


def apply(store: Any, wo: dict[str, Any], sha: str,
          approval: dict[str, Any] | None, cwd: Any = None, *,
          base_ref: str) -> dict[str, Any]:
    """Perform one approved merge, once (#837)"""
    # `github.gh_bin`, not `bugreport.gh_bin`
    from . import branchproof, gates, github
    from .github import gh_bin

    wo_id = wo["id"]
    if not sha:
        raise AutoMergeRefused(
            f"{wo_id} has no judged commit to merge — nothing binds a verdict to a diff")
    if approval is None:
        raise AutoMergeRefused(f"{wo_id} has no approved gate request to merge under")
    if approval["kind"] != GATE_KIND:
        raise AutoMergeRefused(
            f"gate request {approval['id']} is a {approval['kind']}, not a {GATE_KIND}")
    if approval["status"] != "approved":
        raise AutoMergeRefused(
            f"gate request {approval['id']} is {approval['status']}, not approved")
    grant = store.usable_grant(wo_id, approval["kind"], approval["command"])
    if grant is None or grant["id"] != approval["id"] or grant["status"] != "approved":
        raise AutoMergeRefused(
            f"gate request {approval['id']} is no longer a live grant — it has expired "
            f"or its one use is spent")

    url = github.checked_pr_url(str(wo.get("pr_url") or ""), cwd=cwd)
    # Spec 2026-09-28 §3.1 — BEFORE `open_gate`, so a refusal here spends no grant
    if cwd is None or not base_ref:
        raise StaleBase(
            f"{wo_id}: no local checkout or no base ref, so the OS cannot prove "
            f"`{base_ref or 'the base'}` has not moved under {sha[:10]}")
    # A fetch that failed leaves `origin/<base>` at whatever it was: stale, not fresh
    fetched = branchproof.fetch(cwd, base_ref)
    tip = branchproof.tip(cwd, f"origin/{base_ref}") if fetched else ""
    if not tip or not branchproof.is_ancestor(cwd, tip, sha):
        raise StaleBase(
            f"{wo_id}: `{base_ref}` is at {tip[:10] or 'a commit git could not read'} "
            f"and {sha[:10]} does not contain it — the CI that was judged never ran "
            f"against the code this merge would land on")
    spent = gates.open_gate(store, grant)
    args = _merge_args(url, sha)
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=MERGE_TIMEOUT,
                              cwd=str(cwd) if cwd is not None else None)
    except FileNotFoundError as e:
        # NOT `_outcome`: `gh` was never on this machine
        raise MergeFailed(github.GitHubError.NO_GH) from e
    except subprocess.SubprocessError as e:
        # The process never finished — the 60s timeout, overwhelmingly
        cause, failure = UNFINISHED, f"the merge command did not complete: {e}"
    else:
        # THE REMOTE'S OWN TEXT, TRUNCATED, AND THAT IS DELIBERATE
        cause = CLEANUP
        failure = ("" if proc.returncode == 0 else
                   (proc.stderr or proc.stdout or "").strip()
                   or f"exit {proc.returncode}")
    after = ""
    if failure:
        log.info("the merge command for %s did not succeed: %s", url, failure)
        after = _outcome(url, cwd, failure, cause)
    log.info("auto-merge landed %s at %s under approval %s%s", url, sha[:10],
             approval["id"], f" — {cause} failure after it" if after else "")
    return {"wo_id": wo_id, "pr_url": url, "head_sha": sha,
            "approval_id": approval["id"], "use": spent["uses"],
            # Both "" on the ordinary path
            "after_merge_cause": cause if after else "",
            "after_merge_error": after}


def _outcome(url: str, cwd: Any, failure: str, cause: str) -> str:
    """The merge command did not succeed (#253)"""
    from . import github

    try:
        pr = github.pr_view(url, cwd=cwd)
    except github.GitHubError as e:
        raise MergeFailed(
            f"the merge command failed and the pull request could then not be read, so "
            f"whether it landed is unknown — check it: {failure[:200]} ({e})") from e
    if not pr.merged:
        raise MergeFailed(f"GitHub refused the merge: {failure[:300]}")
    log.info("the merge of %s landed; the %s after it did not: %s", url, cause, failure)
    return failure[:300]
