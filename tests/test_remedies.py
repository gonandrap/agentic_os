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
import subprocess
import sys
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


def test_the_first_three_shipped_remedies_are_pinned_by_name_and_order():
    """Adding a remedy must be a test-breaking, reviewed act rather than a prompt edit.

    Both directions in one test: an id that is not there raises, an id that is returns
    the row, and the whole vocabulary equals the shipped tuple.
    """
    with pytest.raises(KeyError):
        remedies.get("restart-the-daemon")

    nudge = remedies.get("nudge")
    assert nudge.id == "nudge"
    assert nudge.headline and nudge.blast
    assert tuple(remedies.REMEDIES) == remedies.SHIPPED_REMEDIES
    # The three this section shipped, in order and first: the self-evolution spec's §4
    # widened the registry behind them and changed nothing about them, so the three names
    # and their order are still the property worth pinning here. The nine-entry closure
    # is graded in `test_the_registry_is_closed_at_nine_...` below.
    assert remedies.SHIPPED_REMEDIES[:3] == ("nudge", "unblock", "file_work_order")
    # Not a restatement of the line above: it is what makes the registry a REGISTRY
    # rather than one hard-coded action, and it is the property the AST pin below keys
    # its allow-list off.
    assert len({r.apply.__name__ for r in remedies.REMEDIES.values()}) == len(
        remedies.REMEDIES)
    assert remedies.REMEDIES["unblock"].subjects == ("work_order",)
    assert set(remedies.REMEDIES["nudge"].subjects) == {"work_order", "feature_order"}
    assert set(remedies.REMEDIES["file_work_order"].subjects) == {"work_order",
                                                                 "feature_order"}


def _waiting_on_slugs() -> set[str]:
    """Every `what` slug `ops.waiting_on` can answer, read off its AST.

    OFF THE SOURCE AND NOT OFF A LIST HERE, `test_the_acting_calls_stay_inside_the_handlers`'
    reason one function along: a copy of the vocabulary in the test file is green the day
    the function's answers and the copy diverge, which is exactly the day a `covers` entry
    starts pointing at a slug nobody answers. The `{"what": wo["status"]}` arm is not a
    literal and its statuses are added by hand — the only dynamic arm, checked by the
    assertion below that every literal arm is still found.
    """
    import ast
    from pathlib import Path

    from jarvis import ops as ops_mod

    tree = ast.parse(Path(ops_mod.__file__).read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "waiting_on")
    slugs = {v.value for node in ast.walk(fn) if isinstance(node, ast.Dict)
             for k, v in zip(node.keys, node.values)
             if isinstance(k, ast.Constant) and k.value == "what"
             and isinstance(v, ast.Constant) and isinstance(v.value, str)}
    assert {"prompt", "pending", "gate_held", "signin"} <= slugs, slugs
    return slugs | {"completed", "cancelled", "failed", "waiting_pr_merge",
                    "needs_review"}


def test_every_covers_slug_is_a_real_blocker_and_no_two_remedies_claim_one():
    """`covers` is the mapping `ops.FIX_MATCHES` used to be, moved ON to the remedy (Neo
    q839). A slug nothing answers means a remedy nothing can reach, and a slug with two
    owners is a registry defect rather than a choice `ops.fix` may make."""
    real = _waiting_on_slugs()
    claimed: dict[str, str] = {}
    for remedy in remedies.REMEDIES.values():
        assert isinstance(remedy.covers, tuple)
        for slug in remedy.covers:
            assert slug in real, f"{remedy.id} covers {slug}, which nothing answers"
            assert slug not in claimed, f"{slug} claimed by {claimed[slug]} and {remedy.id}"
            claimed[slug] = remedy.id
    assert claimed == {"prompt": "nudge", "pending": "unblock"}
    # The empty tuple is a decision and not an omission: it writes code, clears no blocker
    # mechanically, and stays reachable only by being named.
    assert remedies.REMEDIES["file_work_order"].covers == ()


def test_resolve_answers_a_code_match_and_none_for_an_unclaimed_slug():
    """`resolve` is `covering`'s replacement (the user's design addition, superseding Neo
    q839's table-free lookup with the same one-lookup-function seam fo-69ba1cc4's data
    rows will back): a `RemedyMatch` naming the primitive, empty `params`, `source=="code"`
    — and still None for anything unclaimed."""
    nudge = remedies.resolve("prompt")
    assert nudge == remedies.RemedyMatch(remedy="nudge", params={}, source="code")
    unblock = remedies.resolve("pending")
    assert unblock == remedies.RemedyMatch(remedy="unblock", params={}, source="code")
    assert remedies.resolve("gate_held") is None
    assert remedies.resolve("a-slug-shipped-next-year") is None


def test_resolve_raises_when_two_remedies_claim_the_same_slug(monkeypatch):
    """Paired with the green registry above: the guard is asserted to BITE, or the test
    that says the registry is clean is grading nothing."""
    clash = remedies.Remedy(**{**vars(remedies.REMEDIES["file_work_order"]),
                               "covers": ("prompt",)})
    monkeypatch.setitem(remedies.REMEDIES, "file_work_order", clash)
    with pytest.raises(ValueError, match="prompt"):
        remedies.resolve("prompt")


def test_resolve_raises_when_a_row_names_a_primitive_absent_from_remedies(monkeypatch):
    """The row/primitive split, ahead of fo-69ba1cc4's data rows actually arriving: a slug
    that resolves to an id `REMEDIES` does not have is a malformed rule and must not come
    back as a usable `RemedyMatch` — that would fail later, further from the cause."""
    ghost = remedies.Remedy(**{**vars(remedies.REMEDIES["unblock"]),
                               "id": "no-such-primitive", "covers": ("pending",)})
    monkeypatch.setitem(remedies.REMEDIES, "unblock", ghost)
    with pytest.raises(ValueError, match="no-such-primitive"):
        remedies.resolve("pending")


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
                           "apply": lambda *a, **kw: calls.append(a) or "ran"}))

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
                           "apply": lambda *a, **kw: calls.append(a) or "ran"}))

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


# -- 3b. a finding raised by an INVARIANT travels the same intake -----------------------


def test_an_invariant_finding_carries_an_undeclared_delivery_to_the_nudge(
        started, catalog_file, project, fake_claude):
    """Spec §2c of
    docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md.

    STARTS AT `invariants.check_project`, not at a hand-written alarm row: the promise is
    that a worker which pushed past a refusal without finishing gets NUDGED, and the only
    thing that can prove it is the whole path — the detector raises a `source='invariant'`
    finding, the supervisor's real intake claims and judges it, and the judge's `nudge`
    reaches `remedies.propose`. A test that filed the finding and called `propose` itself
    grades `propose`, which §5 already grades.
    """
    from jarvis import invariants
    from test_invariants import DELIVERED, PUSHED, _refused_then_pushed

    _arm(catalog_file, "nudge")
    daemon = started()
    store = ProjectStore(project)
    try:
        wo = _refused_then_pushed(store, head=PUSHED, judged=DELIVERED,
                                  description="FORCE_SUPERVISOR_PROPOSE")
        raised = [v for v in invariants.check_project(store)
                  if v.invariant == "INV-UNDECLARED-DELIVERY"]
        assert [v.wo_id for v in raised] == [wo["id"]]
        (finding,) = [a for a in store.alarms_of(wo["id"])
                      if a["kind"] == "undeclared_delivery"]
        assert finding["source"] == "invariant"
        assert finding["status"] == "raised"
    finally:
        store.close()

    _drain(daemon)
    assert len(_supervisor_calls(fake_claude)) == 1

    alarm = _alarm(wo["id"])
    assert alarm["status"] == "proposed"
    assert alarm["remedy"] == "nudge"

    (approval,) = _approvals(wo["id"])
    assert approval["kind"] == "self_heal"
    assert approval["status"] == "pending"
    assert approval["command"].startswith(f"heal {alarm['id']}: nudge {wo['id']}")

    questions = [q for q in _neo_questions()
                 if q["kind"] == "approval" and q["wo_id"] == wo["id"]]
    assert len(questions) == 1
    assert questions[0]["id"] == approval["neo_question_id"]


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
                 "cancel_work_order", "set_status", "create_work_order",
                 # The eight acts §4 of the self-evolution spec added. WIDENED AND NEVER
                 # LOOSENED: every one of them moves a head, a flag, a gate or a verdict
                 # on a work order nobody asked the OS to touch, which is the whole
                 # population this pin exists for.
                 "update_branch", "abandon_approval", "flag_attention",
                 "clear_attention", "force_validation", "carry_merge_chain",
                 "record_base_update", "record_base_update_failed"}
    handlers = {r.apply.__name__ for r in remedies.REMEDIES.values()}
    #: THE ONE EXEMPTION, as a (name, function) PAIR rather than a name, so it cannot
    #: widen to `flag_attention` anywhere else in the module. `_flag_and_tell` is the
    #: REFUSAL path — a reviewer denied the remedy, so the symptom goes back in front of
    #: the user — and it predates §4 by a whole section. It is not an act a rule can name:
    #: no `Remedy.apply` reaches it, and `raise_attention`, which IS the rule-nameable
    #: version of the same call, is pinned to its own handler by the loop below.
    allowed_pairs = {("flag_attention", "_flag_and_tell")}

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
        if (name, fn) in allowed_pairs:
            continue
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
            store, central, "proj_a", store.get_feature_order(fo_id),
            remedies.Intent.from_alarm(store, alarm))
        (queued,) = store.queued_messages(carrier["id"])
        assert queued["source"] == "supervisor"
        assert "where are you?" in queued["content"]

        orphan = dict(alarm, fo_id="fo-nosuchthing")
        assert store.carrier_for_feature("fo-nosuchthing") is None
        with pytest.raises(remedies.RemedyRefused, match="fo-nosuchthing"):
            remedies.REMEDIES["nudge"].apply(
                store, central, "proj_a", {"id": "fo-nosuchthing"},
                remedies.Intent.from_alarm(store, orphan))
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
    """PROPOSE, THEN APPLY, AND THE USER'S OWN BLOCKER LEFT STANDING.

    §5 asked for an earlier ack to be "still on the row", which `ops.ack_attention`
    could never deliver (kn-b133acce). Issue 573 settled it a level up: applying a
    remedy settles the ALARM and must acknowledge NOTHING on the user's behalf, so the
    ack path is `ops.ack_os_flag`.

    Give the order a blocker `true_blockers` genuinely re-derives (`status='failed'`
    makes "worker failed — review and retry" live), let the remedy run, and require it
    to be STILL FLAGGED with the column still NULL. `ack_attention` would lower the flag
    and record a dismissal the user never made; `clear_attention` would NULL the column
    and discard the dismissals they did.
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
        assert wo["needs_attention"] == 1
        assert wo["attention_reason"] == "worker failed — review and retry"
        assert wo["acknowledged_blockers"] is None
        assert store.events_of_kind(wo_id, "acknowledged") == []

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


# -- 6. §4 of the self-evolution spec: the parameter schema and the precondition --------
#
# docs/superpowers/specs/2026-09-27-self-evolution.md §4. The six new primitives each WRAP an
# existing call rather than reimplementing one, so what is graded here is the seam the
# rest of the feature stands on — `params` (Neo 904), `can_apply`, and the closed tables
# that keep both honest. THE FIVE ACTING HANDLERS ARE TESTED BELOW TOO
# (`_apply_carry_verdict`, `_apply_update_branch`, `_apply_drop_hold`,
# `_apply_lower_attention`, `_apply_force_rejudge`): each is a fresh copy of the daemon's
# own orchestration (review round 1, assumption [4]) and not a thin pass-through to code
# already covered by `tests/test_rejudge_moved_head.py` or the catch-up suite, so a test
# that stopped at the seam would leave every one of them unexercised.

PR_URL = "https://github.com/acme/proj/pull/7"
JUDGED_SHA = "aaaa1111aaaa2222aaaa3333aaaa4444aaaa5555"
MOVED_SHA = "bbbb1111bbbb2222bbbb3333bbbb4444bbbb5555"


def _wo(project: Path, **cols) -> tuple[ProjectStore, dict]:
    """A work order written STRAIGHT into the project store, with no daemon and no CLI.

    Every `can_apply` predicate is read-only and local by contract, so the cheapest
    subject that satisfies the contract is the right fixture for it: a row. The one test
    that needs the fleet (a registered project, a catalog on disk) boots one and says why.
    """
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X")
    if cols:
        store.update_work_order(wo["id"], **cols)
    return store, store.get_work_order(wo["id"])


def _judged(store: ProjectStore, wo_id: str, sha: str = JUDGED_SHA) -> None:
    """One settled, passed round on `sha` — what `ProjectStore.validated_head` reads."""
    row = store.open_validation_round(wo_id=wo_id, fingerprint="fp1", round=1)
    store.set_validation_head(row["id"], sha)
    store.close_validation_round(row["id"], "passed", "")


def _head_moved(store: ProjectStore, wo_id: str, head: str = MOVED_SHA) -> None:
    """The newest head the OS RECORDED ITSELF. `can_apply` may not ask GitHub, so a
    locally recorded head is the only one it can compare a verdict against."""
    from jarvis import invariants

    store.add_event(wo_id, invariants.PR_BASE_UPDATED_EVENT,
                    {"pr_url": PR_URL, "base": "main", "base_sha": "cccc",
                     "head_before": JUDGED_SHA, "head_after": head,
                     "checks": [], "cause": "behind"})


def test_the_registry_is_closed_at_nine_and_every_new_one_takes_a_work_order():
    """§4 widens the vocabulary in ONE reviewed diff, and the closure is what makes that
    true (kn-6c252734). NINE, not ten: `retry_neo_question` is in the spec's table and is
    DROPPED on Neo 908 and filed on the backlog, so a registry of ten would mean somebody
    built it without the review that decision asked for."""
    assert tuple(remedies.REMEDIES) == remedies.SHIPPED_REMEDIES
    assert len(remedies.SHIPPED_REMEDIES) == 9
    assert "retry_neo_question" not in remedies.REMEDIES
    new = ("update_branch", "carry_verdict", "force_rejudge", "lower_attention",
           "drop_hold", "raise_attention")
    for remedy_id in new:
        remedy = remedies.get(remedy_id)
        assert remedy.headline and remedy.blast
        assert remedy.subjects == ("work_order",)
    # Still one handler each: the registry is a REGISTRY rather than nine names for one
    # action, and that is the property the AST pin keys its allow-list off.
    assert len({r.apply.__name__ for r in remedies.REMEDIES.values()}) == 9


def test_the_three_shipped_remedies_keep_the_permissive_default():
    """The seam must not change what already ships. The first three take no parameters
    and refuse nothing in advance — their refusals stay in `apply`, where they were."""
    for remedy_id in ("nudge", "unblock", "file_work_order"):
        remedy = remedies.get(remedy_id)
        assert remedy.params == ()
        assert remedy.can_apply(None, {"id": "wo-1"}, {}) == ""
        assert remedy.check_params({}) == ""


def test_every_param_type_is_a_key_of_the_closed_table_and_names_are_unique():
    """`Param.type` is a STRING rather than a Python type because the sibling section
    validates rows read back out of a database column, and a closed table is what makes
    that safe. A duplicate name would make `check_params` grade one of the two."""
    assert set(remedies.PARAM_TYPES) == {"str", "int", "bool"}
    for remedy in remedies.REMEDIES.values():
        names = [p.name for p in remedy.params]
        assert len(names) == len(set(names)), remedy.id
        for param in remedy.params:
            assert param.type in remedies.PARAM_TYPES, (remedy.id, param.name)
            assert param.help, f"{remedy.id}.{param.name} has no help"


def test_check_params_names_every_problem_at_once():
    """The house rule `plans.parse_plan` and `findings.parse_report` follow: a parser
    that reports the first problem makes its caller fix one thing per round trip, and the
    caller here is a model writing a rule."""
    remedy = remedies.get("drop_hold")
    problem = remedy.check_params({"cause": 7, "colour": "blue"})

    assert problem
    assert problem.count(".") <= 1, problem          # one sentence
    assert "reason" in problem                       # missing, and required
    assert "colour" in problem                       # unknown
    assert "cause" in problem and "int" in problem   # wrong type
    assert remedy.check_params({"cause": "gate", "reason": "the gate went away"}) == ""
    # A bool is not a str, whatever duck typing would say, and an int is not a bool: a
    # rule that wrote `true` where prose belongs has made a mistake worth naming.
    assert remedies.get("force_rejudge").check_params({"reason": True})
    # And the fourth kind, from spec commit 2920441: a value outside `Param.choices`. It
    # is named in the SAME sentence as the rest, for the same round-trip reason.
    both = remedies.get("raise_attention").check_params({"template": "nope",
                                                         "colour": "blue"})
    assert "nope" in both and "colour" in both and both.count(".") <= 1


def test_update_branch_can_apply_reads_the_local_holds_only(project):
    """Positive and negative together: a parked order with a pull request and nothing in
    flight may be caught up, and the same order with a message queued may not. The seven
    guards belong to the daemon; the ones repeated here are the LOCAL ones, because
    `can_apply` makes no network call and no subprocess."""
    from jarvis import invariants, ops

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    remedy = remedies.get("update_branch")
    try:
        assert remedy.can_apply(store, wo, {}) == ""

        msg = store.queue_message(wo["id"], "are you there", source="jarvis")
        assert "message" in remedy.can_apply(store, wo, {})

        bare, other = _wo(project)
        assert "pull request" in remedy.can_apply(bare, other, {})
        bare.close()

        store.mark_message(msg, "delivered")
        for _ in range(ops.CATCH_UP_MAX):
            store.add_event(wo["id"], invariants.PR_BASE_UPDATED_EVENT,
                            {"cause": "behind"})
        assert str(ops.CATCH_UP_MAX) in remedy.can_apply(store, wo, {})
    finally:
        store.close()


def test_carry_verdict_can_apply_wants_a_verdict_and_a_head_that_moved(project):
    """The carry exists for a head the verdict does not yet cover, so both halves have to
    be there: a passed round, and a NEWER head the OS recorded. kn-907c9a61 — the proof
    itself is not re-implemented here and is not re-implemented in the handler either;
    both call `branchproof` and `ops.carry_merge_chain` in the daemon's own order."""
    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    remedy = remedies.get("carry_verdict")
    try:
        assert "round" in remedy.can_apply(store, wo, {})   # never judged

        _judged(store, wo["id"])
        assert "head" in remedy.can_apply(store, wo, {})    # judged, nothing moved

        _head_moved(store, wo["id"])
        assert remedy.can_apply(store, wo, {}) == ""
    finally:
        store.close()


def test_force_rejudge_can_apply_is_the_commands_own_refusal(project, catalog_file):
    """`ops.force_validation_refusal` VERBATIM, and the sharing is structural rather than
    a copied predicate (kn-4ea33fe6): the dashboard, `jarvis validation force` and this
    field have to refuse the same work orders in the same words."""
    from jarvis import ops

    data = json.loads(catalog_file.read_text())
    data["os"]["validation"] = {"enabled": True}
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    remedy = remedies.get("force_rejudge")
    try:
        assert remedy.can_apply(store, wo, {"reason": "the round recorded no commit"}) == ""

        store.set_status(wo["id"], "running")
        running = store.get_work_order(wo["id"])
        refusal = remedy.can_apply(store, running, {"reason": "x"})
        assert refusal == ops.force_validation_refusal(
            store, running, project="proj_a", cfg=ops.validation_config("proj_a"))
    finally:
        store.close()


def test_lower_attention_can_apply_asks_true_blockers(project):
    """The predicate that OWNS the column being cleared. `invariants.true_blockers`
    re-derives the attention reason every tick (INV-ATTENTION-REASON), so a flag it still
    derives would go straight back up and the remedy would be a lie on the record."""
    from jarvis import invariants

    store, wo = _wo(project)
    remedy = remedies.get("lower_attention")
    try:
        assert "flag" in remedy.can_apply(store, wo, {})    # already down

        store.flag_attention(wo["id"], invariants.AUTH_BLOCKER)
        flagged = store.get_work_order(wo["id"])
        assert invariants.true_blockers(store, flagged) == []
        assert remedy.can_apply(store, flagged, {}) == ""

        store.add_assumption(wo["id"], "I assumed the base branch is main")
        pending = store.get_work_order(wo["id"])
        assert "assumption" in remedy.can_apply(store, pending, {})
    finally:
        store.close()


def test_drop_hold_refuses_every_cause_but_the_gate_by_name(project):
    """NEO 908. A hold has no row — `holds.py` derives episodes from timeline events — so
    ending one means writing its CLOSING event, and for every cause but the gate that
    event would assert something false (a `validation_passed` no panel returned). The
    refusal NAMES the cause asked for, because a rule author reading "refused" learns
    nothing about which causes v1 does take."""
    from jarvis import holds

    store, wo = _wo(project)
    remedy = remedies.get("drop_hold")
    try:
        for cause in sorted(set(holds.HOLD_CAUSES) - {holds.GATE}):
            refusal = remedy.can_apply(store, wo, {"cause": cause, "reason": "r"})
            assert cause in refusal, cause
            assert holds.GATE in refusal, cause

        # The gate cause with nothing held is still a refusal, and a different one.
        assert "gate" in remedy.can_apply(store, wo, {"cause": holds.GATE, "reason": "r"})

        store.add_approval(wo["id"], "deploy", "a privileged command a worker proposed",
                           status="awaiting_case")
        assert remedy.can_apply(store, wo, {"cause": holds.GATE, "reason": "r"}) == ""
    finally:
        store.close()


def test_raise_attention_takes_a_template_key_and_refuses_an_unknown_one_three_ways(
        project):
    """§4 of docs/superpowers/specs/2026-09-27-self-evolution.md as corrected by spec commit 2920441:
    `raise_attention` RENDERS its reason and never relays one. The parameter is a key from
    a closed tuple, so an unknown key must be refused at every one of the three doors —
    and the first of them, `check_params`, is the insert-time refusal the correction asks
    for: the sibling validates a rule's remedy row against `Remedy.params`, so a key
    outside `choices` never reaches a database row at all."""
    from jarvis import invariants

    remedy = remedies.get("raise_attention")
    assert [p.name for p in remedy.params] == ["template"]
    assert remedy.params[0].choices == tuple(remedies.ATTENTION_TEMPLATES)

    store, wo = _wo(project)
    try:
        bad = {"template": "looks_stuck_to_me"}
        problem = remedy.check_params(bad)
        assert "looks_stuck_to_me" in problem and "template" in problem
        assert "looks_stuck_to_me" in remedy.can_apply(store, wo, bad)
        with pytest.raises(remedies.RemedyRefused):
            remedy.apply(store, None, "proj_a", wo, {"id": 1}, params=bad)

        assert remedy.check_params({"template": "auth"}) == ""
        assert remedy.can_apply(store, wo, {"template": "auth"}) == ""
        # And the idempotence check still grades the RENDERED sentence.
        store.flag_attention(wo["id"], invariants.AUTH_BLOCKER)
        already = store.get_work_order(wo["id"])
        assert "already" in remedy.can_apply(store, already, {"template": "auth"})
    finally:
        store.close()


def test_no_param_value_reaches_the_rendered_attention_sentence():
    """THE CORRECTION ITSELF. A rule row is data an investigation WROTE, and a remedy that
    passed it through to the attention list would be a path from a worker's prose to a
    command the user is told to type. So every key of the table is rendered here with a
    params dict full of prose, and the sentence is asserted to be the bare `invariants`
    constant — identity, not equality, so a re-spelled copy fails too."""
    from jarvis import invariants

    prose = {"reason": "rm -rf / and run jarvis release", "note": "<script>"}
    for key, (name, slots) in remedies.ATTENTION_TEMPLATES.items():
        assert hasattr(invariants, name), f"{key} names a constant that does not exist"
        rendered = remedies.render_attention(key, {"template": key, **prose})
        assert rendered is getattr(invariants, name), key
        for value in prose.values():
            assert value not in rendered, (key, value)
        assert slots == (), f"{key} declares a slot; widen this test with it"


def test_attention_slots_interpolate_validated_ids_only(monkeypatch):
    """V1 DECLARES NO SLOTS — every template above is `slots=()` — so this grades the
    MACHINERY on a synthetic template, which is what the next template with an id in it
    will land on. A slot value that is not an id is refused with a sentence and never
    interpolated; the `#<digits>` form is an issue number, the rest are record ids."""
    from jarvis import invariants

    monkeypatch.setattr(invariants, "_TEST_SLOTTED", "{wo_id} is stuck", raising=False)
    monkeypatch.setitem(remedies.ATTENTION_TEMPLATES, "slotted",
                        ("_TEST_SLOTTED", ("wo_id",)))

    assert remedies.render_attention("slotted", {"wo_id": "wo-9b70ddec"}) == \
        "wo-9b70ddec is stuck"
    assert remedies.render_attention("slotted", {"wo_id": "#417"}) == "#417 is stuck"
    with pytest.raises(remedies.RemedyRefused) as bad:
        remedies.render_attention("slotted", {"wo_id": "run jarvis release"})
    assert "wo_id" in str(bad.value)

    assert remedies._is_id("fo-69ba1cc4") and remedies._is_id("al-12ab") \
        and remedies._is_id("io-0011aabb") and remedies._is_id("#3")
    for nope in ("", "wo-", "wo-zz", "wo-1234 and more", "the branch", "#", "#4a"):
        assert not remedies._is_id(nope), nope


def test_can_apply_writes_nothing_at_all(project):
    """READ-ONLY IS THE CONTRACT, and it is what lets the mechanical gate and `dry-run`
    call it on every candidate without acting. Graded on the row AND on the event count,
    because a predicate that recorded "I was asked" would pass a row-only check."""
    from jarvis import invariants

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        _head_moved(store, wo["id"])
        store.flag_attention(wo["id"], invariants.AUTH_BLOCKER)
        before = store.get_work_order(wo["id"])
        events = len(store.list_events(wo["id"]))

        for remedy in remedies.REMEDIES.values():
            remedy.can_apply(store, before, {"reason": invariants.AUTH_BLOCKER,
                                             "cause": "gate"})

        assert store.get_work_order(wo["id"]) == before
        assert len(store.list_events(wo["id"])) == events
    finally:
        store.close()


def test_importing_remedies_does_not_import_invariants():
    """THE LAYERING, and it is why `render_attention()` fetches the constant lazily.

    This module has NO `jarvis` import at module scope — `ops`, `db`, `gates` and
    `supervisor` are all imported inside function bodies — because the sibling rules
    section is specified as a leaf that may import it beside stdlib, `db` and `catalog`.
    A module-level `from .invariants import …` drags `project_store`, `neo_store`,
    `budget`, `worker_session`, `catalog` and `automerge` in behind it, eagerly, for
    every importer.

    IN A SUBPROCESS, because by the time this file runs the suite has imported half the
    OS and `sys.modules` would make the assertion vacuous. `tests/test_neo_panel.py::
    test_neo_never_imports_the_panel` states its own boundary for the same reason and
    walks an AST instead; an AST cannot see this one, since what is under test is what a
    lazily-imported module pulls in TRANSITIVELY rather than which names are written.
    """
    probe = ("import sys; import jarvis.remedies as r; "
             "assert 'jarvis.invariants' not in sys.modules, "
             "'jarvis.invariants was imported eagerly'; "
             "assert r.ATTENTION_TEMPLATES, 'the table is empty'; "
             "assert r.render_attention('auth', {}), 'nothing was rendered'; "
             "assert 'jarvis.invariants' in sys.modules, "
             "'render_attention() never reached invariants — it re-spells the strings'")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr


# -- 7. the five acting handlers themselves ---------------------------------------------
#
# Review round 1, assumption [4]: each of these is a fresh copy of the daemon's own
# orchestration and not a pass-through to code the seam tests above already exercise, so
# every one is driven through `REMEDIES[id].apply` — never the private `_apply_*` name —
# so the test goes through the same door a verdict does.


def test_carry_verdict_apply_happy_binds_the_round_to_the_moved_head(project,
                                                                       monkeypatch):
    """Proofs (a) and (b) both hold: the diff is unchanged and the walked chain's newest
    commit IS the live head, so `ops.carry_merge_chain` finds facts and writes them."""
    from jarvis import branchproof, ci, github

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        monkeypatch.setattr(github, "pr_view", lambda url, cwd=None: github.PullRequest(
            state="OPEN", base_ref="main", head_oid=MOVED_SHA, base_oid=JUDGED_SHA))
        monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: True)
        monkeypatch.setattr(branchproof, "diff_fingerprint",
                            lambda repo, base, sha: "same-diff-fingerprint")
        monkeypatch.setattr(
            ci, "base_merge_chain",
            lambda pr_url, judged, head, *, base_ref, cwd=None:
            ((MOVED_SHA, "cccc1111cccc2222cccc3333cccc4444cccc5555", True),))

        sentence = remedies.REMEDIES["carry_verdict"].apply(
            store, None, "proj_a", wo, {}, params={})

        row = store.latest_validation_round(wo_id=wo["id"])
        assert f"round {row['round']}" in sentence
        assert row["carried_head_sha"] == MOVED_SHA
    finally:
        store.close()


def test_carry_verdict_apply_refuses_when_the_diffs_differ(project, monkeypatch):
    """THE CASE THE PANEL CALLED OUT: proof (b) is what decides whether an unjudged
    commit may reach `main`, and a merge whose resolution touched authored content must
    refuse even though proof (a)'s chain would otherwise hold."""
    from jarvis import branchproof, ci, github, ops

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        monkeypatch.setattr(github, "pr_view", lambda url, cwd=None: github.PullRequest(
            state="OPEN", base_ref="main", head_oid=MOVED_SHA, base_oid=JUDGED_SHA))
        monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: True)
        fingerprints = iter(["fp-judged", "fp-head"])
        monkeypatch.setattr(branchproof, "diff_fingerprint",
                            lambda repo, base, sha: next(fingerprints))
        monkeypatch.setattr(
            ci, "base_merge_chain",
            lambda pr_url, judged, head, *, base_ref, cwd=None:
            ((MOVED_SHA, "cccc1111cccc2222cccc3333cccc4444cccc5555", True),))

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["carry_verdict"].apply(
                store, None, "proj_a", wo, {}, params={})

        events = store.events_of_kind(wo["id"], ops.CARRY_REFUSED_EVENT)
        assert len(events) == 1
        assert db.from_json(events[0]["payload"], {})["proof"] == ops.PROOF_PATCH_ID
        row = store.latest_validation_round(wo_id=wo["id"])
        assert row["carried_head_sha"] == ""
    finally:
        store.close()


def test_carry_verdict_apply_refuses_on_an_empty_chain(project, monkeypatch):
    """`base_merge_chain` found no run of merges connecting `judged` to `head` — proof (a)
    fails even though the diffs (proof (b)) agree."""
    from jarvis import branchproof, ci, github, ops

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        monkeypatch.setattr(github, "pr_view", lambda url, cwd=None: github.PullRequest(
            state="OPEN", base_ref="main", head_oid=MOVED_SHA, base_oid=JUDGED_SHA))
        monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: True)
        monkeypatch.setattr(branchproof, "diff_fingerprint",
                            lambda repo, base, sha: "same-diff-fingerprint")
        monkeypatch.setattr(
            ci, "base_merge_chain",
            lambda pr_url, judged, head, *, base_ref, cwd=None: ())

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["carry_verdict"].apply(
                store, None, "proj_a", wo, {}, params={})

        events = store.events_of_kind(wo["id"], ops.CARRY_REFUSED_EVENT)
        assert len(events) == 1
        assert db.from_json(events[0]["payload"], {})["proof"] == ops.PROOF_CHAIN
    finally:
        store.close()


def test_carry_verdict_apply_refuses_when_the_fetch_fails(project, monkeypatch):
    """A fetch that failed means nothing local is comparable — refused before either
    proof is even attempted, so neither `diff_fingerprint` nor `base_merge_chain` runs."""
    from jarvis import branchproof, ci, github, ops

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        monkeypatch.setattr(github, "pr_view", lambda url, cwd=None: github.PullRequest(
            state="OPEN", base_ref="main", head_oid=MOVED_SHA, base_oid=JUDGED_SHA))
        monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: False)
        fingerprint_calls: list[Any] = []
        chain_calls: list[Any] = []
        monkeypatch.setattr(branchproof, "diff_fingerprint",
                            lambda repo, base, sha: fingerprint_calls.append(sha) or "x")
        monkeypatch.setattr(
            ci, "base_merge_chain",
            lambda pr_url, judged, head, *, base_ref, cwd=None:
            chain_calls.append(1) or ())

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["carry_verdict"].apply(
                store, None, "proj_a", wo, {}, params={})

        assert fingerprint_calls == []
        assert chain_calls == []
        events = store.events_of_kind(wo["id"], ops.CARRY_REFUSED_EVENT)
        assert len(events) == 1
        assert db.from_json(events[0]["payload"], {})["proof"] == ops.PROOF_FETCH
    finally:
        store.close()


def test_update_branch_apply_refuses_when_the_head_moved_past_the_verdict(project,
                                                                            monkeypatch):
    """THE NARROWED WINDOW the handler's docstring names: the head is re-read
    immediately before the update, and a head the panel never read must not be merged
    into — so `ci.update_branch` is never reached."""
    from jarvis import ci, github

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        monkeypatch.setattr(github, "pr_view", lambda url, cwd=None: github.PullRequest(
            state="OPEN", base_ref="main", head_oid=MOVED_SHA, base_oid="c0c0c0c0"))
        calls: list[Any] = []
        monkeypatch.setattr(ci, "update_branch",
                            lambda pr_url, cwd=None: calls.append(pr_url))

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["update_branch"].apply(
                store, None, "proj_a", wo, {}, params={})

        assert calls == []
    finally:
        store.close()


def test_update_branch_apply_records_the_failure_and_spends_the_attempt(project,
                                                                           monkeypatch):
    """GitHub refused the update: the branch is exactly where it was, so this is a
    refusal rather than a failure — and `record_base_update_failed` marks the attempt
    spent for THIS base sha (`ops.base_heal_spent`), so it is not retried for ever."""
    from jarvis import ci, github, invariants, ops

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        monkeypatch.setattr(github, "pr_view", lambda url, cwd=None: github.PullRequest(
            state="OPEN", base_ref="main", head_oid=JUDGED_SHA, base_oid="c0c0c0c0"))

        def _raise(pr_url, cwd=None):
            raise github.GitHubError("gh said no", github.GitHubError.REFUSED)

        monkeypatch.setattr(ci, "update_branch", _raise)
        assert ops.base_heal_spent(store, wo["id"], "c0c0c0c0") is False

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["update_branch"].apply(
                store, None, "proj_a", wo, {}, params={})

        events = store.events_of_kind(wo["id"], invariants.PR_BASE_UPDATE_FAILED_EVENT)
        assert len(events) == 1
        assert db.from_json(events[0]["payload"], {})["reason"] == \
            github.GitHubError.REFUSED
        # `ops.catch_up_attempts` counts only a SUCCESSFUL update (`PR_BASE_UPDATED_EVENT`
        # with `cause="behind"`), so a refused one does not move it — the bound a refused
        # attempt actually spends is `base_heal_spent`, keyed on the base sha, which is
        # what stops the same refusal being retried against the same base for ever.
        assert ops.base_heal_spent(store, wo["id"], "c0c0c0c0") is True
    finally:
        store.close()


def test_update_branch_apply_happy_merges_the_base_and_records_the_new_head(project,
                                                                              monkeypatch):
    """`gh pr update-branch` succeeded: the record names both the commit that was judged
    and the one the merge produced."""
    from jarvis import ci, github, invariants

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        answers = iter([
            github.PullRequest(state="OPEN", base_ref="main", head_oid=JUDGED_SHA,
                               base_oid="c0c0c0c0"),
            github.PullRequest(state="OPEN", base_ref="main", head_oid=MOVED_SHA,
                               base_oid="c0c0c0c0"),
        ])
        monkeypatch.setattr(github, "pr_view", lambda url, cwd=None: next(answers))
        monkeypatch.setattr(ci, "update_branch", lambda pr_url, cwd=None: None)

        sentence = remedies.REMEDIES["update_branch"].apply(
            store, None, "proj_a", wo, {}, params={})

        assert JUDGED_SHA[:10] in sentence and MOVED_SHA[:10] in sentence
        (event,) = store.events_of_kind(wo["id"], invariants.PR_BASE_UPDATED_EVENT)
        payload = db.from_json(event["payload"], {})
        assert payload["head_before"] == JUDGED_SHA
        assert payload["head_after"] == MOVED_SHA
    finally:
        store.close()


def test_update_branch_apply_refuses_a_queued_message_before_touching_github(project,
                                                                                monkeypatch):
    """Review round 2's blocking defect: `apply` only checked the re-read head against
    the verdict, and never repeated `_in_flight`'s message guard — so a grant approved
    after a message queued could still move the branch a worker has not seen the message
    about yet. The guard now runs BEFORE `github.pr_view`, so neither call is reached."""
    from jarvis import ci, github

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        store.queue_message(wo["id"], "are you there", source="jarvis")
        pr_view_calls: list[Any] = []
        update_calls: list[Any] = []
        monkeypatch.setattr(
            github, "pr_view",
            lambda url, cwd=None: pr_view_calls.append(url) or github.PullRequest(
                state="OPEN", base_ref="main", head_oid=JUDGED_SHA, base_oid="c0c0c0c0"))
        monkeypatch.setattr(ci, "update_branch",
                            lambda pr_url, cwd=None: update_calls.append(pr_url))

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["update_branch"].apply(
                store, None, "proj_a", wo, {}, params={})

        assert update_calls == []
        assert pr_view_calls == []
    finally:
        store.close()


def test_update_branch_apply_refuses_an_open_validation_round_before_touching_github(
        project, monkeypatch):
    """The same defect, the round guard: a grant approved before a round opened must not
    move the head the seats reading it are judging (Neo question 283, review round 2)."""
    from jarvis import ci, github

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        store.open_validation_round(wo_id=wo["id"], fingerprint="fp-open", round=2)
        pr_view_calls: list[Any] = []
        update_calls: list[Any] = []
        monkeypatch.setattr(
            github, "pr_view",
            lambda url, cwd=None: pr_view_calls.append(url) or github.PullRequest(
                state="OPEN", base_ref="main", head_oid=JUDGED_SHA, base_oid="c0c0c0c0"))
        monkeypatch.setattr(ci, "update_branch",
                            lambda pr_url, cwd=None: update_calls.append(pr_url))

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["update_branch"].apply(
                store, None, "proj_a", wo, {}, params={})

        assert update_calls == []
        assert pr_view_calls == []
    finally:
        store.close()


def test_update_branch_apply_refuses_past_the_catch_up_bound_before_touching_github(
        project, monkeypatch):
    """The third guard `_can_update_branch` carries and `apply` did not (review round 2):
    `ops.CATCH_UP_MAX` recorded catch-ups mean a base moving faster than the OS can keep
    this branch caught up with is a person's problem, not another merge — checked, again,
    before any GitHub call."""
    from jarvis import ci, github, invariants, ops

    store, wo = _wo(project, pr_url=PR_URL, status="waiting_pr_merge")
    try:
        _judged(store, wo["id"])
        for _ in range(ops.CATCH_UP_MAX):
            store.add_event(wo["id"], invariants.PR_BASE_UPDATED_EVENT,
                            {"cause": "behind"})
        pr_view_calls: list[Any] = []
        update_calls: list[Any] = []
        monkeypatch.setattr(
            github, "pr_view",
            lambda url, cwd=None: pr_view_calls.append(url) or github.PullRequest(
                state="OPEN", base_ref="main", head_oid=JUDGED_SHA, base_oid="c0c0c0c0"))
        monkeypatch.setattr(ci, "update_branch",
                            lambda pr_url, cwd=None: update_calls.append(pr_url))

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["update_branch"].apply(
                store, None, "proj_a", wo, {}, params={})

        assert update_calls == []
        assert pr_view_calls == []
    finally:
        store.close()


def test_force_rejudge_apply_refuses_with_no_reason_and_calls_nothing(project,
                                                                        monkeypatch):
    """The reason is stored ON THE ROUND, so an unreasoned rule row is refused before
    `ops.force_validation` is ever asked to open one."""
    from jarvis import ops

    store, wo = _wo(project)
    calls: list[Any] = []
    monkeypatch.setattr(ops, "force_validation",
                        lambda wo_id, *, reason, project_name=None: calls.append(1))
    try:
        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["force_rejudge"].apply(
                store, None, "proj_a", wo, {}, params={})
        assert calls == []
    finally:
        store.close()


def test_force_rejudge_apply_surfaces_ops_error_and_names_the_round_on_success(
        project, monkeypatch):
    """`ops.OpsError` becomes `RemedyRefused` carrying its own sentence; a success names
    the round `ops.force_validation` opened and the reason verbatim."""
    from jarvis import ops

    store, wo = _wo(project)
    try:
        monkeypatch.setattr(
            ops, "force_validation",
            lambda wo_id, *, reason, project_name=None:
            (_ for _ in ()).throw(ops.OpsError("no round budget left")))
        with pytest.raises(remedies.RemedyRefused) as excinfo:
            remedies.REMEDIES["force_rejudge"].apply(
                store, None, "proj_a", wo, {}, params={"reason": "the round recorded no "
                                                                   "commit"})
        assert "no round budget left" in str(excinfo.value)

        monkeypatch.setattr(
            ops, "force_validation",
            lambda wo_id, *, reason, project_name=None: {"round": 3, "reason": reason})
        sentence = remedies.REMEDIES["force_rejudge"].apply(
            store, None, "proj_a", wo, {},
            params={"reason": "the round recorded no commit"})
        assert "3" in sentence and "the round recorded no commit" in sentence
    finally:
        store.close()


def test_lower_attention_apply_happy_clears_a_flag_true_blockers_no_longer_derives(
        project):
    """`invariants.true_blockers` re-derives nothing from a bare `pending` work order, so
    the flag `apply` clears is one the next reconcile tick would have cleared anyway."""
    from jarvis import invariants

    store, wo = _wo(project)
    try:
        store.flag_attention(wo["id"], invariants.AUTH_BLOCKER)
        remedies.REMEDIES["lower_attention"].apply(
            store, None, "proj_a", wo, {}, params={})
        assert not store.get_work_order(wo["id"])["needs_attention"]
    finally:
        store.close()


def test_lower_attention_apply_re_reads_the_row_and_refuses_on_a_fresh_blocker(project):
    """THE RE-READ THE DOCSTRING PROMISES: `subject` is captured stale, on purpose,
    before a pending assumption lands — so a caller passing the row it read at the top of
    the tick must not have the flag taken down from under a blocker nothing else asks
    about again."""
    from jarvis import invariants

    store, wo = _wo(project)
    try:
        store.flag_attention(wo["id"], invariants.AUTH_BLOCKER)
        stale = store.get_work_order(wo["id"])
        store.add_assumption(wo["id"], "I assumed the base branch is main")

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["lower_attention"].apply(
                store, None, "proj_a", stale, {}, params={})

        assert store.get_work_order(wo["id"])["needs_attention"]
    finally:
        store.close()


def test_drop_hold_apply_happy_abandons_the_open_gate_request(project):
    """Closes the request `expired`/`abandoned` — never a verdict — and the command it
    named stays blocked, which is the whole point of ending a hold nobody argued."""
    store, wo = _wo(project)
    try:
        approval = store.add_approval(
            wo["id"], "deploy", "a privileged command a worker proposed",
            status="awaiting_case")
        # `add_approval` already writes the paired `gate_requested` event carrying this
        # approval's id — `_open_gate_episode` reads exactly that event, so nothing
        # further needs writing here.

        sentence = remedies.REMEDIES["drop_hold"].apply(
            store, None, "proj_a", wo, {},
            params={"cause": "gate", "reason": "the recogniser mismatched"})

        closed = store.get_approval(approval["id"])
        assert closed["status"] == "expired"
        assert closed["closed_as"] == "abandoned"
        assert "blocked" in sentence
    finally:
        store.close()


def test_drop_hold_apply_refuses_a_non_gate_cause_and_writes_nothing(project):
    """NEO 908: every cause but the gate has no end-it-now call behind it, so the refusal
    names the cause asked for and touches no row."""
    store, wo = _wo(project)
    try:
        events = len(store.list_events(wo["id"]))
        with pytest.raises(remedies.RemedyRefused) as excinfo:
            remedies.REMEDIES["drop_hold"].apply(
                store, None, "proj_a", wo, {}, params={"cause": "neo", "reason": "x"})
        assert "neo" in str(excinfo.value)
        assert len(store.list_events(wo["id"])) == events
    finally:
        store.close()


def test_drop_hold_apply_refuses_with_no_reason_and_leaves_the_approval_open(project):
    """The reason is the whole record of why a request nobody decided was closed, so an
    unreasoned call must not touch the approval at all."""
    store, wo = _wo(project)
    try:
        approval = store.add_approval(
            wo["id"], "deploy", "a privileged command a worker proposed",
            status="awaiting_case")

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["drop_hold"].apply(
                store, None, "proj_a", wo, {}, params={"cause": "gate", "reason": ""})

        assert store.get_approval(approval["id"])["status"] == "awaiting_case"
    finally:
        store.close()


def test_drop_hold_apply_refuses_with_no_gate_request_open_at_all(project):
    """The one `drop_hold` apply case not yet covered: `_open_gate_episode` finds nothing
    because none was ever filed, so this is refused the same as any other unsatisfied
    precondition — nothing written, no event, no approval touched (there is none)."""
    store, wo = _wo(project)
    try:
        events = len(store.list_events(wo["id"]))

        with pytest.raises(remedies.RemedyRefused) as excinfo:
            remedies.REMEDIES["drop_hold"].apply(
                store, None, "proj_a", wo, {},
                params={"cause": "gate", "reason": "the recogniser mismatched"})

        assert "no gate request" in str(excinfo.value) or "still open" in str(
            excinfo.value)
        assert len(store.list_events(wo["id"])) == events
    finally:
        store.close()


def test_raise_attention_apply_happy_flags_the_invariants_constant_verbatim(project):
    """`apply` renders through the closed table and writes exactly the `invariants`
    constant the template names — never a re-spelled copy — so `true_blockers` and this
    remedy can never read the same blocker as two different sentences."""
    from jarvis import invariants

    store, wo = _wo(project)
    try:
        sentence = remedies.REMEDIES["raise_attention"].apply(
            store, None, "proj_a", wo, {}, params={"template": "pr_closed"})

        flagged = store.get_work_order(wo["id"])
        assert flagged["needs_attention"]
        assert flagged["attention_reason"] == invariants.PR_CLOSED_BLOCKER
        assert invariants.PR_CLOSED_BLOCKER in sentence
    finally:
        store.close()


def test_raise_attention_apply_refuses_an_unknown_template_and_writes_nothing(project):
    """The third of the three doors `test_raise_attention_takes_a_template_key_...`
    already names: `apply` itself refuses a key outside the closed table, and a refusal
    at this door must leave the flag exactly where it was."""
    store, wo = _wo(project)
    try:
        before = store.get_work_order(wo["id"])

        with pytest.raises(remedies.RemedyRefused):
            remedies.REMEDIES["raise_attention"].apply(
                store, None, "proj_a", wo, {},
                params={"template": "looks_stuck_to_me"})

        assert store.get_work_order(wo["id"]) == before
    finally:
        store.close()
