"""Gates end to end: worker attempts to ship → Neo reviews → the retry goes through.

`test_gates.py` pins the pieces. This drives the whole loop through the real daemon,
the real Neo drain and the real dispatch path, because the value of this feature is
entirely in the hand-offs between them — a gate that files a request nobody delivers a
verdict for is a wall with extra steps.
"""

from __future__ import annotations

import json

import pytest

from jarvis import gates, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.hooks import preflight_decision
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore


@pytest.fixture()
def gated_catalog(tmp_path, project):
    """A catalog whose only project gates every privileged action."""
    data = {
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{
            "name": "proj_a",
            "path": str(project),
            "description": "test project",
            "gates": {"enabled": list(gates.KIND_NAMES)},
        }],
    }
    path = tmp_path / "catalog-gated.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def fleet(jarvis_home, fake_claude, gated_catalog, project):
    """A started OS with one gated project and one dispatched work order."""
    ops.start_os(str(gated_catalog), foreground=True)
    daemon = Daemon(load_catalog(gated_catalog))
    wo = ops.create_work_order("proj_a", "ship version 1.2.3",
                               description="cut and deploy the release")
    daemon.tick()  # dispatch it

    class Handle:
        def __init__(self):
            self.daemon = daemon
            self.wo_id = wo["id"]
            self.project = project

        def store(self):
            return ProjectStore(project)

        def env(self):
            """The worker's environment, as dispatch actually wrote it."""
            settings = json.loads(
                (project / ".jarvis" / "worker-settings" / f"{wo['id']}.json").read_text()
            )
            return settings["env"]

        def attempt(self, command):
            return preflight_decision(
                {"tool_name": "Bash", "tool_input": {"command": command},
                 "cwd": str(project)}, self.env())

        def approval(self):
            store = self.store()
            try:
                rows = store.list_approvals(self.wo_id)
                return rows[0] if rows else None
            finally:
                store.close()

    return Handle()


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


# -- dispatch hands the gate config to the worker ------------------------------------


def test_dispatch_passes_the_gate_config_to_the_worker(fleet):
    """The hook runs per Bash command and reads the config from env, so dispatch has to
    put it there — otherwise every gate is silently off in the one place it matters."""
    env = fleet.env()
    assert "JARVIS_GATES" in env
    config = gates.GateConfig.from_json(env["JARVIS_GATES"])
    assert config.enabled == frozenset(gates.KIND_NAMES)


def test_ungated_project_gets_an_empty_gate_config(jarvis_home, fake_claude,
                                                   catalog_file, project):
    """The default catalog has no `gates` key at all; workers must notice nothing."""
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "ordinary work")
    daemon.tick()

    settings = json.loads(
        (project / ".jarvis" / "worker-settings" / f"{wo['id']}.json").read_text())
    assert not gates.GateConfig.from_json(settings["env"]["JARVIS_GATES"])


def test_worker_is_briefed_that_shipping_is_reachable(fleet):
    """A worker told only "you may not ship" gives up and writes a note instead of
    asking. The briefing has to say the gate exists and how to use it."""
    from jarvis.catalog import load_catalog as _load
    from jarvis.dispatch import build_worker_prompt

    catalog = _load(fleet.daemon.catalog.source_path)
    spec = catalog.project("proj_a")
    store = fleet.store()
    try:
        wo = store.get_work_order(fleet.wo_id)
    finally:
        store.close()

    prompt = build_worker_prompt(wo, spec, knowledge=[])

    assert "gated, NOT forbidden" in prompt
    assert "jarvis gate request" in prompt
    assert "release" in prompt


def test_ungated_project_prompt_says_nothing_about_gates(jarvis_home, catalog_file):
    from jarvis.dispatch import build_worker_prompt

    spec = load_catalog(catalog_file).project("proj_a")
    prompt = build_worker_prompt({"id": "wo-x", "title": "t", "description": "d"},
                                 spec, knowledge=[])
    assert "Privileged actions" not in prompt
    assert "jarvis gate request" not in prompt


# -- the full loop -------------------------------------------------------------------


def test_neo_approves_and_the_retry_goes_through(fleet):
    """The whole point, in one test."""
    # 1. The worker asks properly, making its case. FORCE_APPROVE drives the fake model.
    result = ops.request_gate_approval(
        fleet.wo_id, "./scripts/shipit.sh",
        why="FORCE_APPROVE — all tests pass and the PR is merged",
        evidence="PR #42 merged, 264 tests green",
    )
    assert result["status"] == "pending"

    # 2. Meanwhile the command itself is still blocked.
    assert _decision(fleet.attempt("./scripts/shipit.sh")) == "deny"

    # 3. Neo reviews it.
    fleet.daemon._neo_drain()

    approval = fleet.approval()
    assert approval["status"] == "approved"
    assert approval["decided_by"] == "neo"

    # 4. The worker is told, in a message it will receive as its next turn.
    store = fleet.store()
    try:
        messages = store.queued_messages(fleet.wo_id)
        assert any("APPROVED" in m["content"] for m in messages)
        # And the user is informed a release happened, without having been asked.
        assert store.get_work_order(fleet.wo_id)["needs_attention"] == 0
    finally:
        store.close()

    # 5. The retry runs.
    assert _decision(fleet.attempt("./scripts/shipit.sh")) == "allow"


def test_neo_denies_and_the_command_stays_blocked(fleet):
    ops.request_gate_approval(fleet.wo_id, "git push origin main",
                              why="FORCE_DENY — I would like to skip the PR")
    fleet.daemon._neo_drain()

    approval = fleet.approval()
    assert approval["status"] == "denied"
    assert _decision(fleet.attempt("git push origin main")) == "deny"
    store = fleet.store()
    try:
        assert any("DENIED" in m["content"] for m in store.queued_messages(fleet.wo_id))
    finally:
        store.close()


def test_neo_escalation_hands_the_key_to_the_user(fleet):
    """Neo declining is the safe answer, and must leave the user something to act on."""
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh",
                              why="tests are a bit flaky honestly")
    fleet.daemon._neo_drain()

    approval = fleet.approval()
    assert approval["status"] == "pending"   # still open — the user can still decide
    assert approval["escalated"] == 1

    store = fleet.store()
    try:
        wo = store.get_work_order(fleet.wo_id)
        assert wo["needs_attention"] == 1
        assert "gate approval escalated" in wo["attention_reason"]
    finally:
        store.close()

    # The inbox tells the user exactly which command to run.
    central = CentralStore()
    try:
        bodies = [i["body"] for i in central.unacked_inbox()]
    finally:
        central.close()
    assert any(f"jarvis gate approve {approval['id']}" in b for b in bodies)

    # And the command is still blocked in the meantime.
    assert _decision(fleet.attempt("./scripts/shipit.sh")) == "deny"


def test_user_can_open_an_escalated_gate(fleet):
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="please")
    fleet.daemon._neo_drain()
    approval = fleet.approval()

    ops.decide_gate(approval["id"], verdict="approved", reason="I checked it myself")

    assert _decision(fleet.attempt("./scripts/shipit.sh")) == "allow"
    store = fleet.store()
    try:
        wo = store.get_work_order(fleet.wo_id)
        # Deciding is the response to the escalation: the flag comes down.
        assert wo["needs_attention"] == 0
        assert any("I checked it myself" in m["content"]
                   for m in store.queued_messages(fleet.wo_id))
    finally:
        store.close()
    # Neo's record shows the user answered, so it is not left waiting for review.
    neo = NeoStore()
    try:
        q = neo.get(approval["neo_question_id"])
        assert q["answered_by"] == "user"
        assert q["answer"] == "APPROVED"
    finally:
        neo.close()


def test_user_can_refuse_an_escalated_gate(fleet):
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="please")
    fleet.daemon._neo_drain()
    approval = fleet.approval()

    ops.decide_gate(approval["id"], verdict="denied", reason="not this week")

    assert _decision(fleet.attempt("./scripts/shipit.sh")) == "deny"
    assert fleet.approval()["status"] == "denied"


def test_denial_requires_a_reason(fleet):
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="please")
    approval = fleet.approval()
    with pytest.raises(ops.OpsError, match="needs a reason"):
        ops.decide_gate(approval["id"], verdict="denied", reason="   ")


def test_a_decided_gate_cannot_be_decided_again(fleet):
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh",
                              why="FORCE_APPROVE — ready")
    fleet.daemon._neo_drain()
    approval = fleet.approval()

    with pytest.raises(ops.OpsError, match="already approved"):
        ops.decide_gate(approval["id"], verdict="denied", reason="changed my mind")


# -- the worker's own request path ----------------------------------------------------


def test_requesting_a_command_that_needs_no_approval_is_an_error(fleet):
    """Better than filing a request nobody will act on."""
    with pytest.raises(ops.OpsError, match="does not trip any gate"):
        ops.request_gate_approval(fleet.wo_id, "uv run pytest tests/", why="x")


def test_requesting_in_an_ungated_project_explains_why_not(jarvis_home, fake_claude,
                                                           catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "ordinary work")
    with pytest.raises(ops.OpsError, match="no gates enabled"):
        ops.request_gate_approval(wo["id"], "./scripts/shipit.sh", why="x")


def test_asking_twice_reuses_the_open_request(fleet):
    first = ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="a")
    second = ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="b")
    assert second["approval_id"] == first["approval_id"]
    assert "already under review" in second["note"]


def test_asking_when_already_approved_says_so(fleet):
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh",
                              why="FORCE_APPROVE — ready")
    fleet.daemon._neo_drain()
    again = ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="ready")
    assert again["status"] == "approved"
    assert "run the command as written" in again["note"]


# -- the reconciler must not judge a waiting worker ----------------------------------


def test_worker_waiting_on_a_gate_is_not_filed_as_abandoned(fleet, fake_claude):
    """A worker that files a request and ends its turn, as instructed, goes idle. The
    reconciler used to read that as "finished without `jarvis wo finish`" and hand the
    user a review for a work order that is simply waiting."""
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="a")

    store = fleet.store()
    try:
        session_id = store.get_work_order(fleet.wo_id)["session_id"]
    finally:
        store.close()
    fake_claude.set_session_state(session_id, "done")

    fleet.daemon.tick()

    store = fleet.store()
    try:
        wo = store.get_work_order(fleet.wo_id)
        assert wo["status"] == "waiting_input"
        assert wo["needs_attention"] == 0
        assert "idle without" not in (wo["attention_reason"] or "")
    finally:
        store.close()


def test_status_reports_an_escalated_gate_once_with_the_command(fleet):
    """The work order's own flag and the gate item say the same thing; reporting both
    doubles the attention list and buries the line that can actually be acted on."""
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="unsure")
    fleet.daemon._neo_drain()
    approval = fleet.approval()

    st = ops.os_status()

    mine = [a for a in st["attention"] if a.get("wo_id") == fleet.wo_id]
    assert len(mine) == 1
    assert mine[0]["status"] == "gate_escalated"
    assert mine[0]["approval_id"] == approval["id"]
    # The item has to carry the command that resolves it.
    assert f"jarvis gate approve {approval['id']}" in mine[0]["decide"]
    assert st["gates"]["awaiting_you"] == 1


def test_status_is_quiet_while_a_gate_is_with_neo(fleet):
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh", why="a")

    st = ops.os_status()

    assert st["gates"]["awaiting_you"] == 0
    assert [a for a in st["attention"] if a.get("wo_id") == fleet.wo_id] == []


def test_gate_listing_and_show_surface_the_request(fleet):
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh",
                              why="ready to go", evidence="PR #42")

    rows = ops.list_gates(pending_only=True)
    assert len(rows) == 1
    assert rows[0]["project"] == "proj_a"
    assert rows[0]["kind"] == "release"

    detail = ops.show_gate(rows[0]["id"])
    # The request text the reviewer saw is part of the record, not just a prompt.
    assert "ready to go" in detail["neo_question"]["question"]
    assert "PR #42" in detail["neo_question"]["question"]


# -- false positives: the fourth verdict, end to end ----------------------------------


def test_neo_dismisses_a_false_positive_and_the_command_runs(fleet):
    """The counterpart of `test_neo_approves_and_the_retry_goes_through`, for the case
    where the premise was wrong: the command never performed a privileged action."""
    # A plain grep. It trips the `release` gate only because the deploy script's name
    # appears in the search pattern — this exact shape is what filed the real requests
    # that were denied once and approved once.
    command = "grep -rn shipit.sh src/jarvis/gates.py"
    ops.request_gate_approval(
        fleet.wo_id, command,
        why="FORCE_DISMISS — this greps a file; it performs no privileged action",
    )
    assert _decision(fleet.attempt(command)) == "deny"

    fleet.daemon._neo_drain()

    approval = fleet.approval()
    assert approval["status"] == "dismissed"
    assert approval["decided_by"] == "neo"
    # Nothing was authorised, so nothing has a clock or a budget.
    assert approval["expires_at"] is None

    # The worker is told, and the retry runs.
    store = fleet.store()
    try:
        messages = store.queued_messages(fleet.wo_id)
        assert any("DISMISSED" in m["content"] for m in messages)
        assert store.get_work_order(fleet.wo_id)["needs_attention"] == 0
    finally:
        store.close()
    assert _decision(fleet.attempt(command)) == "allow"


def test_a_dismissal_costs_the_user_no_inbox_item(fleet):
    """Neo's approvals and denials both post to the inbox. A dismissal must not.

    It reports that the OS's own recogniser misfired on a command that ships nothing;
    an inbox item for that spends the user's attention on an OS bug, which is the exact
    cost the gate exists to avoid. The rate is surfaced as a count instead.
    """
    ops.request_gate_approval(fleet.wo_id, "grep -rn shipit.sh src/jarvis/gates.py",
                              why="FORCE_DISMISS — read-only")
    fleet.daemon._neo_drain()

    central = CentralStore()
    try:
        items = central.unacked_inbox()
    finally:
        central.close()

    assert [i for i in items if fleet.wo_id in (i.get("body") or "")] == []
    # ...and it is still counted, because a defect nobody can measure never gets fixed.
    assert ops.os_status()["gates"]["false_positives"] == 1


def test_a_dismissal_by_neo_still_lands_as_an_approval_answer_on_the_record(fleet):
    """`jarvis neo review` reads the answer text, so the third verdict has to appear
    there — otherwise a dismissal reads as a denial in Neo's own history."""
    ops.request_gate_approval(fleet.wo_id, "grep -rn shipit.sh src/jarvis/gates.py",
                              why="FORCE_DISMISS — read-only")
    fleet.daemon._neo_drain()

    neo = NeoStore()
    try:
        q = neo.get(fleet.approval()["neo_question_id"])
    finally:
        neo.close()
    assert q["answer"] == "DISMISSED"


def test_user_can_dismiss_a_gate_the_classifier_got_wrong(fleet):
    command = "grep -rn shipit.sh src/jarvis/gates.py"
    fleet.attempt(command)
    approval = fleet.approval()

    ops.decide_gate(approval["id"], verdict="dismissed",
                    reason="the literal is inside a grep pattern; this reads a file")

    assert _decision(fleet.attempt(command)) == "allow"
    assert fleet.approval()["status"] == "dismissed"
    neo = NeoStore()
    try:
        assert neo.get(approval["neo_question_id"])["answer"] == "DISMISSED"
    finally:
        neo.close()


def test_dismissal_requires_a_reason(fleet):
    """The reason IS the defect report on the recogniser, and the only note attached to
    the false-positive count anyone will later read."""
    fleet.attempt("grep -rn shipit.sh src/jarvis/gates.py")
    approval = fleet.approval()
    with pytest.raises(ops.OpsError, match="needs a reason"):
        ops.decide_gate(approval["id"], verdict="dismissed", reason="  ")


def test_decide_gate_refuses_a_verdict_it_does_not_understand(fleet):
    fleet.attempt("./scripts/shipit.sh")
    approval = fleet.approval()
    with pytest.raises(ops.OpsError, match="unknown verdict"):
        ops.decide_gate(approval["id"], verdict="probably", reason="hmm")
    assert fleet.approval()["status"] == "pending"


def test_the_false_positive_rate_is_reportable_across_the_fleet(fleet):
    """The number the WO asks for: how often the gate fires on nothing.

    It is the signal for whether the recognisers are improving, so it has to survive
    both the expiry sweep and a mix of other verdicts in the same table.
    """
    fleet.attempt("grep -rn shipit.sh src/jarvis/gates.py")
    ops.decide_gate(fleet.approval()["id"], verdict="dismissed", reason="a grep")
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh",
                              why="FORCE_APPROVE — ready")
    fleet.daemon._neo_drain()

    rows = ops.list_gates()
    assert sorted(r["status"] for r in rows) == ["approved", "dismissed"]
    assert ops.os_status()["gates"]["false_positives"] == 1
    # list_gates sweeps expiries on the way past; the dismissal must survive it.
    assert [r for r in ops.list_gates() if r["status"] == "dismissed"]


def test_an_older_neo_that_never_heard_of_dismissal_still_ships_a_release(fleet):
    """Backward tolerance, exercised through the real drain rather than the parser.

    Neo's persona ships in the deployed code but its learnings live in the production
    state directory, so a release can leave the two briefly out of step in either
    direction. An old-shaped verdict must still open the gate it always opened.
    """
    ops.request_gate_approval(fleet.wo_id, "./scripts/shipit.sh",
                              why="FORCE_LEGACY_APPROVE — merged and green")
    fleet.daemon._neo_drain()

    assert fleet.approval()["status"] == "approved"
    assert _decision(fleet.attempt("./scripts/shipit.sh")) == "allow"
