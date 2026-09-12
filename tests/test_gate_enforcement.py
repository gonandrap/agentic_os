"""A gate that holds the worker, not just one tool call.

GitHub issue 44, design in `docs/superpowers/specs/2026-09-12-a-gate-that-holds.md`.
`wo-6e7caf6c` filed two gate requests and reached `needs_review` without ever receiving
either verdict: the deny was one tool error carrying English, and every other way out of
the turn was open.

There are three ways out — end the turn, take a different route, declare yourself done —
and a gate closed on two of them is not closed, so each has its own section here. The
fourth section pins the thing that makes the whole design terminate rather than deadlock:
the Stop hold and the narrowed surface apply to OPPOSITE statuses, because the two are
cleared by opposite things.
"""

from __future__ import annotations

import io
import json

import pytest

from jarvis import gates, ops
from jarvis.hooks import handle_hook, main_hook, preflight_decision
from jarvis.invariants import check_project
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

ALL_GATES = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))
MERGE = "gh pr merge 31 --squash"
RELEASE = "./scripts/shipit.sh"


def _decision(result):
    if result is None:
        return None
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture()
def worker(jarvis_home, fake_claude, catalog_file, project):
    """A running work order in a project with every gate live, plus the two moves a
    worker makes at one: attempting the command, and arguing for it."""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing", description="cut a release")
    store.set_status(wo["id"], "running")
    env = {
        "JARVIS_WO_ID": wo["id"],
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
        "JARVIS_GATES": ALL_GATES.to_json(),
    }

    class Handle:
        def __init__(self):
            self.store, self.wo, self.env, self.project = store, wo, env, project

        def attempt(self, command, tool="Bash", **tool_input):
            payload = {"tool_name": tool, "cwd": str(project),
                       "tool_input": {"command": command, **tool_input}}
            return preflight_decision(payload, env)

        def argue(self, command):
            """The compliant second move — what puts a request in front of a reviewer.

            An attempt alone leaves it `awaiting_case` (`gates.AWAITING_CASE`), which is
            precisely the distinction every test below turns on.
            """
            action = gates.classify(command, ALL_GATES)
            approval = store.latest_approval_for(wo["id"], action.kind, action.command)
            neo = NeoStore()
            try:
                gates.amend_request(store, neo, store.get_work_order(wo["id"]), action,
                                    approval, justification="tests are green",
                                    evidence="PR 1, suite passing")
                gates.queue_for_review(store, neo, "proj_a",
                                       store.get_work_order(wo["id"]), action,
                                       store.get_approval(approval["id"]))
            finally:
                neo.close()
            return store.get_approval(approval["id"])

        def stop(self, **extra):
            return handle_hook({"hook_event_name": "Stop", "session_id": "",
                                "cwd": str(project), **extra}, env)

    yield Handle()
    store.close()


# -- 1. ending the turn ----------------------------------------------------------------


def test_the_turn_cannot_end_on_a_request_nobody_is_reviewing(worker):
    """The whole issue in one assertion: the Stop hook was observational, so a worker
    that ignored "END YOUR TURN NOW" and simply stopped left the request unargued and
    unreviewable."""
    worker.attempt(MERGE)

    blocked = worker.stop()

    assert blocked["decision"] == "block"
    approval = worker.store.held_approvals(worker.wo["id"])[0]
    assert f"request {approval['id']}" in blocked["reason"]
    # The way out has to be in the reason: this is the only text the worker gets.
    assert f'jarvis gate request {worker.wo["id"]} "{MERGE}"' in blocked["reason"]
    # And on the record, because nobody reads the worker's transcript.
    held = [e for e in worker.store.list_events(worker.wo["id"])
            if e["kind"] == "gate_turn_held"]
    assert len(held) == 1
    assert json.loads(held[0]["payload"])["approval_id"] == approval["id"]


def test_the_turn_ends_freely_once_the_case_is_made(worker):
    """The half that keeps this from being a deadlock. A `pending` request is cleared
    by a queued verdict, and `Daemon.deliver_messages` will not deliver one into a turn
    still in flight — so blocking here would hold the turn against the only channel that
    could ever free it."""
    worker.attempt(MERGE)
    worker.argue(MERGE)

    assert worker.stop().get("decision") is None
    assert worker.store.pending_approvals(worker.wo["id"])


def test_the_turn_is_held_once_and_not_in_a_loop(worker):
    """`stop_hook_active` means Claude is already going again because of this hook. A
    worker that ignored the reason twice is better parked than spun."""
    worker.attempt(MERGE)
    assert worker.stop()["decision"] == "block"

    assert worker.stop(stop_hook_active=True).get("decision") is None


def test_an_ordinary_turn_ends(worker):
    assert worker.stop().get("decision") is None


def test_a_settled_work_order_is_not_held(worker):
    """Nothing left to argue for: the worker is being taken down, not walking away."""
    worker.attempt(MERGE)
    worker.store.set_status(worker.wo["id"], "cancelled")

    assert worker.stop().get("decision") is None


def test_the_block_is_actually_printed(worker, monkeypatch, capsys):
    """A Stop block has no `hookSpecificOutput`, so the entry point's old print
    condition would have swallowed it — and a decision nobody prints is advice."""
    worker.attempt(MERGE)
    for key, value in worker.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "Stop", "session_id": "", "cwd": str(worker.project)})))

    assert main_hook() == 0

    assert json.loads(capsys.readouterr().out)["decision"] == "block"


# -- 2. taking a different route -------------------------------------------------------


def test_a_request_under_review_narrows_the_session(worker):
    """`latest_approval_for` matches the command string exactly, so the goal was always
    reachable by another command. Under review, it is not."""
    worker.attempt(MERGE)
    worker.argue(MERGE)

    result = worker.attempt("gh pr create --title x --body y")

    assert _decision(result) == "deny"
    assert "under review" in _reason(result)


def test_the_narrowed_session_can_still_read_and_still_reach_the_os(worker):
    """Reading is how a worker answers a question about its own state, and the contract
    commands are the only channel it has to the OS — including `jarvis wo ask`."""
    worker.attempt(MERGE)
    worker.argue(MERGE)

    assert worker.attempt("cat README.md") is None
    assert _decision(worker.attempt(f'jarvis wo ask {worker.wo["id"]} "now what?"')) \
        == "allow"


def test_the_narrowed_session_cannot_write_files(worker):
    worker.attempt(MERGE)
    worker.argue(MERGE)

    result = preflight_decision(
        {"tool_name": "Write", "cwd": str(worker.project),
         "tool_input": {"file_path": str(worker.project / "x.py"), "content": "x = 1"}},
        worker.env)

    assert _decision(result) == "deny"


def test_a_live_grant_still_runs_while_another_request_is_under_review(worker):
    """The narrowing sits AFTER `gate_decision` for this: a command somebody approved
    must not be blocked by an unrelated request still waiting."""
    worker.attempt(MERGE)
    approved = worker.argue(MERGE)
    gates.apply_decision(worker.store, approved["id"], "approved",
                         "ship it", decided_by="neo")
    worker.attempt(RELEASE)
    worker.argue(RELEASE)

    result = worker.attempt(MERGE)

    assert _decision(result) == "allow"
    assert "approved by neo" in _reason(result)


def test_a_held_request_leaves_the_session_open(worker):
    """The mirror image of the Stop hold, and the reason the design terminates: a case
    is made of test output and a branch, so the surface it comes from stays open for
    exactly as long as the worker still owes one."""
    worker.attempt(MERGE)

    assert worker.attempt("git status") is None
    assert worker.store.held_approvals(worker.wo["id"])


# -- 3. declaring yourself done --------------------------------------------------------


@pytest.mark.parametrize("argue", [False, True])
def test_finish_refuses_while_a_request_is_open(worker, argue):
    """The route neither of the others can close: `jarvis wo finish` is a contract
    command, so the narrowed surface has to keep it allowed."""
    worker.attempt(MERGE)
    if argue:
        worker.argue(MERGE)

    with pytest.raises(ops.OpsError) as e:
        ops.finish(worker.wo["id"], "shipped it")

    # Still parked on the gate, and nothing of the submission was written.
    assert worker.store.get_work_order(worker.wo["id"])["status"] == "waiting_input"
    assert not worker.store.get_work_order(worker.wo["id"])["result_summary"]
    if argue:
        assert "End your turn" in str(e.value)
    else:
        assert "jarvis gate request" in str(e.value)


def test_finish_works_once_the_gate_is_decided(worker):
    worker.attempt(MERGE)
    approval = worker.argue(MERGE)
    gates.apply_decision(worker.store, approval["id"], "denied",
                         "not yet", decided_by="neo")

    ops.finish(worker.wo["id"], "left it for review")

    assert worker.store.get_work_order(worker.wo["id"])["status"] != "running"


# -- 4. the tripwire behind all three --------------------------------------------------


def test_a_held_request_on_a_settled_work_order_is_an_orphan_too(worker):
    """INV-GATE-ORPHAN counted `pending` only. A held request is further from a verdict,
    not closer: no question exists for Neo to answer."""
    worker.attempt(MERGE)
    worker.store.set_status(worker.wo["id"], "completed")

    violations = list(check_project(worker.store, repair=True))

    assert [v for v in violations if v.invariant == "INV-GATE-ORPHAN"]
    assert not worker.store.held_approvals(worker.wo["id"])
