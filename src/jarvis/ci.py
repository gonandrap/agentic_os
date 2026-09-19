"""A pull request red only because its base was: recognising it, and healing it.

`github.py` answers "what does this pull request say". This module answers the question
that one cannot: **was the pull request ever asked a fair question?** GitHub builds a
pull request as a merge with its base, so while the base's head is red every pull
request in the fleet builds red — and GitHub never rebuilds that merge ref when the base
moves, so the stale FAILURE conclusion stays on the pull request for ever. No push to
the branch turns it green and no worker can fix it: wo-43c4c665 and wo-b2f88616 each
burned three worker turns proving exactly that, 2026-09-18.

docs/superpowers/specs/2026-09-18-a-red-base-heals-itself.md.

**RE-RUNNING IS NOT THE HEAL, AND THE MEASUREMENT SAYS SO.** A re-run replays the same
merge commit — run 35302313324 has headSha 07a05eb, the merge of the branch with main as
it was when the run was created, a main that was red — so it is guaranteed to reproduce
the inherited failure for ever at full CI cost. Both re-runs the user tried on 2026-09-18
came back red with the identical TypeError. Only UPDATING THE BRANCH regenerates the
merge ref against the base as it is now, which is why the one write here is
`update_branch` and not a re-run.

**THIS MODULE IS SEPARATE FROM `github.py` BECAUSE IT WRITES.** That module's read-only
property is load-bearing rather than tidy: Neo (question 251) put the panel's fetch there
precisely so that "a judging seat cannot write to GitHub" is a property of the code, and
`tests/test_github_artifact.py` walks its AST to prove it. A write added there would
spend that guarantee to save a file. So the write lives here, under its own allowlist and
its own AST test, and the OS's whole GitHub write surface is two commands in two modules:
the merge in `automerge` (gated, irreversible, lands code on the default branch) and this
one (not gated — see `update_branch`).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .bugreport import gh_bin, gh_missing_message
from .github import PR_URL_RE, GhUnavailable, GitHubError, checked_pr_url

log = logging.getLogger("jarvis.ci")

#: Same ceiling as `github.GH_TIMEOUT`, and separate from it deliberately: these calls
#: share the daemon's tick and must not block it, but they answer a different question,
#: and a future need to give the update longer must not silently lengthen the poll.
CI_TIMEOUT = 30

#: Every `gh` subcommand this module may run. `("pr", "update-branch")` IS THE WRITE and
#: is the only one — `tests/test_base_heal.py` walks this module's AST against this
#: tuple, the way `tests/test_github_artifact.py` does for the read-only module. A second
#: write verb has to be added in a commit that also changes that test.
#:
#: `("api", "--method")` IS THAT SHAPE ON PURPOSE. `gh api` is the one verb here whose
#: name does not say what it does — the same string reads a commit or opens a pull
#: request, depending on a flag. So the method is pinned as the SECOND ARGUMENT, which
#: makes the allowlist entry itself say the call is a GET and lets the AST test assert
#: it: an `api` call built any other way is not in this tuple and fails the test.
VERBS = (("run", "list"), ("api", "--method"), ("pr", "update-branch"))

#: Of those, the ones that change something at GitHub. Named apart so the claim a reader
#: wants to check — "the OS updates a branch and does nothing else here" — is one tuple
#: rather than the difference of two.
WRITE_VERBS = (("pr", "update-branch"),)

#: What `gh run list --json` is asked for. `headSha` is what bounds the heal (one update
#: per base sha); `workflowName` is what pairs a base run with a pull request's check;
#: the rest is the temporal signal.
RUN_FIELDS = "databaseId,headSha,conclusion,status,startedAt,workflowName"

#: How far back the base's history is read. A check older than this many base runs is one
#: this heal cannot judge, and it falls through to the worker nudge — the safe direction,
#: and exactly today's behaviour. Twenty is about a day of `main` on the fleet's busiest
#: project and one page of `gh run list`.
BASE_RUN_LIMIT = 20

#: A `conclusion` on a WORKFLOW RUN that means the base itself was broken. Lower case
#: because `gh run list` answers lower case where `statusCheckRollup` answers upper — one
#: CLI spelling one fact two ways, normalised at this boundary rather than at every
#: comparison. Deliberately NOT `github.RED_CONCLUSIONS` applied to a different shape:
#: `cancelled` on a base run means a human stopped it, which says nothing about the code
#: and must never make a pull request look inherited.
RED_RUN_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

#: The shape a branch name must have before it becomes an argument. `github.PR_URL_RE`'s
#: reasoning one field along: `baseRefName` comes back from GitHub about a pull request
#: whose URL a WORKER wrote, and a name beginning with `-` sits in an argument list where
#: `gh` would read it as a flag.
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: The same rule for a commit, one field along again: `headRefOid` is GitHub's answer
#: about a pull request a worker named, and it reaches an API path below.
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Run:
    """One workflow run on the BASE branch. `gh run list`, normalised."""

    run_id: int
    head_sha: str
    #: Lower case, as `gh run list` answers it. Empty while the run is unfinished.
    conclusion: str
    #: `queued`, `in_progress` or `completed`.
    status: str
    #: Epoch seconds, or None when GitHub gave no parseable time.
    started_at: float | None
    workflow: str

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def red(self) -> bool:
        return self.conclusion in RED_RUN_CONCLUSIONS

    @property
    def green(self) -> bool:
        return self.conclusion == "success"


def parse_ts(value: str | None) -> float | None:
    """An ISO-8601 instant as epoch seconds, or None. Never raises.

    None means "this comparison cannot be made" and every caller reads it that way rather
    than as zero — a missing timestamp that sorted first would make every check look as
    though it predated the base's recovery.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _run(args: list[str], *, cwd: Path | None, missing_hint: str) -> str:
    """One `gh` call, as stdout. `github._run`'s contract and its exception types.

    A deliberate copy rather than an import: that function's `url=` shapes every message
    it writes around a pull request, and a base-branch read has none. Six lines; sharing
    them would mean a parameter that means nothing to one of the two callers.
    """
    where = str(cwd) if cwd and Path(cwd).is_dir() else None
    shown = "`gh " + " ".join(args) + "`"
    try:
        proc = subprocess.run([gh_bin(), *args], capture_output=True, text=True,
                              timeout=CI_TIMEOUT, cwd=where)
    except FileNotFoundError as e:
        raise GhUnavailable(gh_missing_message(missing_hint), GitHubError.NO_GH) from e
    except subprocess.TimeoutExpired as e:
        raise GitHubError(f"{shown} timed out after {CI_TIMEOUT}s",
                          GitHubError.TIMEOUT) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        log.info("%s failed: %s", shown, detail)
        raise GitHubError(f"{shown} failed: {detail}", GitHubError.REFUSED)
    return proc.stdout or ""


def base_runs(base_ref: str, cwd: Path | None = None,
              limit: int = BASE_RUN_LIMIT) -> tuple[Run, ...]:
    """The base branch's recent CI, newest first. Raises `GitHubError` on any doubt.

    `--branch` selects runs whose head is that branch, which for a base branch is its
    `push` runs — the ones that say whether the base itself was buildable. Nothing about
    any pull request comes back here.
    """
    if not BRANCH_RE.match(base_ref or ""):
        raise GitHubError(f"{base_ref!r} is not a branch name this may ask about",
                          GitHubError.URL_REFUSED)
    stdout = _run(["run", "list", "--branch", base_ref, "--limit", str(limit),
                   "--json", RUN_FIELDS], cwd=cwd,
                  missing_hint="so Jarvis cannot tell a red base from a red branch")
    try:
        rows = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GitHubError(f"`gh run list --branch {base_ref}` returned no JSON "
                          f"({stdout[:200]!r})", GitHubError.UNREADABLE) from e
    if not isinstance(rows, list):
        raise GitHubError(f"`gh run list --branch {base_ref}` returned {rows!r}",
                          GitHubError.UNREADABLE)
    runs = [
        Run(run_id=int(r.get("databaseId") or 0),
            head_sha=str(r.get("headSha") or ""),
            conclusion=str(r.get("conclusion") or "").lower(),
            status=str(r.get("status") or "").lower(),
            started_at=parse_ts(r.get("startedAt")),
            workflow=str(r.get("workflowName") or ""))
        for r in rows if isinstance(r, dict)
    ]
    # Newest first, by the OS's own ordering rather than by trusting the page order. A
    # run with no parseable time sorts LAST: it cannot be the one that was head at a
    # given moment, and letting it sort first would answer that question wrongly.
    return tuple(sorted(runs, key=lambda r: (r.started_at is not None, r.started_at or 0),
                        reverse=True))


def update_branch(pr_url: str, cwd: Path | None = None) -> None:
    """Rebuild this pull request's merge ref against its base. THE ONLY WRITE HERE.

    `gh pr update-branch` is the "Update branch" button: GitHub merges the base into the
    head branch, server-side, and the pull request's checks re-run against a merge with
    the base AS IT IS NOW. That — not a re-run — is what clears an inherited failure.

    **MERGE, NEVER `--rebase`.** A rebase rewrites the branch's history and force-pushes
    it, which would destroy the provenance the verdict carry-forward rests on
    (`ops.carry_validated_head`) and is refused by the permission classifier for workers
    for the same reason it is wrong here.

    **NOT A GATED ACTION, re-decided on the real command rather than on the re-run this
    replaces.** It does push, which the earlier framing did not, and it is still not
    privileged: it merges nothing into the default branch, ships no release, restarts no
    service, adds no authored content, rewrites no history, and GitHub REFUSES it outright
    on conflict rather than letting anything resolve one — so the worst case is the branch
    it was already going to be. Contrast the merge in `automerge`, which is gated because
    it is irreversible and lands code on `main`. The heal never merges anything: the
    automatic merge that may follow still files its own gate for Neo, so this changes what
    that request says and never whether one happens. The bound is arithmetic instead — one
    update per (pull request, base sha), held by `Daemon.heal_inherited_failure`.

    Raises `GitHubError` if GitHub refuses. The caller treats that as "not healed this
    tick", spends the bound anyway and falls through to the worker nudge — the same shape
    every other transient `gh` failure in the poll already has.
    """
    url = checked_pr_url(pr_url, cwd)
    _run(["pr", "update-branch", url], cwd=cwd,
         missing_hint="so Jarvis cannot rebuild a pull request against a fixed base")


def commit_parents(pr_url: str, sha: str, cwd: Path | None = None) -> tuple[str, ...]:
    """The parents of `sha`, in order, in the pull request's own repository.

    **THE ONLY THING THAT CAN PROVE WHAT THE UPDATE ACTUALLY MERGED**, and it is needed
    because `gh pr update-branch` has no `--expected-head-oid`. `gh pr merge` has
    `--match-head-commit`, so the automatic merge can ask the SERVER to refuse if the
    head moved; this command cannot, so the equivalent guarantee has to be established
    AFTER the fact, by reading what the update produced
    (`ops.carry_validated_head` fact 3, review round 1 of wo-fdfa51c7).

    A read, and the method is pinned in the argument list rather than left to `gh`'s
    default — see `VERBS`. Both the repository and the sha are checked before they
    become a path: the repository comes from `checked_pr_url`, which is already bound to
    this project's own `origin`, and the sha through `SHA_RE`.

    Raises `GitHubError` on any doubt. The caller reads that as "the carry cannot be
    justified", which leaves the pull request held rather than merged — the direction a
    failure here has to fall.
    """
    url = checked_pr_url(pr_url, cwd)
    if not SHA_RE.match(sha or ""):
        raise GitHubError(f"{sha!r} is not a commit this may ask about",
                          GitHubError.URL_REFUSED)
    m = PR_URL_RE.match(url)
    if not m:  # unreachable — `checked_pr_url` anchors on the same pattern
        raise GitHubError(f"{url!r} is not a pull request URL",
                          GitHubError.URL_REFUSED)
    owner, repo = m.group(2), m.group(3)
    stdout = _run(["api", "--method", "GET", f"repos/{owner}/{repo}/commits/{sha}",
                   "--jq", ".parents[].sha"], cwd=cwd,
                  missing_hint="so Jarvis cannot prove what a branch update merged")
    return tuple(line.strip() for line in stdout.splitlines() if line.strip())


# -- the decision, pure ---------------------------------------------------------------


def latest(runs: tuple[Run, ...], workflow: str) -> Run | None:
    """The newest COMPLETED base run of this workflow, or None."""
    for r in runs:
        if r.workflow == workflow and r.completed:
            return r
    return None


def head_when(runs: tuple[Run, ...], workflow: str, at: float | None) -> Run | None:
    """The base run of this workflow that was the base's head at instant `at`.

    The newest that had STARTED by then — not the newest that had FINISHED. That
    distinction is the point: a pull request built at `at` was built as a merge with
    whatever commit was head then, so the verdict that commit eventually received is the
    verdict describing the base the pull request inherited, whether or not it had arrived.
    """
    if at is None:
        return None
    for r in runs:
        if r.workflow == workflow and r.started_at is not None and r.started_at <= at:
            return r
    return None


def base_is_red(runs: tuple[Run, ...], workflows: tuple[str, ...]) -> bool:
    """Is the base broken RIGHT NOW, for any workflow this pull request is failing?

    The window in which nudging a worker is pure waste: nothing it pushes can turn this
    pull request green, and every attempt costs a whole conversation re-send.
    """
    return any(r is not None and r.red for r in (latest(runs, w) for w in workflows))


def inherited(check: dict[str, str], runs: tuple[Run, ...]) -> bool:
    """Did this failing check fail because its BASE was broken, and is the base well now?

    Two facts, both required:

    * the base run that was head when this check STARTED concluded red, so every pull
      request built in that window built the base's failure alongside its own code;
    * the base's newest completed run of the same workflow is green, so the question a
      rebuilt merge ref will be asked is a different question from the one that failed.

    **THE SIGNAL IS TEMPORAL AND WORKFLOW-SCOPED, NOT TEXTUAL** — spec §2, Neo question
    435. Matching the failure text looks like the stronger signal and is the weaker one
    to build on: the temporal claim is CAUSAL (GitHub builds a pull request as a merge
    with its base, so a broken base is in the build by construction), while a text match
    is circumstantial and costs a log download per red check per tick. Matching the JOB
    name is worse than either — fail-fast cancels siblings, so the base and the pull
    request routinely name different shards of one matrix, which is precisely production
    shape 2 (PR #298's `unit (3.11)` against main's failure at 9dd7bcc).

    A false positive costs one branch update, bounded to one per (pull request, base sha).
    A false negative costs exactly today's behaviour: the worker is nudged.
    """
    workflow = check.get("workflow") or ""
    if not workflow:
        return False
    was = head_when(runs, workflow, parse_ts(check.get("started_at")))
    return bool(was and was.red and (n := latest(runs, workflow)) and n.green)


def base_sha(runs: tuple[Run, ...], workflows: tuple[str, ...]) -> str:
    """The base commit this heal is being attempted AGAINST — what bounds the attempt.

    The newest green base head across the workflows in play. One update per (pull
    request, this sha): if the merge ref is rebuilt on this base and CI is still red, the
    failure is the branch's own and the worker nudge is the correct next step. A LATER
    base recovery is a different sha and earns a fresh attempt, because it is a fresh
    question.
    """
    greens = [r for w in workflows if (r := latest(runs, w)) and r.green]
    greens.sort(key=lambda r: (r.started_at is not None, r.started_at or 0), reverse=True)
    return greens[0].head_sha if greens else ""
