"""Work orders that end in a pull request: `waiting_pr_merge`, and the title prefix.

Two halves of the same idea — a PR is where a work order leaves the OS and becomes
something a human has to act on, so the OS has to (a) keep saying so until they act,
and (b) leave the work order id on the artifact they are looking at.

`waiting_pr_merge` is deliberately NOT an attention item: it is a merge queue the user
works through, not a decision blocking the fleet, and the "NEEDS YOU" strip stops being
read the moment everything finished ends up in it.
"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, github, hooks, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import PR_POLL_EVERY_TICKS, Daemon
from jarvis.invariants import PR_CLOSED_BLOCKER, check_project, true_blockers
from jarvis.project_store import OPEN_STATUSES, ProjectStore

PR = "https://github.com/acme/proj/pull/7"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


# -- the state ----------------------------------------------------------------------


def test_finish_with_a_pr_parks_the_work_order(started, project):
    wo = ops.create_work_order("proj_a", "add feature X")

    out = ops.finish(wo["id"], "opened a PR", pr_url=PR)

    assert out["status"] == "waiting_pr_merge"
    store = ProjectStore(project)
    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_pr_merge"
    assert row["pr_url"] == PR
    assert row["result_summary"] == "opened a PR"


def test_finish_without_a_pr_still_completes(started, project):
    wo = ops.create_work_order("proj_a", "answered a question")

    assert ops.finish(wo["id"], "no code needed")["status"] == "completed"


def test_waiting_pr_merge_is_open_but_never_asks_for_the_user(started, project):
    """The whole point: it stays visible without spending the attention budget."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)

    store = ProjectStore(project)
    row = store.get_work_order(wo["id"])
    assert row["status"] in OPEN_STATUSES
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []
    # and no invariant thinks that flagless open work order is a bug
    assert [v.invariant for v in check_project(store)] == []
    assert wo["id"] in [w["id"] for w in store.list_work_orders(statuses=OPEN_STATUSES)]
    assert ops.os_status()["attention"] == []


def test_pending_assumptions_outrank_the_pr(started, project):
    """A merge before the decision would accept the assumptions by the back door."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.assume(wo["id"], "used tabs, not spaces")

    out = ops.finish(wo["id"], "opened a PR", pr_url=PR)

    assert out["status"] == "needs_review"
    store = ProjectStore(project)
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"]
    assert row["pr_url"] == PR  # recorded either way — the link is still useful


def test_the_reconciler_leaves_it_parked(started, project, fake_claude, settle_turns):
    """Settlement re-runs every tick; without the pr_url branch it would complete it."""
    wo = ops.create_work_order("proj_a", "add feature X")
    started.tick()
    store = ProjectStore(project)
    assert settle_turns(store)
    ops.finish(wo["id"], "opened a PR", pr_url=PR)

    started.tick_count = 0
    started.tick()
    started.tick_count = 0
    started.tick()

    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_the_user_closes_it_after_merging(started, project):
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)

    out = ops.mark_done(wo["id"])

    assert out["was"] == "waiting_pr_merge"
    assert out["status"] == "completed"


def test_cli_finish_passes_the_pr_through(started, project, capsys):
    wo = ops.create_work_order("proj_a", "add feature X")

    cli.main(["wo", "finish", wo["id"], "--summary", "done", "--pr", PR])

    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    assert PR in capsys.readouterr().out


def test_cli_list_puts_running_then_pr_merges_first(started, project, capsys):
    """`jarvis wo list` orders by what the user can act on, like the dashboard."""
    running = ops.create_work_order("proj_a", "still going")
    merging = ops.create_work_order("proj_a", "waiting on a merge")
    pending = ops.create_work_order("proj_a", "not started yet")  # newest: listed first
    ops.finish(merging["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    store.set_status(running["id"], "running")

    cli.main(["wo", "list"])

    lines = [ln for ln in capsys.readouterr().out.splitlines() if "wo-" in ln]
    order = [next(w["id"] for w in (running, merging, pending) if w["id"] in ln)
             for ln in lines]
    assert order[:2] == [running["id"], merging["id"]]
    assert pending["id"] in order


# -- the way back from `needs_review` -------------------------------------------------
#
# `finish` routes a work order with pending assumptions to `needs_review` even when it
# carries a PR — the decision outranks the merge. So the review is the ONLY route back,
# and it has to put the work order where `finish` would have if the assumptions had never
# existed. Settling it as `completed` instead loses the PR twice over: off the user's
# open list, and out of the merge poll that would have ended it for them.


@pytest.fixture()
def reviewable_pr(started, project):
    """Finished behind a PR, held in `needs_review` by one pending assumption."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.assume(wo["id"], "used tabs, not spaces")
    assert ops.finish(wo["id"], "opened a PR", pr_url=PR)["status"] == "needs_review"
    return wo


def test_accepting_the_assumptions_parks_the_pr_instead_of_completing(
        started, project, reviewable_pr):
    out = ops.review_work_order(reviewable_pr["id"], accept=True)

    assert out["status"] == "waiting_pr_merge"
    store = ProjectStore(project)
    row = store.get_work_order(reviewable_pr["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []


def test_the_parked_work_order_is_still_polled_after_a_review(
        started, project, fake_gh, reviewable_pr):
    """The point of parking it: the merge still ends the work order unattended."""
    ops.review_work_order(reviewable_pr["id"], accept=True, feedback="tabs are fine")
    fake_gh.set_pr(PR, "MERGED")
    store = ProjectStore(project)

    poll(started, store)

    assert store.get_work_order(reviewable_pr["id"])["status"] == "completed"


def test_accepting_without_a_pr_still_completes(started, project):
    """Unchanged for the ordinary case — only a PR-carrying work order is held back."""
    wo = ops.create_work_order("proj_a", "answered a question")
    ops.assume(wo["id"], "read it as a question, not a code change")
    ops.finish(wo["id"], "no code needed")

    out = ops.review_work_order(wo["id"], accept=True)

    assert out["status"] == "completed"
    assert ProjectStore(project).get_work_order(wo["id"])["status"] == "completed"


def test_a_review_after_the_pr_was_closed_does_not_re_park_it(
        started, project, fake_gh, parked):
    """A CLOSED pull request is never going to merge, so the merge queue is the one
    place this must not go back to — the next poll would only flag it again."""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)
    poll(started, store)

    out = ops.review_work_order(parked["id"], accept=True)

    assert out["status"] == "completed"
    assert store.get_work_order(parked["id"])["status"] == "completed"


def test_rejecting_leaves_the_work_order_where_it_was(started, project, reviewable_pr):
    """A rejection is not an ending: the worker gets guidance and the PR is still its
    problem, so nothing about the merge queue changes."""
    out = ops.review_work_order(reviewable_pr["id"], accept=False,
                                feedback="No — spaces, like the rest of the file")

    assert out["status"] == "needs_review"
    assert ProjectStore(project).get_work_order(
        reviewable_pr["id"])["status"] == "needs_review"


# -- the merge poll -----------------------------------------------------------------
#
# The other half of `waiting_pr_merge`: the OS finding out on its own that the pull
# request was dealt with, so a merge the user already performed does not also cost them
# a `jarvis wo done`. Everything here goes through the fake `gh` — the real one is
# unreachable under the test-isolation gate, which is the point.


@pytest.fixture()
def parked(started, project, fake_gh):
    """A work order finished behind PR, with `gh` ready to be told what happened."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    return wo


def poll(daemon, store):
    """Run the poll step alone, without the rest of the tick around it."""
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


def test_a_merged_pr_completes_the_work_order(started, project, fake_gh, parked):
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-02T10:00:00Z")
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["status"] == "completed"
    assert row["pr_state"] == "MERGED"
    assert not row["needs_attention"]
    # the timeline has to say GitHub ended this, not the user
    events = [e for e in store.list_events(parked["id"]) if e["kind"] == "pr_merged"]
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["merged_at"] == "2026-08-02T10:00:00Z"
    assert not any(e["kind"] == "marked_done" for e in store.list_events(parked["id"]))


def test_an_open_pr_is_left_exactly_where_it_was(started, project, fake_gh, parked):
    """The common case: one `gh` call, no write, no attention."""
    fake_gh.set_pr(PR, "OPEN")
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []


def test_a_closed_pr_asks_for_the_user(started, project, fake_gh, parked):
    """Closed without merging means the delivered work was refused — that needs them."""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["status"] == "needs_review"
    assert row["pr_state"] == "CLOSED"
    assert row["needs_attention"]
    assert row["attention_reason"] == PR_CLOSED_BLOCKER
    assert true_blockers(store, row) == [PR_CLOSED_BLOCKER]
    assert any(e["kind"] == "pr_closed" for e in store.list_events(parked["id"]))


def test_the_closed_pr_reason_survives_the_reconciler(started, project, fake_gh,
                                                      parked):
    """INV-ATTENTION-REASON rewrites any reason `true_blockers` cannot derive.

    Without the PR_CLOSED_BLOCKER branch this work order would keep its status but be
    relabelled "finished without a completion signal — review the session", sending the
    user to read a worker session that did nothing wrong.
    """
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)
    poll(started, store)

    violations = list(check_project(store))

    assert [v.invariant for v in violations] == []
    assert store.get_work_order(parked["id"])["attention_reason"] == PR_CLOSED_BLOCKER


def test_a_closed_pr_is_still_the_users_to_close(started, project, fake_gh, parked):
    """Refused work is not failed work: the ordinary exits still apply."""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)
    poll(started, store)

    out = ops.mark_done(parked["id"])

    assert out["status"] == "completed"
    assert not ProjectStore(project).get_work_order(parked["id"])["needs_attention"]


def test_nothing_parked_means_no_subprocess(started, project, fake_gh):
    """A fleet with no open pull requests must not pay for this step at all."""
    ops.create_work_order("proj_a", "still going")
    ops.finish(ops.create_work_order("proj_a", "no PR here")["id"], "done")

    poll(started, ProjectStore(project))

    assert fake_gh.calls == []


def test_a_merged_pr_closes_the_backlog_item_behind_it(started, project, fake_gh):
    """Promotion links the two; only the ending was missing."""
    central = CentralStore()
    item = central.add_backlog("proj_a", "add feature X")
    wo = ops.promote_backlog(item["id"])
    ops.finish(wo["wo_id"], "opened a PR", pr_url=PR)
    fake_gh.set_pr(PR, "MERGED")
    store = ProjectStore(project)

    poll(started, store)

    row = central.get_backlog(item["id"])
    assert row is not None and row["status"] == "done"


def test_an_unreadable_gh_warns_once_and_leaves_the_work_order_parked(
        started, project, fake_gh, parked):
    """Silence would leave a dead feature the user never learns about; every poll would
    make the inbox unreadable. Once per daemon run is the whole of it."""
    fake_gh.fail("gh: not authenticated")
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)

    warnings = [n for n in store.unrouted_notifications() if n["source"] == "pr-poll"]
    assert len(warnings) == 1
    assert "auto-complete on merge is off" in warnings[0]["title"]
    assert "GH_TOKEN" in warnings[0]["body"]
    assert store.get_work_order(parked["id"])["status"] == "waiting_pr_merge"


def test_one_unreadable_pr_does_not_hide_the_others(started, project, fake_gh, parked):
    """A deleted repo or a typo'd URL is one work order's problem, not the project's."""
    other = ops.create_work_order("proj_a", "add feature Y")
    ops.finish(other["id"], "opened a PR", pr_url="https://github.com/acme/proj/pull/8")
    fake_gh.set_pr("https://github.com/acme/proj/pull/8", "MERGED")  # PR itself unknown
    store = ProjectStore(project)

    poll(started, store)

    assert store.get_work_order(other["id"])["status"] == "completed"
    assert store.get_work_order(parked["id"])["status"] == "waiting_pr_merge"


def test_the_poll_runs_on_its_own_cadence(started, project, fake_gh, parked):
    """Every tick would be four times the API calls for no perceptible gain."""
    fake_gh.set_pr(PR, "OPEN")

    started.tick_count = 0
    started.tick()                       # tick 1: polls
    assert len(fake_gh.calls) == 1
    for _ in range(PR_POLL_EVERY_TICKS - 1):
        started.tick()
    assert len(fake_gh.calls) == 1       # ... and not again until the cadence comes up
    started.tick()
    assert len(fake_gh.calls) == 2


def test_a_merge_notifies_nobody(started, project, fake_gh, parked):
    """`route_new_inbox` has no level filter, so any row here Telegrams the user — about
    a merge they just performed. `jarvis wo done` is silent; so is this."""
    fake_gh.set_pr(PR, "MERGED")
    store = ProjectStore(project)

    poll(started, store)

    assert [n for n in store.unrouted_notifications() if n["source"] == "pr-poll"] == []
    assert store.get_work_order(parked["id"])["status"] == "completed"


# -- reading GitHub -----------------------------------------------------------------


def test_pr_view_parses_the_state(fake_gh):
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-02T10:00:00Z")

    pr = github.pr_view(PR)

    assert pr.merged and not pr.closed_unmerged
    assert pr.merged_at == "2026-08-02T10:00:00Z"
    assert github.pr_view(PR).state == "MERGED"


def test_pr_view_raises_rather_than_guessing(fake_gh):
    """An unreadable pull request must never look like an unmerged one: guessing OPEN
    parks a merged work order forever, guessing CLOSED demands the user for nothing."""
    fake_gh.fail("HTTP 401: Bad credentials")

    with pytest.raises(github.GitHubError) as e:
        github.pr_view(PR)
    assert "Bad credentials" in str(e.value)


def test_the_isolation_gate_stops_a_test_reaching_real_github():
    """No `fake_gh` fixture: the blocked stub must answer, not the real `gh`."""
    with pytest.raises(github.GitHubError):
        github.pr_view(PR)


# -- the PR title prefix ------------------------------------------------------------


@pytest.mark.parametrize("command", [
    'gh pr create --title "Add feature X" --body-file /tmp/b.md',
    "gh pr create --title='Add feature X'",
    'gh pr create -t "Add feature X"',
    'cd /tmp/wt && gh pr create --title "Add feature X"',
    # gh is often not on a worker's PATH, so an absolute path is the normal invocation
    '/snap/bin/gh pr create --title "Add feature X"',
])
def test_gh_pr_create_without_the_prefix_is_denied(command):
    out = hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-1234abcd"},
    )
    assert out is not None
    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    # the fix has to be one copy-paste, so the exact required title is in the reason
    assert '--title "[wo-1234abcd] Add feature X"' in hook["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    'gh pr create --title "[wo-1234abcd] Add feature X" --body-file /tmp/b.md',
    "gh pr create --title='[wo-1234abcd] Add feature X'",
])
def test_a_prefixed_title_passes(command):
    assert hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-1234abcd"},
    ) is None


@pytest.mark.parametrize("command", [
    "gh pr create --fill",                      # no title of ours to judge
    "gh pr list --state open",                  # not a create
    "gh pr merge 7",                            # a gate's business, not this hook's
    'git commit -m "gh pr create --title x"',   # the words, not the command
    'echo "unbalanced',                         # unparseable: never our call
])
def test_the_hook_keeps_out_of_everything_else(command):
    assert hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-1234abcd"},
    ) is None


def test_interactive_sessions_are_untouched():
    """No JARVIS_WO_ID: a human in a managed repo has no work order to name."""
    assert hooks.preflight_decision(
        {"tool_name": "Bash",
         "tool_input": {"command": 'gh pr create --title "Add feature X"'}},
        {},
    ) is None


def test_the_worker_briefing_names_the_required_prefix(catalog_file):
    """A rule enforced only by a hook is a trap; the contract has to state it first."""
    from jarvis.dispatch import build_worker_prompt

    prompt = build_worker_prompt(
        {"id": "wo-test", "title": "add feature X", "description": "d"},
        load_catalog(catalog_file).projects[0], knowledge=[])

    assert "[wo-test] " in prompt
    assert "--pr <url>" in prompt


def test_the_operation_contract_names_it_too(started, project):
    """OPERATION.md is what a worker reads when the briefing is not in front of it."""
    contract = (project / "OPERATION.md").read_text()

    assert "[$JARVIS_WO_ID]" in contract
    assert "--pr <url>" in contract
