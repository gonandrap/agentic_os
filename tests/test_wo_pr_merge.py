"""Work orders that end in a pull request: `waiting_pr_merge`, and the title prefix.

Two halves of the same idea — a PR is where a work order leaves the OS and becomes
something a human has to act on, so the OS has to (a) keep saying so until they act,
and (b) leave the work order id on the artifact they are looking at.

`waiting_pr_merge` is deliberately NOT an attention item: it is a merge queue the user
works through, not a decision blocking the fleet, and the "NEEDS YOU" strip stops being
read the moment everything finished ends up in it.
"""

from __future__ import annotations

import pytest

from jarvis import cli, hooks, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import check_project, true_blockers
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


# -- the PR title prefix ------------------------------------------------------------


@pytest.mark.parametrize("command", [
    'gh pr create --title "Add feature X" --body-file /tmp/b.md',
    "gh pr create --title='Add feature X'",
    'gh pr create -t "Add feature X"',
    'cd /tmp/wt && gh pr create --title "Add feature X"',
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
