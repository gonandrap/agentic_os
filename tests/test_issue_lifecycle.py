"""A bug filed through Jarvis runs itself: work order, in-progress label, self-closing.

Issue #240. `jarvis bug report` used to create a GitHub issue and forget it, so a tracker
the OS wrote to was a tracker only a human maintained. These tests cover the three steps
that were missing — the issue is picked up as work, the tracker says so, and it closes
itself when the work LANDS — plus the four things that make that safe to switch on:
nothing fires unless a project opted in, the label comes back off when the work does not
land, an unreachable `gh` changes nothing and retries, and a human's own decisions about
an issue are never overridden.
"""

import ast
import json
import subprocess
from pathlib import Path

import pytest

from jarvis import bugreport, issues
from jarvis.project_store import ProjectStore

PR = "https://github.com/gonandrap/agentic_os/pull/9"


# -- fixtures -------------------------------------------------------------------------


def _origin(path, repo="gonandrap/agentic_os"):
    """Give a fixture checkout the `origin` that makes it the tracker's project."""
    subprocess.run(["git", "remote", "add", "origin",
                    f"https://github.com/{repo}.git"], cwd=path, check=True)


@pytest.fixture()
def fleet(jarvis_home, fake_claude, fake_gh, tmp_path, project, claude_json):
    """A started OS whose `proj_a` IS the OS's own tracker repo and has opted in.

    The two conditions `issues.tracker_project` requires, made true separately so a test
    can take either away: the git origin, and `bugs.auto_work_order`.
    """
    from jarvis import ops

    _origin(project)
    catalog = tmp_path / "catalog-bugs.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet", "max_in_flight": 50},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "bugs": {"auto_work_order": True}}],
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

        def file_bug(self, title="wo send is lost"):
            return bugreport.report_bug(title=title, description="d",
                                        expected="e", actual="a")

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


@pytest.mark.parametrize("url", [
    "",
    "http://github.com/gonandrap/agentic_os/issues/7",      # not https
    "--repo=someone/else",                                   # reads as a flag
    "https://github.com/gonandrap/agentic_os/issues/7/x",    # not anchored
    "https://github.com/someone/else/issues/7",              # another repository
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


# -- filing a bug puts it on the rails ------------------------------------------------


def test_filing_a_bug_creates_a_work_order_and_labels_the_issue(fleet):
    from jarvis import ops

    result = fleet.file_bug()
    pickup = result["pickup"]
    assert pickup["created"] and pickup["project"] == "proj_a"

    _name, _path, wo = ops.find_work_order(pickup["wo_id"])
    assert wo["issue_url"] == fleet.issue_url
    assert result["url"] in wo["description"], \
        "the worker sees only the description — the issue link has to be in it"
    assert "in progress" in fleet.gh.issue()["labels"]
    assert wo["issue_state"] == issues.IN_PROGRESS


def test_nothing_is_picked_up_unless_a_project_opted_in(opted_out):
    """The shipped default. Any agent in the fleet can run `jarvis bug report`, so an
    unconditional yes would let one worker commit the fleet to unbounded work."""
    result = bugreport.report_bug(title="t", description="d", expected="e", actual="a")
    assert not result["pickup"]["wo_id"]
    assert "auto_work_order" in result["pickup"]["reason"]
    assert "in progress" not in opted_out.issue()["labels"], \
        "an opted-out fleet must not label anything"


def test_the_label_is_created_when_the_repository_does_not_have_it(fleet):
    """GitHub refuses `--add-label` for a label the repository never defined, which would
    otherwise make this feature fail on every tracker but the one it was written on."""
    fleet.gh.set_labels(["bug"])
    fleet.file_bug()
    assert "in progress" in fleet.gh.issue()["labels"]
    assert any(c["argv"][:2] == ["label", "create"] for c in fleet.gh.calls)


def test_one_issue_never_gets_two_work_orders(fleet):
    """Issue #240 D. The fake files every report at the same URL, which is exactly the
    shape being guarded: the same bug reported twice."""
    first = fleet.file_bug()["pickup"]
    second = fleet.file_bug()["pickup"]
    assert second["wo_id"] == first["wo_id"]
    assert not second["created"]
    store = fleet.store()
    try:
        assert len(store.work_orders_for_issue(fleet.issue_url)) == 1
    finally:
        store.close()


def test_a_settled_work_order_lets_the_issue_be_taken_up_again(fleet):
    """A reopened issue must be able to get a fresh work order — the dedup is about LIVE
    work, not about the issue ever having been looked at."""
    first = fleet.file_bug()["pickup"]
    store = fleet.store()
    try:
        store.set_status(first["wo_id"], "cancelled")
    finally:
        store.close()
    assert fleet.file_bug()["pickup"]["wo_id"] != first["wo_id"]


def test_the_user_is_told_what_actually_happened(fleet):
    """`report_bug`'s own rule — never ping about a state that was not reached — applied
    to the rest of the lifecycle."""
    from jarvis.central_store import CentralStore

    result = fleet.file_bug()
    central = CentralStore()
    try:
        bodies = "\n".join(i["body"] or "" for i in central.unacked_inbox())
    finally:
        central.close()
    assert result["pickup"]["wo_id"] in bodies


# -- the issue closes itself ----------------------------------------------------------


def test_a_merged_pull_request_closes_the_issue_with_the_record_on_it(fleet):
    """Steps 3 and 4 of the workflow issue #240 asks for, and decision F: months later
    the issue itself has to say what closed it."""
    from jarvis import ops

    wo_id = fleet.file_bug()["pickup"]["wo_id"]
    store = fleet.store()
    try:
        store.update_work_order(wo_id, pr_url=PR, result_summary="fixed the drop")
        ops.complete_merged(store, store.get_work_order(wo_id))
    finally:
        store.close()

    fleet.sweep()
    issue = fleet.gh.issue()
    assert issue["state"] == "CLOSED"
    assert "in progress" not in issue["labels"]
    body = "\n".join(issue["comments"])
    assert wo_id in body and PR in body and "fixed the drop" in body


def test_the_issue_is_not_closed_while_the_work_order_is_merely_completed(fleet):
    """Issue #232's gap, on the tracker this time: `completed` over a branch nobody
    landed is not a reason to tell the world the bug is fixed."""
    wo_id = fleet.file_bug()["pickup"]["wo_id"]
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

    wo_id = fleet.file_bug()["pickup"]["wo_id"]
    ops.mark_done(wo_id)
    fleet.sweep()
    assert fleet.gh.issue()["state"] == "CLOSED"


def test_a_cancelled_work_order_takes_the_label_back_off(fleet):
    from jarvis import ops

    wo_id = fleet.file_bug()["pickup"]["wo_id"]
    assert "in progress" in fleet.gh.issue()["labels"]
    ops.cancel(wo_id)
    fleet.sweep()
    assert "in progress" not in fleet.gh.issue()["labels"]
    assert fleet.gh.issue()["state"] == "OPEN", "cancelling is not fixing"


def test_a_refused_pull_request_takes_the_label_off_and_puts_it_back(fleet):
    from jarvis import ops

    wo_id = fleet.file_bug()["pickup"]["wo_id"]
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
    fleet.file_bug()
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

    wo_id = fleet.file_bug()["pickup"]["wo_id"]
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

    fleet.file_bug()
    store = fleet.store()
    try:
        store.set_status(store.work_orders_tracking_issues()[0]["id"], "cancelled")
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


def test_a_filing_says_so_when_the_label_did_not_reach_the_tracker(fleet, monkeypatch):
    """The issue exists, so the filing must not fail — but it must not claim the tracker
    shows something it does not either."""
    def boom(*_a, **_k):
        raise issues.IssueLifecycleError("gh fell over")
    monkeypatch.setattr(issues, "apply", boom)

    result = fleet.file_bug()
    assert result["url"], "the issue was created; the filing stands"
    assert result["pickup"]["wo_id"], "so does the work order"
    assert "gh fell over" in result["pickup"]["label_error"]
    assert "NOT labelled" in bugreport.pickup_note(result["pickup"])


# -- never fight a human --------------------------------------------------------------


def test_an_issue_a_human_already_closed_is_commented_on_but_not_reclosed(fleet):
    from jarvis import ops

    wo_id = fleet.file_bug()["pickup"]["wo_id"]
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
    assert len(fleet.gh.issue()["comments"]) == 1, "and exactly once"


def test_an_issue_a_human_reopened_is_left_reopened(fleet):
    from jarvis import ops

    wo_id = fleet.file_bug()["pickup"]["wo_id"]
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
