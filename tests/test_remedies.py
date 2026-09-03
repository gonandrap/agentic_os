"""The remedy: a closed vocabulary, a mandatory gate, and the daemon that applies it.

§5 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.

WHAT THESE TESTS ARE BUILT TO AVOID, and it is the same trap three times over.

* A NEGATIVE ASSERTION IS GREEN ON A PATH THAT NEVER RAN. "no approval was filed" passes
  when the supervisor never ran at all, "no message was queued" passes when the remedy
  never ran, and `{"decision": "propose"}` already yields `failed` on the tree this
  section starts from, because `_validate` refused anything outside `{ack, escalate}`.
  So every refusal here is asserted IN THE SAME TEST as the positive it is the opposite
  of, and every "nothing happened" is paired with a patched acting function whose call
  count is asserted to be zero and then, in the same fixture, to be one.
* `needs_attention == 0` GRADES NOTHING: `ProjectStore.clear_attention` reaches it too,
  and that is the exact regression the nudge path forbids. What discriminates is a
  re-derivable blocker surviving in `acknowledged_blockers`, which only
  `ops.ack_attention` writes.
* A test that reaches the daemon without arming remedies exercises the disabled path and
  still gets a perfectly good result. `_arm` is called explicitly in every test that
  wants it, never from a fixture.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from jarvis import catalog, db, gates, ops, remedies, supervisor
from jarvis.project_store import ProjectStore

# The alarm-raising and drain helpers are §2's, reused verbatim so this section is
# tested against the rows the real raiser produces rather than hand-written ones.
from test_supervisor import _alarm, _burning, _drain, _supervisor_calls


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """`test_supervisor.started`, redeclared: a fixture is module-scoped in pytest and
    only the helper FUNCTIONS above cross the file boundary."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    return lambda: Daemon(load_catalog(catalog_file))


def _arm(catalog_file: Path, *allowed: str, enabled: bool = True) -> None:
    """Turn the supervisor on AND arm remedies. Explicit in every test that wants it."""
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True,
                                "remedies": {"enabled": enabled,
                                             "allowed": list(allowed)}}
    catalog_file.write_text(json.dumps(data))


def _store(wo_id: str) -> ProjectStore:
    return ProjectStore(ops.find_work_order(wo_id)[1])


def _approvals(wo_id: str, **kw) -> list[dict]:
    store = _store(wo_id)
    try:
        return store.list_approvals(wo_id, **kw)
    finally:
        store.close()


def _neo_questions() -> list[dict]:
    from jarvis.neo_store import NeoStore

    store = NeoStore()
    try:
        return store.list_questions()
    finally:
        store.close()


def _proposed(started, catalog_file, monkeypatch, tmp_path, token, *allowed):
    """One work order carrying a live alarm the supervisor has just proposed a remedy on.

    Driven through the REAL path — the fake `claude` answers a real supervisor review —
    because the contract under test is what the daemon does with the verdict, and a
    hand-written `wo_alarms` row would prove nothing about `_validate` or `_apply`.
    """
    _arm(catalog_file, *allowed)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path, description=token)
    _drain(daemon)
    return daemon, wo_id


# -- 1. the registry is closed ---------------------------------------------------------


def test_the_registry_is_closed_and_shipped_with_exactly_two():
    """Adding a remedy must be a test-breaking, reviewed act rather than a prompt edit.

    Both directions in one test: an id that is not there raises, an id that is returns
    the row, and the whole vocabulary equals the shipped tuple.
    """
    with pytest.raises(KeyError):
        remedies.get("restart-the-daemon")

    nudge = remedies.get("nudge")
    assert nudge.id == "nudge"
    assert nudge.headline and nudge.blast
    assert tuple(remedies.REMEDIES) == remedies.SHIPPED_REMEDIES == ("nudge", "unblock")
    # Not a restatement of the line above: it is what makes the registry a REGISTRY
    # rather than one hard-coded action, and it is the property the AST pin below keys
    # its allow-list off.
    assert len({r.apply.__name__ for r in remedies.REMEDIES.values()}) == 2
    assert remedies.REMEDIES["unblock"].subjects == ("work_order",)
    assert set(remedies.REMEDIES["nudge"].subjects) == {"work_order", "feature_order"}


def test_the_catalog_refuses_a_remedy_the_os_does_not_have(tmp_path):
    """An unknown id is a `CatalogError` naming the known ones, `GateConfig.parse`'s rule
    — a typo must not silently leave a permission unset. Paired with the id that IS
    known, since "parse_catalog raised" is also what a broken block does."""
    with pytest.raises(catalog.CatalogError, match="nudge, unblock"):
        catalog.parse_catalog({"os": {"supervisor": {"remedies": {
            "allowed": ["reboot"]}}}, "projects": []})

    cat = catalog.parse_catalog({"os": {"supervisor": {"remedies": {
        "enabled": True, "allowed": ["nudge"]}}},
        "projects": [{"name": "p", "path": str(tmp_path)}]})
    assert cat.os.supervisor.remedies == catalog.RemedyConfig(True, ("nudge",))
    # Field-level inheritance, `_parse_inspect`'s shape: the project said nothing and
    # gets the fleet's answer.
    assert cat.projects[0].supervisor.remedies.allowed == ("nudge",)


def test_the_permission_is_a_safety_key_and_reaches_the_config_console(tmp_path):
    """What the OS may DO is a safety key, like `os.neo.enabled` — and both halves of it
    have to be addressable, or a project can arm a remedy the console cannot show."""
    from jarvis import config_version

    assert "*.supervisor.remedies.*" in catalog.SAFETY_KEYS
    cat = catalog.parse_catalog({"os": {}, "projects": [{"name": "p",
                                                         "path": str(tmp_path)}]})
    resolved = config_version.resolve(cat)
    for path in ("os.supervisor.remedies.enabled", "os.supervisor.remedies.allowed",
                 "projects.p.supervisor.remedies.enabled",
                 "projects.p.supervisor.remedies.allowed"):
        assert path in resolved, path
    assert resolved["os.supervisor.remedies.allowed"] == []


def test_the_self_heal_gate_exists_and_nothing_classifies_into_it():
    """Every other kind exists to catch a command a worker typed; this one is filed
    programmatically. Both halves: it IS a kind, and `classify` returns it for nothing —
    including `gate_rules`' own canary corpus, which is the set of strings the OS
    believes are the most privileged things anyone can run."""
    from jarvis import gate_rules

    assert gates.SELF_HEAL == "self_heal"
    assert "self_heal" in gates.KIND_NAMES
    kind = next(k for k in gates.KINDS if k.name == "self_heal")
    assert kind.conflict_markers == ()

    everything_on = gates.GateConfig.parse(True)
    assert everything_on.enabled == frozenset(gates.KIND_NAMES)
    corpus = [c for _, c in gate_rules.SEED_CANARIES] + [
        "heal al-1a2b: nudge wo-3c4d — where are you?",
        "jarvis gate approve 4 --reason ok",
    ]
    for command in corpus:
        action = gates.classify(command, everything_on)
        assert action is None or action.kind != "self_heal", command
    # The positive partner: the corpus is not simply inert — every canary still gates.
    assert all(gates.classify(c, everything_on) is not None
               for _, c in gate_rules.SEED_CANARIES)


# -- 2. the grant is mandatory ---------------------------------------------------------


@pytest.fixture()
def granted(started, catalog_file, monkeypatch, tmp_path):
    """A proposed `nudge` with its `self_heal` approval sitting `pending`."""
    daemon, wo_id = _proposed(started, catalog_file, monkeypatch, tmp_path,
                              "FORCE_SUPERVISOR_PROPOSE", "nudge")
    (approval,) = _approvals(wo_id)
    assert approval["kind"] == "self_heal" and approval["status"] == "pending"
    return daemon, wo_id, approval["id"]


def _apply_now(wo_id: str, project: str = "proj_a") -> str:
    from jarvis.central_store import CentralStore

    store, central = _store(wo_id), CentralStore()
    try:
        alarm = _alarm(wo_id)
        approval = store.get_approval(int(alarm["remedy_approval_id"]))
        wo = store.get_work_order(wo_id)
        return remedies.apply(store, central, project, approval, alarm, wo)
    finally:
        central.close()
        store.close()


@pytest.mark.parametrize("status", ["pending", "denied", "dismissed", "expired"])
def test_no_grant_no_remedy_and_the_handler_is_never_reached(granted, monkeypatch,
                                                             status):
    """THE CENTRAL SAFETY PROPERTY. `pytest.raises` alone would pass on a `RemedyRefused`
    thrown AFTER the message went out, so the handler is patched and its call count is
    what is asserted — the only evidence that nothing reached the session."""
    _daemon, wo_id, approval_id = granted
    calls: list[tuple] = []
    monkeypatch.setitem(
        remedies.REMEDIES, "nudge",
        remedies.Remedy(**{**vars(remedies.REMEDIES["nudge"]),
                           "apply": lambda *a: calls.append(a) or "ran"}))

    store = _store(wo_id)
    try:
        if status == "expired":
            store.decide_approval(approval_id, "approved", "yes", "test")
            # Genuinely past its window, rather than a `status` column that says so:
            # `usable_grant` re-checks the clock and never trusts the row.
            store.conn.execute("UPDATE approvals SET expires_at=? WHERE id=?",
                               (db.now() - 1, approval_id))
        elif status != "pending":
            store.decide_approval(approval_id, status, "because", "test")
    finally:
        store.close()

    with pytest.raises(remedies.RemedyRefused):
        _apply_now(wo_id)
    assert calls == []
    assert _store_messages(wo_id) == []


def test_an_approved_grant_delivers_exactly_once_and_is_spent(granted, monkeypatch):
    """The positive partner of the four refusals above, in the same fixture: one call,
    and `uses == 1` — which is what proves the grant was SPENT through `gates.open_gate`
    rather than read around."""
    _daemon, wo_id, approval_id = granted
    calls: list[tuple] = []
    monkeypatch.setitem(
        remedies.REMEDIES, "nudge",
        remedies.Remedy(**{**vars(remedies.REMEDIES["nudge"]),
                           "apply": lambda *a: calls.append(a) or "ran"}))

    store = _store(wo_id)
    try:
        store.decide_approval(approval_id, "approved", "go ahead", "test")
    finally:
        store.close()

    assert _apply_now(wo_id) == "ran"
    assert len(calls) == 1

    store = _store(wo_id)
    try:
        assert store.get_approval(approval_id)["uses"] == 1
    finally:
        store.close()
    assert _alarm(wo_id)["status"] == "acked"


def _store_messages(wo_id: str) -> list[dict]:
    store = _store(wo_id)
    try:
        return store.queued_messages(wo_id)
    finally:
        store.close()


# -- 3. the allow-list bites before the gate -------------------------------------------


@pytest.mark.parametrize("enabled,allowed", [(False, ("nudge",)), (True, ())])
def test_a_remedy_the_catalog_forbids_files_nothing_and_says_why(
        started, catalog_file, monkeypatch, tmp_path, fake_claude, enabled, allowed):
    """The user must never be asked to approve something their own catalog forbids. Both
    switches, separately, because `enabled: true` with an empty allow-list is a shipping
    state in its own right and would be lost if one flag stood for both.

    The call count is the positive partner: "no approval was filed" is green when the
    supervisor never ran at all, which is precisely what a mis-parsed catalog produces.
    """
    _arm(catalog_file, *allowed, enabled=enabled)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path,
                     description="FORCE_SUPERVISOR_PROPOSE")
    _drain(daemon)
    assert len(_supervisor_calls(fake_claude)) == 1

    alarm = _alarm(wo_id)
    assert alarm["status"] == "escalated"
    assert alarm["verdict"] == "propose"
    assert "remedies" in (alarm["verdict_reason"] or "")
    assert _approvals(wo_id) == []
    assert not [q for q in _neo_questions() if q["kind"] == "approval"]
    # And the OS said so out loud rather than only writing a column.
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        assert any(r["wo_id"] == wo_id and "cost alarm" in r["title"].lower()
                   for r in central.unacked_inbox())
    finally:
        central.close()


def test_an_armed_remedy_files_one_request_and_leaves_the_work_order_alone(
        started, catalog_file, monkeypatch, tmp_path, fake_claude):
    """The positive partner of the two refusals above.

    `waiting_input` is the defect this asserts against: `gates.file_request` parks a
    running work order, and a worker that never asked for a gate is read as waiting on
    the USER by `jarvis status`, the dashboard and `invariants.true_blockers`.
    """
    daemon, wo_id = _proposed(started, catalog_file, monkeypatch, tmp_path,
                              "FORCE_SUPERVISOR_PROPOSE", "nudge")
    assert len(_supervisor_calls(fake_claude)) == 1

    alarm = _alarm(wo_id)
    assert alarm["status"] == "proposed"
    assert alarm["verdict"] == "propose"
    assert alarm["remedy"] == "nudge"
    assert alarm["remedy_argument"]

    (approval,) = _approvals(wo_id)
    assert approval["kind"] == "self_heal"
    assert approval["status"] == "pending"
    assert approval["command"].startswith(f"heal {alarm['id']}: nudge {wo_id}")
    assert approval["max_uses"] == 1
    assert approval["id"] == alarm["remedy_approval_id"]

    questions = [q for q in _neo_questions() if q["kind"] == "approval"]
    assert len(questions) == 1
    # The remedy in words, not merely the symptom: the reviewer is ruling on the ACTION.
    assert remedies.REMEDIES["nudge"].blast in questions[0]["question"]
    assert questions[0]["id"] == approval["neo_question_id"]

    store = _store(wo_id)
    try:
        assert store.get_work_order(wo_id)["status"] == "running"
        # Nothing has reached the session: a proposal is not an act.
        assert store.queued_messages(wo_id) == []
        assert store.get_work_order(wo_id)["needs_attention"] == 1
    finally:
        store.close()


# -- 4. the acting calls stay inside the handlers ---------------------------------------


def test_the_acting_calls_stay_inside_the_handlers():
    """THE PIN ON THE ACTING MODULE, and it is deliberately NOT an import walk.

    `tests/test_neo_panel.py::test_neo_never_imports_the_panel` walks `ast.Import` only,
    which grades nothing here: `remedies.py` legitimately imports `ops`. Reachability is
    not decidable from an AST; ENCLOSURE is, so the property stated is that every
    acting name in this module is inside one of the registry's own handlers.
    """
    source = Path(remedies.__file__).read_text()
    tree = ast.parse(source)
    forbidden = {"send_message", "queue_message", "unblock_work_order", "cancel",
                 "cancel_work_order", "set_status"}
    handlers = {r.apply.__name__ for r in remedies.REMEDIES.values()}

    enclosing: dict[ast.AST, str] = {}

    def walk(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(child, ast.FunctionDef) else fn
            enclosing[child] = here
            walk(child, here)

    walk(tree, "")
    found = []
    for node, fn in enclosing.items():
        name = (node.attr if isinstance(node, ast.Attribute)
                else node.id if isinstance(node, ast.Name) else None)
        if name in forbidden:
            found.append((name, fn))

    assert found, "the walk found no acting names at all — it would pass on any module"
    for name, fn in found:
        assert fn in handlers, f"{name} is called from {fn!r}, not from a handler"


def test_the_pin_would_catch_the_move_it_forbids():
    """A guard nobody has ever seen fail is a guard nobody knows works. The same shape,
    over source that acts from outside a handler."""
    tree = ast.parse("from . import ops\n"
                     "def helper(wo):\n    ops.queue_message(wo, 'hi')\n")
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert fn.name == "helper"
    assert "queue_message" in {n.attr for n in ast.walk(fn)
                               if isinstance(n, ast.Attribute)}


def test_the_supervisors_own_pin_is_untouched_and_still_passes():
    """§5 widens what the OS may do and changes NOTHING about what `supervisor.py` may
    name. Re-run here as well as in its own file so a diff that edits both is caught by
    the one that is about the boundary."""
    tree = ast.parse(Path(supervisor.__file__).read_text())
    forbidden = {"cancel", "cancel_work_order", "set_status", "send_message",
                 "queue_message"}
    named = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    named |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (named & forbidden), sorted(named & forbidden)
    # And the persona's first line, which `testing.py`'s fake and
    # `test_supervisor._supervisor_calls` both match on: rewriting it makes every
    # `_supervisor_calls(...) == []` assertion in the repository vacuously true.
    assert supervisor.SUPERVISOR_PERSONA.splitlines()[0] == (
        "You are the SUPERVISOR inside the Jarvis agentic OS.")


# -- the verdict shapes ----------------------------------------------------------------


def test_a_propose_naming_no_remedy_fails_while_a_valid_one_proposes(
        started, catalog_file, monkeypatch, tmp_path, fake_claude):
    """BOTH HALVES IN ONE TEST, because the failing half is green on an empty diff:
    `_validate` refused every decision outside `{ack, escalate}` before this section, so
    `{"decision": "propose", "remedy": "reboot"}` already left `failed` with no approval
    and the flag up. Only the valid half grades the change."""
    _arm(catalog_file, "nudge")
    daemon = started()
    bad = _burning(daemon, monkeypatch, tmp_path, title="the bad one",
                   description="FORCE_SUPERVISOR_BAD_REMEDY")
    good = _burning(daemon, monkeypatch, tmp_path, title="the good one",
                    description="FORCE_SUPERVISOR_PROPOSE")
    _drain(daemon)
    assert len(_supervisor_calls(fake_claude)) == 2

    bad_alarm = _alarm(bad)
    assert bad_alarm["status"] == "failed"
    assert bad_alarm["verdict"] is None
    assert "reboot-the-daemon" in bad_alarm["verdict_reason"]
    assert _approvals(bad) == []

    good_alarm = _alarm(good)
    assert good_alarm["status"] == "proposed"
    assert good_alarm["verdict"] == "propose"
    assert len(_approvals(good)) == 1

    for wo_id in (bad, good):
        store = _store(wo_id)
        try:
            assert store.get_work_order(wo_id)["status"] == "running"
            assert store.get_work_order(wo_id)["needs_attention"] == 1
        finally:
            store.close()


def test_the_second_remedy_really_cuts_a_dead_edge_and_only_a_dead_one(
        started, catalog_file, monkeypatch, tmp_path):
    """`unblock` is what makes the registry a registry rather than one action wearing a
    dict, so it is exercised for real rather than asserted about.

    Both directions in one test: an edge to a CANCELLED dependency is cut, and an edge to
    a live one in the same work order is not — which is the `drop_all` this path may
    never reach.
    """
    from jarvis.central_store import CentralStore

    _arm(catalog_file, "unblock")
    started()
    dead = ops.create_work_order("proj_a", "the cancelled one")
    live = ops.create_work_order("proj_a", "the live one")
    blocked = ops.create_work_order("proj_a", "the stranded one",
                                    depends_on=[dead["id"], live["id"]])
    ops.cancel(dead["id"])

    store, central = _store(blocked["id"]), CentralStore()
    try:
        alarm = store.add_alarm(blocked["id"], "long-turn", 1, "stranded")
        approval = store.add_approval(blocked["id"], "self_heal",
                                      f"heal {alarm['id']}: unblock {blocked['id']} — x",
                                      max_uses=1)
        store.update_alarm(alarm["id"], status="proposed", verdict="propose",
                           remedy="unblock", remedy_approval_id=approval["id"])
        store.decide_approval(approval["id"], "approved", "go on", "test")
        result = remedies.apply(store, central, "proj_a",
                                store.get_approval(approval["id"]),
                                store.get_alarm(alarm["id"]),
                                store.get_work_order(blocked["id"]))
        assert dead["id"] in result
        assert [d["id"] for d in store.unfinished_dependencies(blocked["id"])] == [
            live["id"]]
        assert store.get_alarm(alarm["id"])["status"] == "acked"
    finally:
        central.close()
        store.close()


def test_a_feature_subject_is_nudged_through_its_carrier(started, catalog_file):
    """A feature order has no session, so §1's `carrier_for_feature` is what the nudge
    speaks through — the REAL one, since §1 landed before this did.

    Both rungs: a feature with a carrier gets the message on the CARRIER, and a feature
    with none refuses rather than raising, which is the difference between "there was
    nothing to speak to" and a bug.
    """
    from jarvis.central_store import CentralStore

    _arm(catalog_file, "nudge")
    started()
    seed = ops.create_work_order("proj_a", "anything, to find the project")
    store, central = _store(seed["id"]), CentralStore()
    try:
        fo_id = store.create_feature_order("the feature")["id"]
        store.set_feature_status(fo_id, "executing")
        store.create_work_order("manage it", parent_id=fo_id, kind="manager")
        carrier = store.carrier_for_feature(fo_id)
        assert carrier is not None, "the fixture gave the feature no carrier"
        alarm = store.add_finding(carrier["id"], kind="stalled", reason="nothing moved",
                                  source="health", probe="stalled",
                                  subject_kind="feature_order", fo_id=fo_id)
        store.update_alarm(alarm["id"], remedy_argument="where are you?")
        alarm = store.get_alarm(alarm["id"])

        assert remedies.REMEDIES["nudge"].apply(
            store, central, "proj_a", store.get_feature_order(fo_id), alarm)
        (queued,) = store.queued_messages(carrier["id"])
        assert queued["source"] == "supervisor"
        assert "where are you?" in queued["content"]

        orphan = dict(alarm, fo_id="fo-nosuchthing")
        assert store.carrier_for_feature("fo-nosuchthing") is None
        with pytest.raises(remedies.RemedyRefused, match="fo-nosuchthing"):
            remedies.REMEDIES["nudge"].apply(
                store, central, "proj_a", {"id": "fo-nosuchthing"}, orphan)
        assert len(store.queued_messages(carrier["id"])) == 1
    finally:
        central.close()
        store.close()


def test_a_remedy_that_does_not_apply_to_this_subject_is_refused():
    """`unblock` is a work-order remedy. The subject check is a pure predicate, so it is
    asserted as one — with the pair that makes it discriminating."""
    assert remedies.subject_kind({"wo_id": "wo-1"}) == "work_order"
    assert remedies.subject_kind(
        {"wo_id": "wo-1", "subject_kind": "feature_order"}) == "feature_order"
    assert "feature_order" not in remedies.REMEDIES["unblock"].subjects
    assert "feature_order" in remedies.REMEDIES["nudge"].subjects


def test_the_gate_reviewer_is_told_not_to_dismiss_a_self_heal_request():
    """A `self_heal` request rides `kind='approval'`, so Neo reads it under
    `gates.REVIEWER_PERSONA` — whose FIRST and highest-priority instruction is to dismiss
    anything that runs no privileged command, which is every proposal this feature will
    ever file. Left alone the feature is inert AND the dismissal path would derive an
    `exempt_pattern` from an intent string. Neo ruled on q224: carve it out at the top.
    """
    persona = " ".join(gates.REVIEWER_PERSONA.split())
    anchor = persona.index("SELF-HEAL REQUEST")
    assert anchor < persona.index("PREMISE CHECK"), \
        "the carve-out must be read before the check it switches off"
    assert "NEVER DISMISS ONE" in persona
    assert "never propose an `exempt_pattern` for one" in persona
    # It is scoped: the other kinds keep every rule, including the one this suite's
    # ordering test pins.
    assert "Everything below is about the other kinds." in persona
    # And it is reachable — the anchor is the literal the request actually opens with.
    question = remedies._request_question(
        "proj_a", "wo-1", remedies.REMEDIES["nudge"], "where are you?", "", "stalled")
    assert question.startswith("SELF-HEAL REQUEST")


def test_the_gate_reviewers_ordering_pin_still_holds_with_the_carve_out():
    """The carve-out is prose inserted above a persona whose clause ORDER is load-bearing
    and pinned in `tests/test_gates.py`. Re-run that pin here so a later edit to my
    section fails in my file too, rather than only in a sibling's."""
    persona = gates.REVIEWER_PERSONA
    assert (persona.index("PREMISE CHECK")
            < persona.index("DISMISS when the command performs no privileged action")
            < persona.index("APPROVE when all of these hold")
            < persona.index("DENY when the request")
            < persona.index("ESCALATE to the user"))


# -- 5. the gate verdict, and the end-to-end pair ---------------------------------------


def test_a_self_heal_verdict_queues_no_worker_message_but_a_pr_merge_still_does(
        granted, monkeypatch, tmp_path):
    """`gates.apply_decision` messages the worker on EVERY verdict, which is right for a
    worker that ran a command and wrong for a session that never asked. BOTH SIDES IN
    ONE TEST: "no message" alone would pass if the guard had disabled messaging outright.
    """
    from jarvis.central_store import CentralStore

    _daemon, wo_id, approval_id = granted
    store, central = _store(wo_id), CentralStore()
    try:
        # Parked by something else entirely. A `self_heal` verdict set no wait, so it
        # must not end one — `end_wait_if_nothing_is_out` is skipped for this kind.
        store.set_status(wo_id, "waiting_input")
        gates.apply_decision(store, approval_id, verdict="denied",
                             reason="not warranted", decided_by="user",
                             central=central, project="proj_a")
        assert store.queued_messages(wo_id) == []
        assert store.get_work_order(wo_id)["status"] == "waiting_input"

        pr = store.add_approval(wo_id, "pr_merge", "gh pr merge 1 --squash")
        gates.apply_decision(store, pr["id"], verdict="approved", reason="ship it",
                             decided_by="user", central=central, project="proj_a")
        queued = store.queued_messages(wo_id)
        assert len(queued) == 1
        assert queued[0]["source"] == "gate"
        assert "gh pr merge 1 --squash" in queued[0]["content"]
        # ...and the shared tail still runs for a real gate: the wait it set is ended.
        assert store.get_work_order(wo_id)["status"] == "running"
    finally:
        central.close()
        store.close()


def test_a_denied_proposal_leaves_the_alarm_with_the_user_and_the_flag_up(granted):
    """The user refused the remedy and the symptom it was for has not gone anywhere."""
    from jarvis.central_store import CentralStore

    _daemon, wo_id, approval_id = granted
    store, central = _store(wo_id), CentralStore()
    try:
        store.clear_attention(wo_id)
        gates.apply_decision(store, approval_id, verdict="denied",
                             reason="I will look myself", decided_by="user",
                             central=central, project="proj_a")
        alarm = store.get_alarm(_alarm(wo_id)["id"])
        assert alarm["status"] == "escalated"
        assert "denied by user" in alarm["verdict_reason"]
        assert "I will look myself" in alarm["verdict_reason"]
        assert store.get_work_order(wo_id)["needs_attention"] == 1
        assert store.events_of_kind(wo_id, "remedy_refused")
    finally:
        central.close()
        store.close()

    rows = [r for r in CentralStore().unacked_inbox() if r["wo_id"] == wo_id]
    assert any("still needs you" in r["title"] for r in rows)


def test_jarvis_gate_deny_does_not_take_the_alarms_flag_back_down(granted):
    """`ops.decide_gate` clears attention after every verdict — the flag an ESCALATED
    gate raised, which the user has just answered. For a `self_heal` denial the flag
    standing afterwards is the ALARM's and is not stale, and nothing re-derives a live
    alarm in `true_blockers`, so clearing it here would put it down for good."""
    _daemon, wo_id, approval_id = granted
    ops.decide_gate(approval_id, "denied", "no", project_name="proj_a")

    store = _store(wo_id)
    try:
        assert store.get_work_order(wo_id)["needs_attention"] == 1
        assert store.get_alarm(_alarm(wo_id)["id"])["status"] == "escalated"
    finally:
        store.close()


def test_the_end_to_end_pair_from_proposal_to_a_delivered_nudge(
        started, catalog_file, monkeypatch, tmp_path):
    """PROPOSE, THEN APPLY, AND `acknowledged_blockers` HOLDING A RE-DERIVED BLOCKER.

    THE OBVIOUS VERSION OF THIS ASSERTION IS UNACHIEVABLE, and `kn-b133acce` is why:
    §5 asks for an EARLIER ack to be "still on the row", but `ops.ack_attention`
    overwrites the column with `true_blockers(store, wo)`, whose last line filters out
    anything already acked — so a pre-existing ack is always either overwritten or
    filtered to `[]`, whatever the code under test does. Measured on this diff: `[]`.

    What discriminates instead is the shape that entry landed on. Give the order a
    blocker `true_blockers` genuinely re-derives (`status='failed'` makes "worker failed
    — review and retry" live), let the remedy run, and require THAT string to be in the
    column afterwards. `ProjectStore.clear_attention` — the forbidden move — leaves it
    NULL, and `needs_attention == 0` is reached identically by both, so the column is
    the only thing that tells them apart.
    """
    daemon, wo_id = _proposed(started, catalog_file, monkeypatch, tmp_path,
                              "FORCE_SUPERVISOR_PROPOSE", "nudge")
    store = _store(wo_id)
    try:
        alarm = store.get_alarm(_alarm(wo_id)["id"])
        assert alarm["status"] == "proposed"
        assert store.queued_messages(wo_id) == []
        assert store.get_work_order(wo_id)["needs_attention"] == 1
        approval_id = int(alarm["remedy_approval_id"])
        store.update_work_order(wo_id, status="failed")
        store.flag_attention(wo_id, "worker failed — review and retry")
    finally:
        store.close()

    ops.decide_gate(approval_id, "approved", "go on then", project_name="proj_a")
    _remedy_tick(daemon)

    store = _store(wo_id)
    try:
        wo = store.get_work_order(wo_id)
        queued = store.queued_messages(wo_id)
        assert len(queued) == 1
        assert queued[0]["source"] == "supervisor"
        assert "Where have you got to" in queued[0]["content"]

        assert store.get_alarm(alarm["id"])["status"] == "acked"
        assert wo["needs_attention"] == 0
        assert wo["acknowledged_blockers"] is not None
        assert "worker failed — review and retry" in json.loads(
            wo["acknowledged_blockers"])

        (applied,) = store.events_of_kind(wo_id, "remedy_applied")
        payload = json.loads(applied["payload"])
        assert payload["remedy"] == "nudge"
        assert wo_id in payload["result"]
        assert store.get_approval(approval_id)["uses"] == 1
        # The grant was spent through the gate's own machinery, so the audit trail says
        # so in the gate's own vocabulary rather than only in the remedy's.
        (opened,) = store.events_of_kind(wo_id, "gate_opened")
        assert json.loads(opened["payload"])["clearance"] == "approved"
    finally:
        store.close()


def _remedy_tick(daemon, timeout: float = 20.0) -> None:
    """One remedy tick, waited out on the daemon's own guard rather than on a sleep."""
    daemon.remedy_tick()
    deadline = time.monotonic() + timeout
    while daemon.remedy_applying and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not daemon.remedy_applying, "the remedy pass never finished"


def test_the_tick_applies_nothing_while_the_gate_is_still_pending(granted):
    """The negative partner of the end-to-end pair, and it needs one: a tick that
    applied nothing because it was never wired up would look identical."""
    daemon, wo_id, _approval_id = granted
    _remedy_tick(daemon)
    assert _store_messages(wo_id) == []
    assert _alarm(wo_id)["status"] == "proposed"


# -- 6. the stale-proposal invariant ----------------------------------------------------


def test_the_invariant_closes_an_abandoned_proposal_and_leaves_a_live_one_alone(
        started, catalog_file, monkeypatch, tmp_path):
    """ONE TEST, TWO ALARMS. The untouched partner is compared row-to-row with `==`
    before and after: "the other one is still proposed" would pass on a check that
    rewrote every column it touched."""
    from jarvis.invariants import check_project

    _arm(catalog_file, "nudge")
    daemon = started()
    abandoned = _burning(daemon, monkeypatch, tmp_path, title="the abandoned one",
                         description="FORCE_SUPERVISOR_PROPOSE")
    live = _burning(daemon, monkeypatch, tmp_path, title="the live one",
                    description="FORCE_SUPERVISOR_PROPOSE")
    _drain(daemon)

    store = _store(abandoned)
    try:
        gone = _alarm(abandoned)
        assert gone["status"] == "proposed"
        store.conn.execute("DELETE FROM approvals WHERE id=?",
                           (gone["remedy_approval_id"],))
        before = store.get_alarm(_alarm(live)["id"])
        assert before["status"] == "proposed"

        violations = [v for v in check_project(store, repair=True)
                      if v.invariant == "INV-REMEDY-PROPOSAL-STALE"]

        assert [v.context["alarm_id"] for v in violations] == [gone["id"]]
        after = store.get_alarm(gone["id"])
        assert after["status"] == "escalated"
        assert "abandoned" in after["verdict_reason"]
        assert store.get_work_order(abandoned)["needs_attention"] == 1
        assert store.get_alarm(before["id"]) == before
    finally:
        store.close()
