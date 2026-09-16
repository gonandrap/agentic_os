"""The one place in the OS that writes to GitHub: merging a pull request the panel read.

docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md. The user hand-merges
every pull request the fleet produces, and GitHub's own auto-merge cannot take that over:
it fires on branch protection alone, so it would land a submission the validation panel
never read. On this repository CI runs the tests the submitter wrote and the panel is the
reviewer — so merging on CI alone deletes the review and keeps the latency.

This module is the other answer. Repository auto-merge stays OFF, `protect-main` is not
edited, no bypass actor is added, and the human merge is therefore untouched on every
pull request in the repository: the mechanism adds a way for the MACHINE to do what the
person was already doing and subtracts nothing from the person.

Six positive facts have to line up before a byte lands, and any one missing is a hold
(`decide`). The two that carry the weight:

* **the commit.** A verdict is a judgement about one diff, and GitHub merges whatever is
  at the head ref at merge time. `validation_rounds.head_sha` records which commit the
  seats were shown, `decide` refuses when the live head is not it, and the merge itself
  runs `--match-head-commit <that sha>` so GITHUB refuses server-side if the head moved
  in the milliseconds after the poll looked. Three things must agree — the stored sha,
  the head at view time, the head at merge time — and any disagreement merges nothing.
  This matters here rather than in theory: `Daemon.heal_pull_request` exists precisely to
  make workers push to parked branches, and GitHub does not disarm its own auto-merge on
  a push from anyone with write permission.
* **the authority.** Every merge is a mandatory `auto_merge` gate request reviewed by
  Neo, escalable to the user, recorded in the approvals ledger. The capability ships off
  and is granted one project at a time (`ValidationConfig.auto_merge`).

**WHY THIS IS NOT IN `github.py`.** That module's whole claim is that everything in it is
a question — `READ_ONLY_VERBS` plus an AST walk over it — and the panel's blind review
rests on that claim: a seat that could write to GitHub could talk to the implementor it
is judging (Neo, question 251). `bugreport.create_issue` is the precedent for a write
living outside it. `WRITE_VERBS` below holds this module to the same standard from the
other side: one verb pair, and a test walks this file to prove nothing else is built.

Every failure direction ends in "nothing merged", and that falls out of the structure
rather than out of an exception handler someone remembered to write: the merge needs six
positive facts, so a daemon that is down, a `gh` that cannot authenticate, a panel that
died silently on a usage limit (issue #235) and a round nobody judged are all simply
missing facts. The visible cost of every one of them is that the pull request sits there
waiting for a human, which is exactly today's behaviour.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: The gate every merge here rides. `gate_rules.AUTO_MERGE`, spelled through the import
#: below at its call sites; named as a module constant for `remedies.GATE_KIND`'s reason.
GATE_KIND = "auto_merge"

#: EVERY `gh` SUBCOMMAND THIS MODULE MAY RUN, as the (verb, subverb) pair that opens the
#: argument list — `github.READ_ONLY_VERBS`' mechanism pointed the other way, at the one
#: module allowed to write. `tests/test_automerge.py` walks this file's AST and holds
#: every `gh` argument list it builds against this set, so a second write verb cannot
#: arrive without a commit that also edits the test.
WRITE_VERBS = (("pr", "merge"),)

#: A grant covers ONE merge. Not a threshold — the shape of the permission: the reviewer
#: authorised landing ONE commit, and a retry after a failure needs a fresh review rather
#: than a free second attempt at an irreversible act. `remedies.GRANT_USES`' reasoning.
GRANT_USES = 1

#: A merge is one API round trip. Longer than `github.GH_TIMEOUT` because GitHub computes
#: the squash on the way, and the daemon must still never block its tick on one.
MERGE_TIMEOUT = 60

#: Why a hold was recorded, as a stable token. The dedupe in `Daemon._note_automerge_held`
#: keys on (head sha, this, the reason's text), so one pull request records each distinct
#: reason once per commit instead of every two minutes — and a hold that CHANGES (checks
#: were still running, then the branch stopped merging cleanly) is still recorded.
#:
#: ONE CODE PER CONDITION, never one shared by several: kn-0aba30f0's rule, and issue #263
#: is what breaks without it. Four conditions used to return `pr_not_ready`, so the second
#: one to hold a commit was deduped away as a repeat of the first and the user was sent to
#: look at CI for a merge conflict. The reason's text is in the key as well because a code
#: is coarser than a sentence — `BEHIND` and `DIRTY` are one code and two different things
#: to do about it.
HELD_DISABLED = "disabled"
HELD_STATUS = "status"
HELD_ASSUMPTIONS = "assumptions"
HELD_NOT_PASSED = "not_passed"
HELD_SHA_UNRECORDED = "sha_unrecorded"
HELD_SHA_MOVED = "sha_moved"
HELD_PR_CLOSED = "pr_closed"
HELD_NOT_MERGEABLE = "not_mergeable"
HELD_CHECKS_NOT_GREEN = "checks_not_green"
HELD_MERGE_STATE_UNCLEAN = "merge_state_unclean"

#: WHY the merge command did not succeed on a pull request that merged anyway (§5.5).
#: Two causes, never folded into one: a command that RAN and exited non-zero is a local
#: tidy-up that failed, and a command that NEVER FINISHED is a timeout that may have
#: expired after GitHub had already merged. Calling the second one a cleanup failure
#: sends a reader hunting a branch deletion that was never attempted.
CLEANUP = "cleanup"
UNFINISHED = "unfinished"

#: The event each cause writes. Separate kinds rather than one kind with a field,
#: because `timeline._describe` keys on the kind and the two want different sentences.
AFTER_MERGE_EVENT = {CLEANUP: "automerge_cleanup_failed",
                     UNFINISHED: "automerge_command_unfinished"}


class AutoMergeRefused(Exception):
    """NOTHING WAS ATTEMPTED. Raised by `apply` for every reason a merge must not run, so
    a caller cannot mistake a refusal for a merge that quietly did nothing.

    Not news, and not counted: the commonest instance by far is a grant that has already
    been spent, which is the ordinary state of every commit whose one authorised attempt
    has been made. See `MergeFailed` for the other half — and why the two must not be one
    exception.
    """


class MergeFailed(AutoMergeRefused):
    """The merge RAN and GitHub refused it. A fact about the pull request, not about us.

    Separate from its parent because the daemon counts these and reports them, and
    folding the two together made the record lie in a way that only showed up under test:
    `GRANT_USES` is 1, so the first failed attempt spends the grant and every later poll
    refuses before reaching GitHub. Counting those as merge failures wrote two extra
    `automerge_failed` events per commit whose reason was "the grant is spent" — and the
    user's inbox row then quoted that as the reason the merge failed, instead of the 403
    that actually caused it.
    """


@dataclass(frozen=True)
class Decision:
    """Armed, or held with a reason a person can read. Nothing else.

    `reason` is written for `jarvis wo show` and the dashboard, so it says what is true
    of THIS pull request rather than naming a predicate: "round 2 passed on a1b2c3d, the
    head is now e4f5a6b" tells the user what to do about it, and `head_sha mismatch` does
    not.
    """

    armed: bool
    code: str
    reason: str
    #: The commit the panel judged, when there is one. `""` otherwise, and that empty
    #: string never reaches a merge command — `apply` refuses without a sha.
    judged_sha: str = ""
    #: The commit at the head of the pull request as this tick's `gh pr view` saw it.
    head_sha: str = ""
    round_id: int = 0
    round_n: int = 0


def _held(code: str, reason: str, **fields: Any) -> Decision:
    return Decision(armed=False, code=code, reason=reason, **fields)


def decide(round_row: dict[str, Any] | None, wo: dict[str, Any], pr: Any, cfg: Any,
           *, validated_head: str | None,
           pending_assumptions: bool = False) -> Decision:
    """May the OS merge this pull request right now? PURE — no store, no clock, no `gh`.

    Dicts and one `github.PullRequest` in, armed-or-held-with-a-reason out. Pure for
    `arbitrate`'s reason: the whole condition table is then unit-testable without a
    network, and the safety rule lives in one function rather than in a sequence of `if`s
    spread through a 180-line daemon method. `pr` arrives as the dataclass rather than a
    dict so that `checks_green` keeps its single reader — re-deriving "is CI green" over a
    payload here is exactly how the OS would come to judge a submission by a standard it
    does not police while it waits (issue #224).

    The six conditions, all of which must hold, cheapest and most-specific first:

    1. the project has opted in AND the panel is on (`cfg.auto_merge and cfg.enabled`);
    2. the work order is parked in `waiting_pr_merge`;
    3. it owes the user no assumption decision;
    4. `ProjectStore.validated_head` yields a commit — which is one fact, not two: the
       latest round settled `passed` AND it recorded which commit it judged;
    5. that commit IS the live head;
    6. GitHub says the pull request is open, mergeable, green and CLEAN.

    **`validated_head` IS PASSED IN AND NEVER RE-DERIVED HERE.** Conditions 4 and 5 rest
    on "which commit did the panel accept", and that question has exactly one home
    (`ProjectStore.validated_head`, spec §5.2) for `arbitrate`'s reason: a rule spread
    over two call sites is a rule that holds by luck, and the copy that is not the one
    actually running is the copy that rots. `round_row` is still read below, but ONLY to
    write the sentence a person reads — "round 2 is rejected" against "round 2 passed but
    read a worktree" are the same refusal with different advice — and never to decide.
    The two must be the SAME row: `Daemon.auto_merge` reads it once and derives both, so
    that the validator opening a round on its own thread cannot hand this function a
    predicate and a wording taken a microsecond apart.

    Condition 3 is redundant with condition 2 — `ops.land_when_cleared` cannot reach
    `waiting_pr_merge` with an assumption pending — and it is re-checked because the
    redundancy is the point: the two are independent judgements over one artifact and
    neither may be inferred from the other
    (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md).

    Condition 6 requires `CLEAN` and not merely "not failing". `UNSTABLE` (mergeable with
    a non-required check red) and `BLOCKED` (a requirement outstanding) both hold, and
    `UNKNOWN` — GitHub has not computed mergeability yet — holds and retries next tick.
    `CLEAN` is the ordinary state for this repository *because* the user left
    `strict_required_status_checks_policy` off; with it on, a branch that is merely behind
    reports `BEHIND` and this would essentially never arm.
    """
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

    outcome = str((round_row or {}).get("outcome") or "")
    n = int((round_row or {}).get("round") or 0)
    # `pr.head_oid`, not `getattr(pr, ..., "")`: a default would turn a renamed or
    # missing field into "" — which compares unequal to every judged commit and holds
    # this feature off for ever, silently. Let it raise.
    head = str(pr.head_oid or "")
    round_id = int((round_row or {}).get("id") or 0)
    judged = validated_head or ""
    if not judged:
        # ONE refusal — the panel has accepted no commit — told two ways, because the two
        # want different things from the reader. The round is read for the wording only;
        # `validated_head` above is what decided. `pending` and `failed` are the shapes a
        # panel that never answered leaves behind, and both must read as NOT VALIDATED
        # rather than as not refused.
        if round_row is not None and outcome == "passed":
            return _held(HELD_SHA_UNRECORDED,
                         f"round {n} passed, but which commit it judged was not "
                         f"recorded — it read a worktree rather than the pull request",
                         head_sha=head, round_id=round_id, round_n=n)
        return _held(HELD_NOT_PASSED,
                     f"round {n} is {outcome}" if round_row is not None
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
        return _held(HELD_CHECKS_NOT_GREEN,
                     "CI has not finished a unanimous pass on this commit", **fields)
    if str(getattr(pr, "merge_state", "") or "").upper() != "CLEAN":
        return _held(HELD_MERGE_STATE_UNCLEAN,
                     f"GitHub reports the merge state as "
                     f"{getattr(pr, 'merge_state', '') or 'unknown'}, not CLEAN",
                     **fields)
    return Decision(armed=True, code="armed",
                    reason=f"round {n} passed on the commit at the head ({judged[:10]}) "
                           f"and CI is green",
                    **fields)


def merge_command(pr_url: str, sha: str) -> str:
    """The exact command a merge runs, as one string. The thing a reviewer authorises.

    ONE renderer, because the string is the grant's whole scope: `usable_grant` matches
    it exactly, so a request filed with one spelling and a merge attempted with another
    is a merge that silently never happens. The judged sha is IN it, which also makes the
    grant naturally per-commit — an approval to land a1b2c3d cannot land anything else,
    and a denial for a1b2c3d does not have to be remembered separately to stop the OS
    asking about it again every two minutes.
    """
    return " ".join(["gh", *_merge_args(pr_url, sha)])


def _merge_args(pr_url: str, sha: str) -> list[str]:
    """`gh`'s arguments for one merge, without the binary. See `WRITE_VERBS`.

    `--match-head-commit` IS THE MECHANISM, not a belt-and-braces extra: it is the API's
    `sha` parameter — "commit SHA that the pull request head must match to allow merge" —
    so GitHub refuses at the server if the head moved after this tick looked. The
    comparison in `decide` exists so the OS can SAY why it is holding; this is what makes
    it true (spec §5.3).

    **NO `--delete-branch`, and spec §10.1's answer is REVERSED** (issue #253). That flag
    deletes the remote branch AND then the local one — the spec proposed it believing it
    did the first only — and the local delete fails whenever a worktree still has the
    branch checked out, which on this path is every time: nothing removes a worker's
    worktree, and the work order completes only after the merge. So it turned every OS
    merge into a command that half-failed. Deleting a developer's local branches was
    never this mechanism's business; the remote half belongs to the repository's own
    `delete_branch_on_merge` setting, which is one click and the owner's to make.
    """
    return ["pr", "merge", pr_url, "--squash", "--match-head-commit", sha]


# -- proposing -------------------------------------------------------------------------


def _request_question(project: str, wo: dict[str, Any], decision: Decision,
                      pr_url: str, checks: tuple[str, ...]) -> str:
    """What the reviewer reads. Not `gates.build_request_question`, for one reason.

    That renderer opens "The worker for work order X wants to …", and here the worker
    finished long ago and asked for nothing: the OS is asking. A reviewer told the wrong
    actor rules on the wrong question, and the request text is all it ever sees —
    `remedies._request_question` exists for the same reason one level along.
    """
    return "\n\n".join([
        f"AUTOMATIC MERGE REQUEST — gate `{GATE_KIND}`",
        f"The validation panel accepted {wo['id']} in {project}, and the commit it "
        f"judged is still the commit at the head of the pull request. Nothing has been "
        f"merged; this authorises the OS to merge it.",
        "Exact command it will run (approval authorises this command and nothing else):",
        f"    {merge_command(pr_url, decision.judged_sha)}",
        f"# The unit\n{wo.get('title') or '(untitled)'}\n{pr_url}",
        f"# What was judged\n"
        f"Round {decision.round_n} passed, on commit {decision.judged_sha}.\n"
        f"That is the commit at the head right now — the two were compared this tick, "
        f"and `--match-head-commit` makes GitHub refuse the merge if it moves before it "
        f"runs.\n"
        f"CI on that commit: {', '.join(checks) or 'no checks reported'}.\n"
        f"Read the deliberation with: jarvis validation show {wo['id']}",
        "# What it cannot undo\n"
        "The squash lands on the default branch. No branch is deleted, here or on the "
        "remote. The work order completes. Nothing here weakens branch protection: the "
        "five required "
        "checks still apply and GitHub refuses the merge on its own if they do not pass.",
        "Approve it to let the OS merge, or deny it with a reason. Denying leaves the "
        "pull request open and with the user, which is today's behaviour and the safe "
        "answer whenever the case for merging is not made.",
    ])


def propose(store: Any, neo: Any, project: str, wo: dict[str, Any],
            decision: Decision, checks: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """File the `auto_merge` approval and its Neo question. Returns the approval, or None.

    None means one already exists for this exact command — filed, decided or refused —
    and a second would be the OS asking every two minutes about a question somebody has
    already answered. Because `merge_command` carries the judged sha, "this exact command"
    is naturally "this exact commit": a denial stops the asking for the commit that was
    denied and not for the next one, which is what a reviewer refusing THIS diff means.

    NOT `gates.file_request`, and the difference is deliberate rather than incidental.
    That function re-statuses a `running`/`dispatching` work order to `waiting_input`; a
    work order here is `waiting_pr_merge`, so it would in fact do nothing — but relying on
    a status it happens not to match is how the next caller inherits a surprise, and the
    question renderer would still name the wrong actor (`_request_question`).
    """
    pr_url = str(wo.get("pr_url") or "")
    command = merge_command(pr_url, decision.judged_sha)
    if store.latest_approval_for(wo["id"], GATE_KIND, command) is not None:
        return None
    approval = store.add_approval(
        wo["id"], GATE_KIND, command,
        # No recogniser fired: this was filed by the OS, not matched out of a command
        # line, and `gates.learn_from_dismissal` has no pattern here to generalise.
        matched="",
        justification=decision.reason,
        evidence=f"round {decision.round_n} passed on {decision.judged_sha}; "
                 f"checks: {', '.join(checks) or 'none reported'}",
        max_uses=GRANT_USES,
    )
    question = neo.ask(
        project, wo["id"],
        _request_question(project, wo, decision, pr_url, checks),
        context=f"{wo.get('title') or ''}\n{(wo.get('description') or '')[:800]}",
        # THE EXISTING KIND. `Daemon._deliver_gate_verdict` looks an `approval` question's
        # subject up in `approvals`, which is where this row lives. A new kind without a
        # delivery arm falls through to `queue_message` and messages a worker that
        # finished long ago — the one act this gate is fenced against.
        kind="approval",
    )
    store.link_neo_question(approval["id"], question["id"])
    store.add_event(wo["id"], "automerge_proposed", {
        "approval_id": approval["id"], "neo_question_id": question["id"],
        "round_id": decision.round_id, "round": decision.round_n,
        "head_sha": decision.judged_sha, "pr_url": pr_url})
    log.info("auto-merge proposed for %s as gate request %s (sha %s)",
             wo["id"], approval["id"], decision.judged_sha[:10])
    return approval


# -- the verdict ------------------------------------------------------------------------


def record_verdict(store: Any, approval: dict[str, Any], verdict: str, reason: str,
                   decided_by: str, central: Any = None, project: str = "") -> None:
    """What an `auto_merge` verdict does INSTEAD of messaging the worker.

    **AN APPROVAL MERGES NOTHING HERE.** It records that permission exists and returns;
    the next pull-request poll finds the usable grant and merges then — which is what
    re-verifies the head sha AFTER the approval rather than before it. A grant lives an
    hour (`gates.GRANT_TTL_SECONDS`), so a verdict that merged inline would be merging on
    a view of the pull request that could be an hour old, and the reviewer's own
    `--match-head-commit` would be the only thing left standing between a stale decision
    and `main`. One safety net is not a design.

    A denial or a dismissal records and stops: the pull request stays open, the work order
    stays in `waiting_pr_merge` with its link, and the user merges it by hand exactly as
    they do today. Nothing is flagged for attention, because "a human merges this one" is
    not a problem — it is the behaviour this feature is an optimisation over.

    A DISMISSAL IS RECORDED AS THE MISTAKE IT IS. Nothing classifies into this kind, so
    `dismissed` here cannot mean "the recogniser was wrong"; it can only be a reviewer
    reaching for the wrong verb. `apply` refuses on anything but `approved` — a dismissal
    clears `ProjectStore.usable_grant`, and without that guard the one verdict that means
    "this was not a privileged action" would authorise the merge.
    """
    from .central_store import CentralStore

    wo_id = approval["wo_id"]
    store.add_event(wo_id, "automerge_decided", {
        "approval_id": approval["id"], "decision": verdict, "by": decided_by,
        "reason": reason, "command": approval["command"]})
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


# -- merging ----------------------------------------------------------------------------


def attempts(store: Any, wo_id: str, head_sha: str) -> int:
    """How many times the OS has already failed to merge THIS commit.

    Per head sha rather than per work order: a new commit is a new submission, judged
    afresh and authorised afresh, so what it has to answer is "have we already told the
    user about THIS one". Counted off the timeline rather than a column, `ops.PrRepair`'s
    way — the events are the record, and a counter would be a second one to keep in step.

    **NOT a retry budget, and spec §8's `AUTO_MERGE_MAX_ATTEMPTS = 3` is deliberately not
    implemented.** `GRANT_USES` is 1 and `propose` files at most one gate request per
    (work order, judged commit), so a commit gets exactly ONE authorised attempt and a cap
    of three could never bind — it would be a constant that reads like a guarantee and
    enforces nothing. The bound is the grant. This is the dedupe for
    `Daemon._warn_automerge_failed`, nothing more.
    """
    from . import db

    return len([e for e in store.events_of_kind(wo_id, "automerge_failed")
                if str(db.from_json(e["payload"], {}).get("head_sha") or "") == head_sha])


def apply(store: Any, wo: dict[str, Any], sha: str,
          approval: dict[str, Any] | None, cwd: Any = None) -> dict[str, Any]:
    """Perform one approved merge, once. Raises `AutoMergeRefused` if it may not.

    Four refusals before anything runs, and `AutoMergeRefused` rather than a falsy return
    for each: a caller that cannot tell "refused" from "merged nothing successfully" is a
    caller that will eventually complete a work order whose pull request is still open.

    **`status == "approved"` IS CHECKED EXPLICITLY**, not left to `usable_grant`. That
    function clears a command on TWO statuses and only one of them is an authorisation:
    `dismissed` means the recogniser matched something that performs no privileged action,
    and nothing classifies into this kind, so a dismissal here is a reviewer's slip. Taking
    it as permission would turn the one verdict that means "this was never privileged"
    into the thing that merges to `main`.

    The grant is spent through `gates.open_gate` and never by hand — one function spends
    every grant in the OS, and the alternative is a second place that knows how, which is
    how the two come to disagree (`remedies.apply`'s note). It is spent BEFORE the merge
    runs, which is the right way round: a permission is to ATTEMPT the act, and a retry
    after a failure needs a fresh review rather than a free second go at something
    irreversible.

    The URL is re-checked before it becomes an argument (`github.checked_pr_url`) even
    though the poll that got here already read it: it is written by the submitter
    (`jarvis wo finish --pr`), it reaches a command run with the operator's credentials,
    and this is the one command in the OS that can change a repository. `cwd` is the
    project directory, and passing it is what makes that check include "and it is on THIS
    project's own origin" rather than merely "it is shaped like a pull-request URL".
    """
    # `github.gh_bin`, not `bugreport.gh_bin` — the same function, reached through the
    # module that owns talking to GitHub. `JARVIS_GH_BIN` and the PATH story are
    # `github.py`'s contract, and a second import path is how a caller comes to resolve
    # the binary one way while the module it is imitating resolves it another.
    from . import gates, github
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
    spent = gates.open_gate(store, grant)
    args = _merge_args(url, sha)
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=MERGE_TIMEOUT,
                              cwd=str(cwd) if cwd is not None else None)
    except FileNotFoundError as e:
        # NOT `_outcome`: `gh` was never on this machine, so nothing reached GitHub and
        # there is no pull-request state that could have changed. The one failure whose
        # outcome is knowable without asking.
        raise MergeFailed(github.GitHubError.NO_GH) from e
    except subprocess.SubprocessError as e:
        # The process never finished — the 60s timeout, overwhelmingly. GitHub may well
        # have merged before it expired, so this goes through `_outcome` like any other
        # failure; what it must NOT do is arrive there as the same fact as an exit code.
        cause, failure = UNFINISHED, f"the merge command did not complete: {e}"
    else:
        # THE REMOTE'S OWN TEXT, TRUNCATED, AND THAT IS DELIBERATE — the opposite of
        # `github.GitHubError.reason`, which substitutes a fixed vocabulary because its
        # string is interpolated into five seat prompts and a judge's prompt is no place
        # for text a remote server chose. This string reaches a human: the timeline, and
        # one inbox row once the budget is spent. The likeliest failure here is a `gh`
        # with read credentials and no write scope, and "HTTP 403: Resource not
        # accessible" is the whole diagnosis — a fixed phrase would send the reader back
        # to the log for the only fact that matters. Full detail is logged either way.
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
            # Both "" on the ordinary path. Non-empty means the pull request MERGED and
            # the command that merged it did not succeed — `_outcome`. The CAUSE is
            # carried separately from the text because it picks the event kind and the
            # sentence, and the two causes are not the same fact.
            "after_merge_cause": cause if after else "",
            "after_merge_error": after}


def _outcome(url: str, cwd: Any, failure: str, cause: str) -> str:
    """The merge command did not succeed. Did the PULL REQUEST merge? Returns, or raises.

    THE EXIT CODE IS NOT THE OUTCOME (issue #253, spec §5.5). The command merges REMOTELY
    and then tidies up locally, so one process reports two acts through one status, and
    the local half can fail over something the remote never saw. Both live merges of
    0.10.0 landed
    on `main` and then exited non-zero because `--delete-branch` could not delete a local
    branch a worker's worktree still had checked out. The OS said "GitHub refused the
    merge" about a merge GitHub had accepted, wrote `automerge_failed` and an inbox row
    for it, and spent the one attempt `GRANT_USES` allows on an operation that had
    succeeded. Both work orders were rescued only by the separate pull-request poll,
    which made this mechanism correct by accident.

    So the merge is judged by the only authority on it — GitHub's own `state`, read
    through the module that owns reading it. Three answers, and the middle one is the
    point:

    * MERGED — it landed. The failure text comes back for the timeline, under `cause`.
      Not a `MergeFailed`: no `automerge_failed` event, no inbox row, and the work order
      completes. Neither cause needs a person.
    * anything else — GitHub really did refuse, which is the 403-shaped case `apply`'s
      comment above is written for.
    * unreadable — two `gh` calls failed and the OS does not know what happened, so it
      says that rather than choose. Claiming a merge that did not happen would complete
      a work order whose pull request is still open, which is the worse of the two
      errors, and the pull-request poll settles the case either way within a tick.

    **`cause` IS CARRIED THROUGH AND NEVER ASSUMED.** This function is reached by two
    roads and only one of them is a cleanup: a command that ran and exited non-zero
    (`CLEANUP`), and a command that never finished at all (`UNFINISHED` — the timeout).
    Labelling a timed-out merge "the cleanup after it failed" sends a reader hunting a
    branch deletion that was never attempted, so the two keep separate event kinds and
    separate sentences all the way to the timeline (`AFTER_MERGE_EVENT`).
    """
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
