"""A bug filed through Jarvis runs itself: work order, in-progress label, self-closing.

Issue #240. `jarvis bug report` used to create a GitHub issue and forget it, so a tracker
the OS wrote to was a tracker only a human maintained. These tests cover the three steps
that were missing — the issue is picked up as work, the tracker says so, and it closes
itself when the work LANDS — plus the things that make that safe to leave on for every
agent in the fleet: the priority is a required input and the only thing that routes,
a `critical`/`blocker` claim is re-assessed by Neo before anything is dispatched, the
label comes back off when the work does not land, an unreachable `gh` or an unreachable
Neo changes nothing and retries, and a human's own decisions about an issue are never
overridden.
"""

import ast
import json
import subprocess
from pathlib import Path

import pytest

from jarvis import bugreport, cli, issues
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_BUG_REPO

#: Everything here happens on the repository `fake_gh` makes `bug_repo()` answer, which
#: NOBODY OWNS (review round 2) — so a test that got past the fake `gh` would write to a
#: repository that does not exist rather than to the live public tracker.
PR = f"https://github.com/{FIXTURE_BUG_REPO}/pull/9"
PR_2 = f"https://github.com/{FIXTURE_BUG_REPO}/pull/10"


# -- fixtures -------------------------------------------------------------------------


def _origin(path, repo=FIXTURE_BUG_REPO):
    """Give a fixture checkout the `origin` that makes it the tracker's project."""
    subprocess.run(["git", "remote", "add", "origin",
                    f"https://github.com/{repo}.git"], cwd=path, check=True)


@pytest.fixture()
def fleet(jarvis_home, fake_claude, fake_gh, tmp_path, project, claude_json):
    """A started OS whose `proj_a` IS the OS's own tracker repo.

    The one condition `issues.tracker_project` requires, made true separately from
    everything else so a test can take it away: the checkout's `origin`.
    """
    from jarvis import ops

    _origin(project)
    catalog = tmp_path / "catalog-bugs.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet", "max_in_flight": 50},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project)}],
    }))
    ops.start_os(str(catalog), foreground=True)

    class Fleet:
        gh = fake_gh
        path = project
        catalog_path = catalog
        issue_url = fake_gh.issue_url

        def store(self):
            return ProjectStore(project)

        def spec(self):
            from jarvis.catalog import load_catalog
            return load_catalog(catalog).project("proj_a")

        def sweep(self):
            """One reconcile of the tracker, as the daemon runs it."""
            from jarvis.catalog import load_catalog
            from jarvis.daemon import Daemon
            store = ProjectStore(project)
            try:
                Daemon(load_catalog(catalog)).sync_issues(self.spec(), store)
            finally:
                store.close()

        def file_bug(self, title="wo send is lost", priority="blocker"):
            return bugreport.report_bug(title=title, description="d",
                                        expected="e", actual="a", priority=priority)

        def _last_triage(self):
            from jarvis.neo_store import NeoStore
            neo = NeoStore()
            try:
                asked = [r for r in neo.list_questions() if r.get("kind") == "triage"]
                return max(asked, key=lambda r: r["id"])
            finally:
                neo.close()

        def triage(self, **verdict):
            """Deliver a Neo verdict for the queued triage question, as the drain does."""
            from jarvis.catalog import load_catalog
            from jarvis.central_store import CentralStore
            from jarvis.daemon import Daemon
            full = {"escalate": False, "approve": True, "verdict": "approved",
                    "answer": "", "reason": "because", "dispatch": None, **verdict}
            central = CentralStore()
            try:
                Daemon(load_catalog(catalog))._deliver_triage_verdict(
                    central, self._last_triage(), full)
            finally:
                central.close()

        def blocker_wo(self, issue_url=None, title="wo send is lost"):
            """A bug all the way through the rails: filed `blocker`, confirmed by Neo,
            work order created. The starting point for every lifecycle test below."""
            if issue_url:
                self.gh.next_issue(issue_url)
            self.file_bug(title=title, priority="blocker")
            self.triage(approve=True)
            return self.wo_id(issue_url)

        def land(self, wo_id, pr_url=PR):
            """The work order's code reaches `main`, and the tracker is reconciled."""
            from jarvis import ops
            store = ProjectStore(project)
            try:
                store.update_work_order(wo_id, pr_url=pr_url)
                ops.complete_merged(store, store.get_work_order(wo_id))
            finally:
                store.close()
            self.sweep()

        def releases(self):
            """Every open work order filed to SHIP something, newest last."""
            from jarvis import db
            from jarvis.daemon import Daemon
            from jarvis.project_store import OPEN_STATUSES
            store = ProjectStore(project)
            try:
                return [w for w in store.list_work_orders(statuses=OPEN_STATUSES,
                                                          include_hidden=True)
                        if isinstance(db.from_json(w.get("metadata"), {}).get(
                            Daemon.RELEASE_BATCH_KEY), list)]
            finally:
                store.close()

        def wo_id(self, issue_url=None):
            """The work order a confirmed triage created, if any."""
            store = ProjectStore(project)
            try:
                rows = store.work_orders_for_issue(issue_url or fake_gh.issue_url)
                return rows[0]["id"] if rows else ""
            finally:
                store.close()

    return Fleet()


@pytest.fixture()
def opted_out(jarvis_home, fake_claude, fake_gh, catalog_file):
    """The shipped default: a fleet that has turned nothing on."""
    from jarvis import ops
    ops.start_os(str(catalog_file), foreground=True)
    return fake_gh


# -- what may write to GitHub ---------------------------------------------------------


def test_every_gh_command_this_module_builds_is_a_declared_issue_verb():
    """`github.py`'s read-only proof, pointed the other way.

    That module may build no write verb; this one is where the writes live, so the thing
    worth pinning is that the SET of them is small, declared and cannot grow by accident.
    A `["pr", "merge", …]` or a `["repo", "delete", …]` added anywhere in this file, at
    any nesting depth, fails here — there is no way to add a GitHub write to the OS
    without editing this test.
    """
    tree = ast.parse(Path(issues.__file__).read_text())
    verbs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) < 2:
            continue
        words = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
        if not all(isinstance(w, str) for w in words[:2]):
            continue
        # A command verb is one bare word. The prose lists this module builds for the
        # closing comment are strings too, and admitting them would make the guard
        # report its own comment text as an undeclared command.
        if not words[0] or not words[0].isidentifier():
            continue
        verbs.add((words[0], words[1]))
    assert verbs, "found no gh argument lists at all — has the module been rewritten?"
    assert verbs <= set(issues.ISSUE_VERBS), (
        f"an undeclared gh command is built in issues.py: "
        f"{sorted(verbs - set(issues.ISSUE_VERBS))}")


#: The commands in a write module that WRITE NOTHING, and what each is for. Every one of
#: them earns its place by making a write safe: `issue view` is what makes the label and
#: close idempotent, `issue list` is the follow-up dedupe, and `repo view` is the privacy
#: read that decides whether a seat's own words may be published at all (spec §9 of
#: docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md).
ISSUE_READS = {("issue", "view"), ("issue", "list"), ("repo", "view")}


def test_the_declared_verbs_are_exactly_these_writes_and_these_three_reads():
    """The AST test above answers "is every command declared?"; this one answers "which
    commands are declared", which is the question a reader of ISSUE_VERBS has.

    A `<=` check passes the moment a verb is ADDED to the tuple, so on its own it lets the
    set grow with only a line of production code touched. Pinning the set exactly is what
    makes a widening land here, in the place the decision gets recorded (`kn-2531869c`).
    """
    assert set(issues.ISSUE_VERBS) == ISSUE_READS | {
        ("issue", "edit"), ("issue", "comment"), ("issue", "close"),
        ("label", "create"), ("issue", "create")}


@pytest.mark.parametrize("url", [
    "",
    "http://github.com/gonandrap/agentic_os/issues/7",      # not https
    "--repo=someone/else",                                   # reads as a flag
    "https://github.com/gonandrap/agentic_os/issues/7/x",    # not anchored
    "https://github.com/someone/else/issues/7",              # another repository
    "https://evil.example/gonandrap/agentic_os/issues/7",    # the repository, another HOST
    "https://github.com/gonandrap/agentic_os/pull/7",        # not an issue
])
def test_the_os_refuses_to_write_anywhere_but_its_own_tracker(url):
    """The column is a string the record hands to a command run with the operator's
    credentials — `github.UntrustedPullRequest`'s exposure, on the writing side."""
    with pytest.raises(issues.IssueLifecycleError):
        issues.checked_issue_url(url)


def test_the_tracker_url_is_accepted():
    url = "https://github.com/gonandrap/agentic_os/issues/7"
    assert issues.checked_issue_url(url) == url


# -- the second repository: a project's own tracker (user ruling, 2026-09-16) ----------
#
# The panel's follow-ups are filed on the project under review, so this module no longer
# writes to exactly one repository. THE CHECK DID NOT GO AWAY, it gained a caller who
# names the repository — and these pin that the widening is bounded rather than open.


def test_a_caller_may_name_its_own_repository_and_is_still_held_to_it():
    """The new authority and its limit, in one pair. A URL on the repository the caller
    named is accepted; one on any OTHER repository is refused just as hard as before —
    so a URL out of the record still cannot redirect a write, which is the whole of the
    exposure `github.UntrustedPullRequest` describes."""
    ours = "https://github.com/acme/proj_a/issues/12"
    assert issues.checked_issue_url(ours, "acme/proj_a") == ours
    with pytest.raises(issues.IssueLifecycleError):
        issues.checked_issue_url(ours, "someone/else")
    # ...and naming a repository does not relax the SHAPE rule either.
    with pytest.raises(issues.IssueLifecycleError):
        issues.checked_issue_url("--repo=acme/proj_a", "acme/proj_a")
    # ...nor does it let the HOST through. A URL is `<host>/<owner>/<repo>/issues/N` and
    # the caller names only the middle: match on owner and repository alone and the
    # project's real name under an attacker's host reads as the project's own.
    with pytest.raises(issues.IssueLifecycleError):
        issues.checked_issue_url("https://evil.example/acme/proj_a/issues/12",
                                 "acme/proj_a")


def test_the_default_is_still_the_os_tracker():
    """Every pre-existing caller passes no repository, so the bug-tracker lifecycle's
    confinement is untouched by the widening. Without this the parameter could quietly
    default to "anywhere" and every test above would still pass."""
    with pytest.raises(issues.IssueLifecycleError):
        issues.checked_issue_url("https://github.com/acme/proj_a/issues/12")


@pytest.mark.parametrize("repo", [
    "", "acme", "acme/", "/proj_a", "-acme/proj_a",       # reads as a flag
    "acme/proj_a extra", "acme/proj a", "acme/proj_a;rm",
])
def test_a_repository_name_is_checked_before_it_becomes_an_argument(repo):
    """`--repo` is the value that now VARIES per project, where it used to be one
    constant — so it is held to an anchored shape for `checked_label`'s reason: `gh`
    reads a leading `-` as a flag."""
    with pytest.raises(issues.IssueLifecycleError):
        issues.checked_repo(repo)


def test_a_real_repository_name_is_accepted():
    assert issues.checked_repo("acme/proj_a") == "acme/proj_a"


# -- is the destination private? the read that decides what may be published -----------


def test_a_repository_github_calls_private_is_the_only_one_reported_private(fake_gh):
    """Both answers off the same read, because "it returns False" is also true of a
    function that never looks."""
    fake_gh.set_private(True)
    assert issues.repo_is_private("acme/proj_a") is True
    fake_gh.set_private(False)
    assert issues.repo_is_private("acme/proj_a") is False


@pytest.mark.parametrize("break_it, message", [
    ("fail", "gh: Could not resolve to a Repository with that name"),
    ("garble_privacy_read", "gh: a warning nobody expected"),
])
def test_a_repository_the_os_cannot_read_is_treated_as_public(fake_gh, break_it,
                                                              message):
    """FAIL CLOSED: a refusal and an unparseable answer reach the same branch as "public".
    Unknown is not a third case — the finding text goes out only where the OS has
    POSITIVELY established the destination is private (spec §9)."""
    getattr(fake_gh, break_it)(message)
    assert issues.repo_is_private("acme/proj_a") is False


def test_a_repository_name_that_would_read_as_a_flag_is_never_sent(fake_gh):
    """`gh repo view` takes the repository POSITIONALLY — it has no `--repo` flag — so
    `checked_repo` is the only thing standing between a name and an argument `gh` would
    read as an option. It raises, and the blanket `except` turns that into "not
    private", which is the safe answer for a repository nobody could look up."""
    assert issues.repo_is_private("--json=isPrivate") is False
    assert [c for c in fake_gh.calls if c["argv"][:2] == ["repo", "view"]] == [], (
        "a name that failed the shape check still reached `gh`")


# -- where an issue belongs, given its work order -------------------------------------


def _wo(store, status="pending", **fields):
    wo = store.create_work_order(title="t", issue_url="https://x/y/issues/1")
    if status != "pending":
        store.set_status(wo["id"], status)
    if fields:
        store.update_work_order(wo["id"], **fields)
    return store.get_work_order(wo["id"])


@pytest.mark.parametrize("status", ["pending", "running", "waiting_input",
                                    "needs_review", "waiting_pr_merge"])
def test_an_open_work_order_means_the_issue_is_in_progress(project, status):
    store = ProjectStore(project)
    try:
        assert issues.desired_state(store, _wo(store, status)) == issues.IN_PROGRESS
    finally:
        store.close()


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_a_dead_work_order_hands_the_issue_back(project, status):
    """Issue #240 C: a tracker that lies the other way is no better."""
    store = ProjectStore(project)
    try:
        assert issues.desired_state(store, _wo(store, status)) == issues.RELEASED
    finally:
        store.close()


def test_a_refused_pull_request_hands_the_issue_back_and_takes_it_again(project):
    """The work order stays OPEN when its pull request is closed unmerged — and nothing
    is under way, so the label must not claim otherwise. Reopening reverses it, because
    the state is derived (`pr_closure_told`) rather than written down once."""
    store = ProjectStore(project)
    try:
        wo = _wo(store, "needs_review")
        store.add_event(wo["id"], "pr_closed", {})
        assert issues.desired_state(store, wo) == issues.RELEASED
        store.add_event(wo["id"], "pr_reopened", {})
        assert issues.desired_state(store, wo) == issues.IN_PROGRESS
    finally:
        store.close()


def test_a_completed_work_order_closes_its_issue(project):
    store = ProjectStore(project)
    try:
        assert issues.desired_state(store, _wo(store, "completed")) == issues.CLOSED
    finally:
        store.close()


def test_completed_over_work_that_never_landed_leaves_the_issue_open(project):
    """Neo, question 289, and issue #232's gap on the tracker: `completed` alone does not
    prove the code reached `main`, so an order the user closed over a stranded branch
    takes its label off and leaves the issue for someone to decide about."""
    store = ProjectStore(project)
    try:
        wo = _wo(store, "completed")
        store.add_event(wo["id"], "work_unlanded", {"closed_by": "marked_done"})
        assert issues.desired_state(store, wo) == issues.RELEASED
    finally:
        store.close()


def test_a_merge_supersedes_an_earlier_stranding(project):
    """The same episode arithmetic `work_abandoned` uses: a refusal stands only until the
    work order is delivered again. Without this, an order parked once as unlanded could
    never close its issue however it ended."""
    store = ProjectStore(project)
    try:
        wo = _wo(store, "completed")
        store.add_event(wo["id"], "work_unlanded", {})
        store.add_event(wo["id"], "pr_merged", {})
        assert issues.desired_state(store, wo) == issues.CLOSED
    finally:
        store.close()


# -- the priority is the only thing that routes ---------------------------------------


def test_a_bug_report_without_a_priority_is_refused(fleet):
    """The user's ruling: a required input, no default, no inference. A default would be
    the OS deciding how serious someone else's bug is, and both available defaults are
    wrong — `critical` lets a typo cut a release, `low` buries an urgent report."""
    with pytest.raises(bugreport.BugReportError) as e:
        bugreport.report_bug(title="t", description="d", expected="e", actual="a")
    assert "priority" in str(e.value)
    assert "blocker" in str(e.value), "the refusal has to carry the rubric, or it is a wall"
    assert not fleet.gh.calls, "and nothing is filed — the refusal comes first"


def test_an_unknown_priority_is_refused_with_the_rubric(fleet):
    with pytest.raises(bugreport.BugReportError) as e:
        bugreport.report_bug(title="t", description="d", expected="e", actual="a",
                             priority="URGENT!!")
    assert "critical" in str(e.value) and "low" in str(e.value)


def test_the_cli_will_not_file_a_bug_without_one(fleet, capsys):
    """argparse refuses it before `report_bug` is reached: the flag is `required=True`
    with the five levels as `choices`, so the rubric is in `--help` and a typo cannot
    reach the tracker."""
    with pytest.raises(SystemExit):
        cli.main(["bug", "report", "t", "-d", "d", "-e", "e", "-a", "a"])
    assert "--priority" in capsys.readouterr().err


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_a_non_dispatching_bug_is_queued_and_never_asks_neo(fleet, level):
    """Three of the five commit the fleet to nothing, so there is nothing to guard
    against and no reason to spend a Neo call on them."""
    from jarvis.central_store import CentralStore
    from jarvis.neo_store import NeoStore

    pickup = fleet.file_bug(priority=level)["pickup"]
    assert pickup["backlog_id"] and not pickup["wo_id"]
    assert f"priority: {level}" in fleet.gh.issue()["labels"]
    assert "in progress" not in fleet.gh.issue()["labels"]

    neo = NeoStore()
    try:
        assert not [q for q in neo.list_questions() if q.get("kind") == "triage"]
    finally:
        neo.close()
    central = CentralStore()
    try:
        assert any(i["id"] == pickup["backlog_id"] for i in central.list_backlog())
    finally:
        central.close()


@pytest.mark.parametrize("level", ["critical", "blocker"])
def test_a_dispatching_claim_goes_to_neo_and_waits(fleet, level):
    """The filing agent's rating is a CLAIM. Nothing is dispatched off it — the bug waits
    in the backlog exactly like any other until Neo rules."""
    pickup = fleet.file_bug(priority=level)["pickup"]
    assert pickup["neo_question_id"], "a critical/blocker claim must be re-assessed"
    assert pickup["backlog_id"] and not pickup["wo_id"]
    assert f"priority: {level}" in fleet.gh.issue()["labels"]
    assert not fleet.wo_id(), "no work order exists until Neo confirms"


def test_neo_confirming_creates_the_work_order_and_labels_the_issue(fleet):
    from jarvis import ops

    fleet.file_bug(priority="blocker")
    fleet.triage(approve=True)

    wo_id = fleet.wo_id()
    assert wo_id
    _name, _path, wo = ops.find_work_order(wo_id)
    assert wo["issue_url"] == fleet.issue_url
    assert wo["issue_priority"] == "blocker"
    assert fleet.issue_url in wo["description"], \
        "the worker sees only the description — the issue link has to be in it"
    assert "in progress" in fleet.gh.issue()["labels"]
    assert "priority: blocker" in fleet.gh.issue()["labels"]


def test_neo_downgrading_leaves_it_in_the_backlog_and_corrects_the_label(fleet):
    fleet.file_bug(priority="blocker")
    fleet.triage(approve=False, answer="medium", reason="one command, has a workaround")

    assert not fleet.wo_id(), "a downgraded claim dispatches nothing"
    labels = fleet.gh.issue()["labels"]
    assert "priority: medium" in labels
    assert "priority: blocker" not in labels, "one priority label, not two"
    assert "in progress" not in labels


def test_a_downgrade_that_names_no_level_lands_on_the_safe_one(fleet):
    """Neo refusing to confirm is the whole signal; the level it landed on is detail. A
    level nobody stated must never be guessed at upwards."""
    fleet.file_bug(priority="critical")
    fleet.triage(approve=False, answer="")
    assert f"priority: {issues.SAFE_DOWNGRADE}" in fleet.gh.issue()["labels"]
    assert not fleet.wo_id()


def test_a_downgrade_that_names_a_higher_level_is_not_a_confirmation(fleet):
    """`deny` plus `blocker` in the answer is a contradiction, and the safe reading of a
    contradiction is the refusal — not the level that would spend money."""
    fleet.file_bug(priority="critical")
    fleet.triage(approve=False, answer="blocker")
    assert f"priority: {issues.SAFE_DOWNGRADE}" in fleet.gh.issue()["labels"]
    assert not fleet.wo_id()


def test_the_tracker_records_both_the_claim_and_the_verdict(fleet):
    """The user's instruction: the disagreement is the signal that says whether the
    rubric is working, so neither half may be overwritten silently."""
    from jarvis.central_store import CentralStore

    result = fleet.file_bug(priority="blocker")
    fleet.triage(approve=False, answer="medium", reason="bounded to one surface")

    # PUBLIC: both levels, so the disagreement is visible on the tracker itself.
    body = "\n".join(fleet.gh.issue()["comments"])
    assert "blocker" in body and "medium" in body
    # And the ORIGINAL claim is in the issue body, where no re-assessment can move it.
    filed = [c for c in fleet.gh.calls if c["argv"][:2] == ["issue", "create"]][0]
    assert "blocker" in filed["stdin"]
    assert result["url"]
    # PRIVATE: the reasoning, on the record the user reads, and NOT on the tracker.
    central = CentralStore()
    try:
        inbox = "\n".join((i["body"] or "") for i in central.unacked_inbox())
    finally:
        central.close()
    assert "bounded to one surface" in inbox
    assert "jarvis neo show" in inbox, "and a pointer to the rest of it"


def test_neos_reasoning_never_reaches_the_public_tracker(fleet):
    """Review round 2, and `closing_comment`'s rule on the other comment this OS writes.

    Neo answers with fleet context behind it — learnings, other work orders, project
    names, absolute paths — and a GitHub comment is indexed and cached whether or not it
    is later deleted. Nobody reads this one before it leaves the machine, so the tracker
    gets the two levels and the work order, and nothing a model wrote in prose.
    """
    leak = ("promoted over /home/gonzalo/workspace/secret_client per kn-deadbeef; "
            "wo-000000 hit the same thing")
    fleet.file_bug(priority="blocker")
    fleet.triage(approve=False, answer="medium", reason=leak)

    body = "\n".join(fleet.gh.issue()["comments"])
    assert "/home/gonzalo" not in body and "secret_client" not in body
    assert "kn-deadbeef" not in body and "wo-000000" not in body
    assert "blocker" in body and "medium" in body, "the levels still are public"


def test_neo_escalating_dispatches_nothing_and_says_so(fleet):
    """Fail closed. An unconfirmed `blocker` that quietly became a release is the worse
    failure by a long way, so the refusal direction is fixed."""
    from jarvis.central_store import CentralStore

    pickup = fleet.file_bug(priority="blocker")["pickup"]
    fleet.triage(escalate=True, approve=False, reason="not enough evidence")

    assert not fleet.wo_id()
    central = CentralStore()
    try:
        items = central.unacked_inbox()
    finally:
        central.close()
    assert any("UNCONFIRMED" in (i["title"] or "") for i in items)
    assert any(pickup["backlog_id"] in (i["body"] or "") for i in items), \
        "the user has to be told where it is waiting"


def test_a_filing_that_cannot_reach_neo_is_queued_not_dispatched(fleet, monkeypatch):
    """Same refusal, one layer earlier: Neo unreachable at filing time."""
    def boom(*_a, **_k):
        raise RuntimeError("neo store is gone")
    monkeypatch.setattr(issues, "ask_triage", boom)

    pickup = fleet.file_bug(priority="blocker")["pickup"]
    assert pickup["backlog_id"] and not pickup["wo_id"]
    assert "NOT confirmed" in pickup["reason"]
    assert not fleet.wo_id()


def test_nothing_is_routed_when_no_project_owns_the_tracker(opted_out):
    """The bug is filed and left for a human — the OS never invents a home for it."""
    result = bugreport.report_bug(title="t", description="d", expected="e", actual="a",
                                  priority="blocker")
    assert not result["pickup"]["wo_id"] and not result["pickup"]["backlog_id"]
    assert "git origin" in result["pickup"]["reason"]


def test_the_label_is_created_when_the_repository_does_not_have_it(fleet):
    """GitHub refuses `--add-label` for a label the repository never defined, which would
    otherwise make this feature fail on every tracker but the one it was written on."""
    fleet.gh.set_labels(["bug"])
    fleet.file_bug(priority="low")
    assert "priority: low" in fleet.gh.issue()["labels"]
    assert any(c["argv"][:2] == ["label", "create"] for c in fleet.gh.calls)


def test_one_issue_never_gets_two_work_orders(fleet):
    """Issue #240 D. The fake files every report at the same URL, which is exactly the
    shape being guarded: the same bug reported twice."""
    fleet.file_bug(priority="blocker")
    fleet.triage(approve=True)
    first = fleet.wo_id()
    fleet.file_bug(priority="blocker")
    fleet.triage(approve=True)

    store = fleet.store()
    try:
        assert [w["id"] for w in store.work_orders_for_issue(fleet.issue_url)] == [first]
    finally:
        store.close()


def test_a_settled_work_order_lets_the_issue_be_taken_up_again(fleet):
    """A reopened issue must be able to get a fresh work order — the dedup is about LIVE
    work, not about the issue ever having been looked at."""
    fleet.file_bug(priority="blocker")
    fleet.triage(approve=True)
    first = fleet.wo_id()
    store = fleet.store()
    try:
        store.set_status(first, "cancelled")
    finally:
        store.close()

    fleet.file_bug(priority="blocker")
    fleet.triage(approve=True)
    store = fleet.store()
    try:
        assert len(store.work_orders_for_issue(fleet.issue_url)) == 2
    finally:
        store.close()


def test_the_user_is_told_what_actually_happened(fleet):
    """`report_bug`'s own rule — never ping about a state that was not reached — applied
    to the rest of the lifecycle. A claim awaiting Neo says exactly that."""
    from jarvis.central_store import CentralStore

    pickup = fleet.file_bug(priority="blocker")["pickup"]
    central = CentralStore()
    try:
        bodies = "\n".join(i["body"] or "" for i in central.unacked_inbox())
    finally:
        central.close()
    assert pickup["backlog_id"] in bodies
    assert "re-assessing" in bodies, \
        "a queued claim must not read as a decision that was taken"


# -- the issue closes itself ----------------------------------------------------------


def test_a_merged_pull_request_closes_the_issue_with_the_record_on_it(fleet):
    """Steps 3 and 4 of the workflow issue #240 asks for, and decision F: months later
    the issue itself has to say what closed it."""
    from jarvis import ops

    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.update_work_order(
            wo_id, pr_url=PR,
            result_summary="patched /home/gonzalo/workspace/secret_client/app.py")
        ops.complete_merged(store, store.get_work_order(wo_id))
    finally:
        store.close()

    fleet.sweep()
    issue = fleet.gh.issue()
    assert issue["state"] == "CLOSED"
    assert "in progress" not in issue["labels"]
    body = "\n".join(issue["comments"])
    assert wo_id in body and PR in body
    # Review round 1: the work order id, the title and the PR — and NOTHING ELSE. The
    # worker wrote `result_summary` for the internal record, with no idea anyone would
    # publish it, and this tracker is public.
    assert "/home/gonzalo" not in body and "secret_client" not in body, \
        "worker prose must never reach a public issue unread"


def test_the_issue_is_not_closed_while_the_work_order_is_merely_completed(fleet):
    """Issue #232's gap, on the tracker this time: `completed` over a branch nobody
    landed is not a reason to tell the world the bug is fixed."""
    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.set_status(wo_id, "completed")
        store.add_event(wo_id, "work_unlanded", {"closed_by": "marked_done"})
    finally:
        store.close()

    fleet.sweep()
    assert fleet.gh.issue()["state"] == "OPEN"
    assert "in progress" not in fleet.gh.issue()["labels"], \
        "but nothing is under way, so no label"


def test_marking_a_bug_done_with_nothing_to_land_closes_it(fleet):
    """Neo's ruling on question 289: a not-a-bug, a docs-only fix or an already-fixed
    report has no pull request to wait for, and leaving its issue open for ever is the
    hand-maintained tracker this whole issue is about."""
    from jarvis import ops

    wo_id = fleet.blocker_wo()
    ops.mark_done(wo_id)
    fleet.sweep()
    assert fleet.gh.issue()["state"] == "CLOSED"


def test_a_cancelled_work_order_takes_the_label_back_off(fleet):
    from jarvis import ops

    wo_id = fleet.blocker_wo()
    assert "in progress" in fleet.gh.issue()["labels"]
    ops.cancel(wo_id)
    fleet.sweep()
    assert "in progress" not in fleet.gh.issue()["labels"]
    assert fleet.gh.issue()["state"] == "OPEN", "cancelling is not fixing"


def test_a_refused_pull_request_takes_the_label_off_and_puts_it_back(fleet):
    from jarvis import ops

    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.update_work_order(wo_id, pr_url=PR)
        ops.record_pr_closed(store, store.get_work_order(wo_id))
    finally:
        store.close()
    fleet.sweep()
    assert "in progress" not in fleet.gh.issue()["labels"]

    store = fleet.store()
    try:
        store.add_event(wo_id, "pr_reopened", {})
    finally:
        store.close()
    fleet.sweep()
    assert "in progress" in fleet.gh.issue()["labels"]


# -- the sweep costs nothing when there is nothing to do ------------------------------


def test_a_reconciled_issue_is_not_touched_again(fleet):
    fleet.blocker_wo()
    before = len(fleet.gh.calls)
    fleet.sweep()
    fleet.sweep()
    assert len(fleet.gh.calls) == before, \
        "a tracker already saying the right thing must cost no gh call at all"


def test_a_project_that_never_filed_a_bug_pays_nothing(opted_out, catalog_file, project):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    store = ProjectStore(project)
    try:
        store.create_work_order(title="ordinary work")
        before = len(opted_out.calls)
        Daemon(load_catalog(catalog_file)).sync_issues(
            load_catalog(catalog_file).project("proj_a"), store)
        assert len(opted_out.calls) == before
    finally:
        store.close()


# -- `gh` unreachable: fail closed, say so once, retry --------------------------------


def test_an_unreachable_gh_changes_nothing_and_retries(fleet):
    from jarvis import ops

    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.update_work_order(wo_id, pr_url=PR)
        ops.complete_merged(store, store.get_work_order(wo_id))
    finally:
        store.close()

    fleet.gh.fail("gh: could not authenticate")
    fleet.sweep()
    assert fleet.gh.issue()["state"] == "OPEN", "nothing may be half-applied"
    store = fleet.store()
    try:
        assert store.get_work_order(wo_id)["issue_state"] == issues.IN_PROGRESS, \
            "the column records what was APPLIED — a failed write records nothing"
    finally:
        store.close()

    fleet.gh.works()
    fleet.sweep()
    assert fleet.gh.issue()["state"] == "CLOSED", "the next sweep is the retry"


def test_the_user_hears_once_that_the_tracker_is_not_being_kept_up(fleet):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.set_status(wo_id, "cancelled")
    finally:
        store.close()

    fleet.gh.fail("gh: could not authenticate")
    daemon = Daemon(load_catalog(fleet.catalog_path))
    store = fleet.store()
    try:
        for _ in range(3):
            daemon.sync_issues(fleet.spec(), store)
        notes = [n for n in store.unrouted_notifications()
                 if n.get("source") == "issue-sync"]
    finally:
        store.close()
    assert len(notes) == 1, "an inbox entry every tick is how an inbox stops being read"


def test_a_filing_says_so_when_the_priority_label_did_not_reach_the_tracker(
        fleet, monkeypatch):
    """The issue exists, so the filing must not fail — but it must not claim the tracker
    shows something it does not either."""
    def boom(*_a, **_k):
        raise issues.IssueLifecycleError("gh fell over")
    monkeypatch.setattr(issues, "set_priority_label", boom)

    result = fleet.file_bug(priority="high")
    assert result["url"], "the issue was created; the filing stands"
    assert result["pickup"]["backlog_id"], "so does the queued item"
    assert "gh fell over" in result["pickup"]["label_error"]
    assert "did NOT reach the issue" in bugreport.pickup_note(result["pickup"])


# -- never fight a human --------------------------------------------------------------


def test_an_issue_a_human_already_closed_is_commented_on_but_not_reclosed(fleet):
    from jarvis import ops

    wo_id = fleet.blocker_wo()
    fleet.gh.set_issue(fleet.issue_url, state="CLOSED", labels=["in progress"])
    store = fleet.store()
    try:
        store.update_work_order(wo_id, pr_url=PR)
        ops.complete_merged(store, store.get_work_order(wo_id))
    finally:
        store.close()

    fleet.sweep()
    closes = [c for c in fleet.gh.calls if c["argv"][:2] == ["issue", "close"]]
    assert not closes, "it was already closed — re-closing is a write for nothing"
    assert wo_id in "\n".join(fleet.gh.issue()["comments"]), \
        "the record still belongs on the issue"

    fleet.sweep()
    closing = [c for c in fleet.gh.issue()["comments"] if "Closed by" in c]
    assert len(closing) == 1, "and exactly once"


def test_an_issue_a_human_reopened_is_left_reopened(fleet):
    from jarvis import ops

    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.update_work_order(wo_id, pr_url=PR)
        ops.complete_merged(store, store.get_work_order(wo_id))
    finally:
        store.close()
    fleet.sweep()
    assert fleet.gh.issue()["state"] == "CLOSED"

    fleet.gh.set_issue(fleet.issue_url, state="OPEN")
    before = len(fleet.gh.calls)
    fleet.sweep()
    assert fleet.gh.issue()["state"] == "OPEN", "reopening is the user's call, not ours"
    assert len(fleet.gh.calls) == before, "and the OS does not even look"


def test_a_human_closing_an_issue_under_a_live_work_order_is_not_undone(fleet):
    fleet.file_bug()
    fleet.gh.set_issue(fleet.issue_url, state="CLOSED", labels=["in progress"])
    fleet.sweep()
    assert fleet.gh.issue()["state"] == "CLOSED", "the OS never reopens an issue"


# -- a confirmed fix that LANDS ships a release ---------------------------------------


ISSUE_2 = f"https://github.com/{FIXTURE_BUG_REPO}/issues/8"


def test_a_landed_confirmed_blocker_files_a_release_through_the_ordinary_path(fleet):
    """The user's instruction: the automatic ship goes through the existing release
    machinery, not around it. So what the OS files is a WORK ORDER told to run
    `shipit.sh --stage`, which gates like any other — not a release of its own."""
    fleet.land(fleet.blocker_wo())

    releases = fleet.releases()
    assert len(releases) == 1
    brief = releases[0]["description"]
    assert f"scripts/shipit.sh --stage --wo {releases[0]['id']}" in brief, \
        "the brief must name the staged path and the order's own id"
    assert "gated" in brief, "the worker is told the gate is expected, not a failure"
    assert fleet.issue_url in brief, "and which fix it is shipping"


def test_a_second_landed_blocker_joins_the_pending_release(fleet):
    """Two blockers ten minutes apart are one release: the first has not gone out yet,
    and a release ships whatever is on `main`."""
    fleet.land(fleet.blocker_wo())
    first = fleet.releases()[0]["id"]

    second = fleet.blocker_wo(ISSUE_2, title="daemon drops a tick")
    fleet.land(second, pr_url=PR_2)

    releases = fleet.releases()
    assert len(releases) == 1 and releases[0]["id"] == first, \
        "a second release order is a second restart of the fleet for one batch of fixes"
    assert ISSUE_2 in releases[0]["description"], "but it must say it carries both"
    assert fleet.issue_url in releases[0]["description"]


def test_a_settled_release_does_not_hold_the_next_fix_back(fleet):
    """Batching is "one release IN FLIGHT", not "one release". Once it has shipped, the
    next landed blocker earns its own."""
    fleet.land(fleet.blocker_wo())
    first = fleet.releases()[0]["id"]
    store = fleet.store()
    try:
        store.set_status(first, "completed")
    finally:
        store.close()

    fleet.land(fleet.blocker_wo(ISSUE_2, title="daemon drops a tick"),
               pr_url=PR_2)
    assert len(fleet.releases()) == 1, "the settled one is no longer open"
    assert fleet.releases()[0]["id"] != first


def test_a_fix_that_merely_completed_ships_nothing(fleet):
    """Issue #240 B and the user's point 4: "ship once the work lands" means landed.
    Cutting a release off a `completed` signal is issue #232's gap with a version number
    on it."""
    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.update_work_order(wo_id, pr_url=PR)
        store.set_status(wo_id, "completed")
        store.add_event(wo_id, "work_unlanded", {"closed_by": "marked_done"})
    finally:
        store.close()

    fleet.sweep()
    assert fleet.gh.issue()["state"] == "OPEN", \
        "the code is on a branch nobody merged — the bug is not fixed yet"
    assert not fleet.releases(), "nothing landed, so there is nothing to ship"


def test_a_blocker_that_produced_no_code_closes_its_issue_and_ships_nothing(fleet):
    """The other route into CLOSED: a `blocker` that turned out to need no code. The
    issue closes (Neo's ruling on question 289), and a release carrying nothing would be
    a restart of the whole fleet for an empty tag."""
    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        store.set_status(wo_id, "completed")
    finally:
        store.close()

    fleet.sweep()
    assert fleet.gh.issue()["state"] == "CLOSED"
    assert not fleet.releases()


def test_a_landed_fix_for_a_backlog_priority_ships_nothing(fleet):
    """Only `critical` and `blocker` commit the fleet to a release. A `high` the user
    promoted themselves closes its issue and stops there."""
    fleet.file_bug(priority="high")
    store = fleet.store()
    try:
        wo = store.create_work_order(title="fix it", issue_url=fleet.issue_url,
                                     issue_priority="high")
    finally:
        store.close()
    fleet.land(wo["id"])

    assert fleet.gh.issue()["state"] == "CLOSED"
    assert not fleet.releases()


def test_the_release_order_is_filed_once_however_often_the_sweep_runs(fleet):
    fleet.land(fleet.blocker_wo())
    fleet.sweep()
    fleet.sweep()
    assert len(fleet.releases()) == 1


# -- the record, and the question nobody is waiting on --------------------------------


def _escalate(fleet, reason="not enough evidence"):
    """Drive the triage question to `escalated`, exactly as `neo.drain_queue` does.

    The daemon's delivery and the question's own status are written by two different
    callers — `_deliver_triage_verdict` and `drain_queue` — so a test that only calls the
    first is testing the inbox row and nothing else.
    """
    from jarvis.neo_store import NeoStore

    q = fleet._last_triage()
    fleet.triage(escalate=True, approve=False, reason=reason)
    neo = NeoStore()
    try:
        neo.mark(q["id"], "escalated", reason=reason)
    finally:
        neo.close()
    return q["id"]


@pytest.mark.parametrize("kind,label", [
    ("issue_in_progress", "under way"),
    ("issue_released", "handed back"),
    ("issue_closed", "closed"),
    ("release_batched", "release"),
])
def test_every_event_this_feature_writes_has_a_label_a_reader_can_read(kind, label):
    """kn-3f133363: falling through to the generic renderer is NOT the same claim as a
    reader being able to see it — the fallback prints the bare kind beside a JSON blob.

    The timeline is the ONLY surface these four have. The issue shows the result but
    never when the OS decided it, and `issue_state` is a column nothing renders.
    """
    from jarvis import timeline

    text, _detail = timeline._describe(kind, {"issue_url": "u", "wo_id": "w"})
    assert text != kind, f"{kind} falls through to the raw-kind renderer"
    assert label in text.lower()


def test_the_events_the_lifecycle_writes_reach_the_timeline(fleet):
    """And the labels are reachable from real events, not only from `_describe`."""
    from jarvis import timeline

    wo_id = fleet.blocker_wo()
    store = fleet.store()
    try:
        entries = timeline.build_timeline(store.get_work_order(wo_id),
                                          store.list_events(wo_id),
                                          store.list_messages(wo_id))
    finally:
        store.close()
    assert any("under way" in (e.get("label") or "") for e in entries), \
        "the label put on the tracker has to show up on the record"


def test_an_unconfirmed_triage_question_is_visible_and_points_at_the_backlog(fleet):
    """kn-4edb0eb7: a Q_KIND is a claim about WHO IS WAITING, and for `triage` the answer
    is nobody — its `wo_id` is empty. It must still reach the user, because an
    unconfirmed `blocker` nobody hears about is the failure this path exists to prevent,
    but pointed at the command that actually resolves it."""
    from jarvis import ops

    pickup = fleet.file_bug(priority="blocker")["pickup"]
    qid = _escalate(fleet)

    items = [a for a in ops.os_status()["attention"]
             if a.get("neo_question_id") == qid]
    assert items, "an escalated priority claim must not be invisible"
    assert "backlog promote" in items[0]["decide"], \
        "`jarvis neo answer` would message a work order that does not exist"
    assert pickup["backlog_id"] in items[0]["decide"]


def test_answering_a_triage_question_is_refused_and_names_the_right_command(fleet):
    """The other half of the same guard. `ops.neo_answer_escalated` forwards every kind
    it does not refuse, and the forward here would look up `wo_id=''`."""
    from jarvis import ops

    pickup = fleet.file_bug(priority="blocker")["pickup"]
    qid = _escalate(fleet)

    with pytest.raises(ops.OpsError) as e:
        ops.neo_answer_escalated(qid, "make it a blocker")
    assert "backlog promote" in str(e.value) and pickup["backlog_id"] in str(e.value)


def test_correcting_neo_on_a_triage_question_messages_nobody(fleet):
    """kn-4edb0eb7's worst case: `neo review --correct` forwards to the worker whenever
    the order is not terminal, and it is the command the OS itself tells the user to
    run. The learning still lands — that is what teaches the rubric."""
    from jarvis import ops
    from jarvis.neo_store import NeoStore

    fleet.file_bug(priority="blocker")
    qid = fleet._last_triage()["id"]
    fleet.triage(approve=False, answer="medium", reason="bounded to one command")
    neo = NeoStore()
    try:  # what `neo.drain_queue` records beside the delivery the fixture just drove
        neo.record_answer(qid, "medium", reason="bounded to one command")
    finally:
        neo.close()

    out = ops.neo_review(qid, approved=False, feedback="that was a real blocker")
    assert out["learning_recorded"], "the correction still teaches Neo"
    assert not out["forwarded_to_worker"]


def test_a_triage_question_is_closed_once_its_backlog_item_moves(fleet):
    """INV-NEO-ESCALATION-STALE, extended to the one kind whose subject is a CENTRAL
    backlog item rather than a row in this project's database. Without it a claim the
    user has already promoted goes on asking for a ruling nobody can give — the exact
    production shape that invariant was written for."""
    from jarvis import invariants
    from jarvis.central_store import CentralStore
    from jarvis.neo_store import NeoStore

    pickup = fleet.file_bug(priority="blocker")["pickup"]
    qid = _escalate(fleet)

    store = fleet.store()
    try:
        assert not list(invariants.check_neo_escalations_are_live(store)), \
            "while the item is still queued the question is live"
        central = CentralStore()
        try:
            central.mark_backlog(pickup["backlog_id"], "promoted")
        finally:
            central.close()
        found = list(invariants.check_neo_escalations_are_live(store))
    finally:
        store.close()

    assert found and found[0].invariant == "INV-NEO-ESCALATION-STALE"
    neo = NeoStore()
    try:
        assert neo.get(qid)["status"] not in ("escalated", "failed")
    finally:
        neo.close()
