"""Every open work order has an actor or a blocker — never neither (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md)"""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest

from jarvis import db
from jarvis import gates, invariants, ops
from jarvis.catalog import DEFAULT_PULL_REQUESTS_RECOMPUTE_STALL_MINUTES, load_catalog
from jarvis.daemon import (
    SETTLE_STATUSES,
    TURN_CLAIM_GRACE_SECONDS,
    Daemon,
    answers_question,
    reviews_gate,
    runnable_round,
    settles_turn,
)
from jarvis.neo_store import NEO_HELD_Q_STATUSES, NeoStore
from jarvis.invariants import user_facing_wait
from jarvis.project_store import (
    CLAIM_RULES,
    FO_TERMINAL_STATUSES,
    OPEN_STATUSES,
    UNGOVERNED_ORIGINS,
    VALIDATION_CI_CAUSE,
    VALIDATION_HELD_CAUSE,
    ProjectStore,
)
from test_claim_rules import HOLDS  # the four delayed rules, built once

ORIGINS = ("jarvis", "injected", "adhoc")

PR = "https://github.com/acme/proj/pull/7"

GATED = "gh " + "pr merge 42"  # split so the recogniser does not gate this file

#: (status, origin) pairs that correctly derive NEITHER an actor nor a blocker
EXPECT_NEITHER: dict[tuple[str, str], str] = {
    ("dispatching", "adhoc"):
        "`Daemon.settle_turns` skips `UNGOVERNED_ORIGINS` and `true_blockers` derives "
        "nothing from `dispatching` — correct per `Daemon.retire_ungoverned` / "
        "INV-ADHOC-LEGACY-RETIRED",
    ("dispatching", "injected"):
        "`Daemon.settle_turns` skips `UNGOVERNED_ORIGINS`; nothing claims an injected "
        "row that has not started a turn",
    ("running", "adhoc"):
        "both `Daemon.settle_turns` and `Daemon.retry_paused_turns` skip "
        "`UNGOVERNED_ORIGINS`, and an `adhoc` row is asked for nothing "
        "(INV-ADHOC-LEGACY-RETIRED)",
    ("running", "injected"):
        "`Daemon.track_injected_sessions` follows this row, but its eligibility keys "
        "on the live `claude_cli` agents view (`sessions_by_cwd`), not on a row read",
    ("needs_review", "adhoc"):
        "`true_blockers`' triage is behind `governed`",
    ("needs_review", "injected"):
        "`true_blockers`' triage is behind `governed`",
}

def _params() -> list:
    return [pytest.param(status, origin, id=f"{status}-{origin}")
            for status in OPEN_STATUSES for origin in ORIGINS]


def _row(store: ProjectStore, status: str, origin: str) -> dict:
    """The minimal realistic row for this status: what the passes select on"""
    kw: dict = {"origin": origin, "status": status}
    if status == "budget_exhausted":
        kw["budget_usd"] = 1.0
    if status == "idle":  # `idle` is only ever written to a feature manager
        kw["kind"] = "manager"
        kw["parent_id"] = store.create_feature_order("a feature")["id"]
    wo = store.create_work_order("add feature X", **kw)
    if status == "waiting_pr_merge":
        store.update_work_order(wo["id"], pr_url=PR)
    if status == "validating":
        store.open_validation_round(wo_id=wo["id"], fingerprint="fp-1")
    return store.get_work_order(wo["id"])


@pytest.mark.parametrize(("status", "origin"), _params())
def test_an_actor_or_a_blocker_never_neither(jarvis_home, project, status, origin):
    store = ProjectStore(project)
    wo = _row(store, status, origin)
    actor = ops.scheduled_actor(store, wo)
    blockers = invariants.true_blockers(store, wo)
    why = EXPECT_NEITHER.get((status, origin))
    if why is not None:
        # The table bites both ways: an exemption that started deriving something is a stale exemption
        assert actor is None, f"{status}/{origin} now derives {actor} — {why}"
        assert blockers == [], f"{status}/{origin} now derives {blockers} — {why}"
    else:
        assert actor is not None or blockers != [], (
            f"{status}/{origin} derives neither an actor nor a blocker; if that is "
            "correct, add it to EXPECT_NEITHER with the sentence saying why")


def test_every_open_status_is_in_the_registry():
    assert set(ops.SCHEDULED_ACTORS) == set(OPEN_STATUSES)


def _ops_imports() -> dict[str, str]:
    """name -> module it was imported from, for every `from . import`/`from .x import`"""
    tree = ast.parse(Path(ops.__file__).read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            name = alias.asname or alias.name
            found[name] = node.module or alias.name
    return found


def _module_level_names(module) -> set[str]:
    """Names the module DEFINES at top level — functions and constants, no imports"""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _pass_literals() -> list[tuple[str, ast.expr]]:
    """(`predicate`, the expression written as `test`) per `_Pass(...)` in ops.py"""
    tree = ast.parse(Path(ops.__file__).read_text(encoding="utf-8"))
    out: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_Pass"):
            continue
        assert len(node.args) == 4 and not node.keywords, (
            "a `_Pass` is four positional args; the pin reads `predicate` and `test` "
            "by position")
        out.append((ast.literal_eval(node.args[2]), node.args[3]))
    return out


def test_every_predicate_is_an_imported_symbol():
    """`predicate` names where the test LIVES, and the registry passes that symbol"""
    imported = _ops_imports()
    entries = [(status, p) for status, entry in ops.SCHEDULED_ACTORS.items()
               for p in entry.passes]
    by_predicate: dict[str, list[ast.expr]] = {}
    for predicate, node in _pass_literals():
        by_predicate.setdefault(predicate, []).append(node)
    # SETS, not counts: one `_Pass` literal may be registered under several statuses (the shared...
    assert set(by_predicate) == {p.predicate for _, p in entries}, (
        "every `_Pass` literal in ops.py must be reachable from SCHEDULED_ACTORS")
    for status, p in entries:
        module, _, name = p.predicate.rpartition(".")
        assert module and name, f"{status}: {p.predicate} is not dotted"
        home = importlib.import_module(f"jarvis.{module}")
        assert name in _module_level_names(home), (
            f"{status}: {p.predicate} is not a module-level function or constant of "
            f"{module} — the dotted path must name where the predicate LIVES")
        resolved = getattr(home, name)
        assert resolved is p.test, f"{status}: {p.predicate} is not the test called"
        assert getattr(resolved, "__module__", home.__name__) == home.__name__, (
            f"{status}: {p.predicate} resolves to a symbol of {resolved.__module__}")
        if module != "ops":  # ops must IMPORT the pass's predicate, never copy it
            assert imported.get(name) == module, (
                f"{status}: ops.py does not import {name} from {module}")
        for node in by_predicate[p.predicate]:
            assert isinstance(node, ast.Name) and node.id == name, (
                f"{status}: {p.predicate}'s `test` is written as "
                f"{type(node).__name__}, not the bare name `{name}` — a lambda or a "
                "condition restated in the registry is the defect this pins")


def test_no_fallthrough(jarvis_home, project):
    """STILL A PIN after wo-715db5af: the XOR holds over a status's OWN passes"""
    shared = (ops.REVIEWS_GATE, ops.JUDGES_ROUND)
    for status, entry in ops.SCHEDULED_ACTORS.items():
        own = tuple(p for p in entry.passes if p not in shared)
        assert bool(entry.none_because) is not bool(own), (
            f"{status}: `none_because` must be non-empty exactly when there are no "
            "passes other than the shared status-agnostic constants")
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X")
    with pytest.raises(KeyError):
        ops.scheduled_actor(store, {**wo, "status": "completed"})


# -- the predicates said NO: what the registry derives, and what the pass does

def _age_past_grace(store: ProjectStore, wo_id: str) -> dict:
    """Backdate the claim past `TURN_CLAIM_GRACE_SECONDS` and re-read the row"""
    store.conn.execute("UPDATE work_orders SET updated_at=? WHERE id=?",
                       (db.now() - TURN_CLAIM_GRACE_SECONDS - 60, wo_id))
    return store.get_work_order(wo_id)


@pytest.fixture()
def settler(fake_claude, catalog_file, project):
    """`settler(store)` — `Daemon.settle_turns` over one project, driven directly"""
    catalog = load_catalog(catalog_file)
    daemon, spec = Daemon(catalog), catalog.projects[0]

    def run(store: ProjectStore) -> None:
        daemon.settle_turns(spec, store)

    return run


def test_validating_without_a_round_derives_no_actor(jarvis_home, project):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="validating")
    assert ops.scheduled_actor(store, wo) is None


def test_waiting_pr_merge_without_a_pr_url_derives_no_actor(jarvis_home, project):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="waiting_pr_merge")
    assert ops.scheduled_actor(store, wo) is None


# -- `waiting_pr_merge`: an actor only while GitHub is still recomputing

STALLED_HEAD, STALLED_TIP = "0488753", "88cac10"


def _parked_on_a_pr(store: ProjectStore, **cache) -> dict:
    wo = store.create_work_order("add feature X", status="waiting_pr_merge")
    store.update_work_order(wo["id"], pr_url=PR, **cache)
    return store.get_work_order(wo["id"])


def test_a_pull_request_github_stopped_recomputing_derives_no_actor(jarvis_home,
                                                                    project):
    """#995's own row"""
    store = ProjectStore(project)
    wo = _parked_on_a_pr(store, pr_head_oid=STALLED_HEAD, pr_ref_oid=STALLED_TIP,
                         pr_mergeable_unknown_since=db.now())

    assert ops.scheduled_actor(store, wo) is None


def test_a_fresh_unknown_is_an_ordinary_push_and_keeps_its_actor(jarvis_home, project):
    """kn-5e93939e: UNKNOWN right after a push is NORMAL"""
    store = ProjectStore(project)
    wo = _parked_on_a_pr(store, pr_head_oid=STALLED_HEAD, pr_ref_oid=STALLED_HEAD,
                         pr_mergeable_unknown_since=db.now())

    actor = ops.scheduled_actor(store, wo)

    assert actor is not None and actor.pass_name == "Daemon.poll_pull_requests"


def test_an_unknown_past_the_threshold_derives_no_actor(jarvis_home, project):
    """Condition (b) alone: head and tip agree"""
    store = ProjectStore(project)
    stale = db.now() - (DEFAULT_PULL_REQUESTS_RECOMPUTE_STALL_MINUTES + 1) * 60
    wo = _parked_on_a_pr(store, pr_head_oid=STALLED_HEAD, pr_ref_oid=STALLED_HEAD,
                         pr_mergeable_unknown_since=stale)

    assert ops.scheduled_actor(store, wo) is None


def test_an_unobserved_tip_never_disqualifies(jarvis_home, project):
    """Empty is NOT RECORDED and never a sha that could match one: a row the poll has not read a remote..."""
    store = ProjectStore(project)
    wo = _parked_on_a_pr(store, pr_head_oid=STALLED_HEAD, pr_ref_oid="")

    actor = ops.scheduled_actor(store, wo)

    assert actor is not None and actor.pass_name == "Daemon.poll_pull_requests"


def test_a_claim_past_its_grace_is_a_hole_the_settler_still_fails(jarvis_home, project,
                                                                 settler):
    # Spec lines 27-29: the grace is the only thing telling "just claimed" from "the daemon died between...
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="dispatching")
    wo = _age_past_grace(store, wo["id"])
    assert (store.latest_turn(wo["id"]), bool(wo["needs_attention"])) == (None, False), \
        "the premise: no turn was ever recorded and nothing is flagged yet"
    assert ops.scheduled_actor(store, wo) is None

    settler(store)

    moved = store.get_work_order(wo["id"])
    assert (moved["status"], bool(moved["needs_attention"]),
            invariants.true_blockers(store, moved) != []) == ("failed", True, True)


def test_a_status_outside_the_sweep_is_not_moved(jarvis_home, project, settler):
    store = ProjectStore(project)
    assert "needs_review" not in SETTLE_STATUSES
    wo = store.create_work_order("add feature X", status="needs_review")
    wo = _age_past_grace(store, wo["id"])

    settler(store)

    moved = store.get_work_order(wo["id"])
    assert (moved["status"], bool(moved["needs_attention"])) == ("needs_review", False)


def test_an_ungoverned_origin_in_the_sweep_is_not_moved(jarvis_home, project, settler):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="dispatching",
                                 origin=UNGOVERNED_ORIGINS[0])
    wo = _age_past_grace(store, wo["id"])

    settler(store)

    moved = store.get_work_order(wo["id"])
    assert (moved["status"], bool(moved["needs_attention"])) == ("dispatching", False)


def test_answers_question_tracks_the_drains_own_held_set(jarvis_home, project):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="waiting_input")
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", wo["id"], "Should the export default to CSV or JSON?")
        assert q["status"] in NEO_HELD_Q_STATUSES, "the premise: Neo holds it"
        held = answers_question(store, wo)
        neo.record_answer(q["id"], "CSV")
    finally:
        neo.close()
    assert (held is not None, answers_question(store, wo)) == (True, None)


@pytest.mark.parametrize(("make", "expected"), [
    (lambda store, wo_id: store.add_approval(wo_id, "pr_merge", GATED), True),
    (lambda store, wo_id: store.mark_approval_escalated(
        store.add_approval(wo_id, "pr_merge", GATED)["id"],
        "cannot judge this one"), False),
    (lambda store, wo_id: store.add_approval(wo_id, "pr_merge", GATED,
                                             status="awaiting_case"), True),
    (lambda store, wo_id: None, False),
], ids=["pending", "escalated", "held", "none"])
def test_reviews_gate_only_when_the_user_does_not_hold_it(jarvis_home, project,
                                                          make, expected):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="waiting_input")
    make(store, wo["id"])
    assert reviews_gate(store, wo) is expected


# -- `pending`: the claim SQL is the actor, and a DELAY is still that actor

def test_a_claimable_pending_order_names_the_dispatch_pass(jarvis_home, project):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X")
    actor = ops.scheduled_actor(store, wo)
    assert actor is not None and actor.pass_name == "Daemon.dispatch_pending"


@pytest.mark.parametrize(("code", "make"), HOLDS, ids=[c for c, _ in HOLDS])
def test_a_delayed_pending_order_still_names_the_dispatch_pass(jarvis_home, project,
                                                               code, make):
    """A live dependency, a cap, a backoff and a held plan are all WAITING CORRECTLY: the turn is later"""
    store = ProjectStore(project)
    wo = store.get_work_order(make(store))
    actor = ops.scheduled_actor(store, wo)
    assert actor is not None and actor.pass_name == "Daemon.dispatch_pending"
    rule = next(r for r in CLAIM_RULES if r.code == code)
    assert rule.why in actor.why
    assert invariants.true_blockers(store, wo) == []


@pytest.mark.parametrize(("field", "value"), [("hidden", 1), ("status", "running")])
def test_a_row_the_claims_selection_rejects_derives_no_actor(jarvis_home, project,
                                                             field, value):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X")
    store.conn.execute(f"UPDATE work_orders SET {field}=? WHERE id=?",
                       (value, wo["id"]))
    row = {**store.get_work_order(wo["id"]), "status": "pending"}
    assert ops.scheduled_actor(store, row) is None


# -- `validating`: a HELD round names the panel, because the hold LIFTS

def _held_round(store: ProjectStore, wo_id: str) -> dict:
    """Round one closed `failed` with the hold ahead of it"""
    rnd = store.open_validation_round(wo_id=wo_id, fingerprint="fp-1")
    store.close_validation_round(int(rnd["id"]), "failed", "the usage window is spent",
                                 VALIDATION_HELD_CAUSE)
    store.add_event(wo_id, "validation_failed",
                    {"round": int(rnd["round"]), "cause": VALIDATION_HELD_CAUSE,
                     "reopens_at": db.now() + 3600})
    return store.get_work_order(wo_id)


def test_a_round_inside_its_hold_still_names_the_panel(jarvis_home, project):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="validating")
    row = _held_round(store, wo["id"])

    actor = ops.scheduled_actor(store, row)

    assert actor is not None and actor.pass_name == "Daemon.validation_tick"


def test_the_passs_own_hold_gate_is_unchanged(jarvis_home, project):
    """`runnable_round` is `validation_tick`'s selection AND its hold test: weakening it walks the pass..."""
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="validating")
    row = _held_round(store, wo["id"])

    assert runnable_round(store, row) is None


# -- issue #1199: the panel's selection is `OPEN_STATUSES`, so every open status carries the pass

def _ci_held_round(store: ProjectStore, wo_id: str) -> dict:
    """Round one closed `failed` on `VALIDATION_CI_CAUSE`: the panel re-runs it when CI finishes"""
    rnd = store.open_validation_round(wo_id=wo_id, fingerprint="fp-1")
    store.close_validation_round(int(rnd["id"]), "failed",
                                 "waiting for GitHub to finish the checks",
                                 VALIDATION_CI_CAUSE)
    store.add_event(wo_id, "validation_failed",
                    {"round": int(rnd["round"]), "cause": VALIDATION_CI_CAUSE,
                     "reopens_at": db.now() + 300})
    return store.get_work_order(wo_id)


def _delivered_with_a_confirming_assumption(store: ProjectStore) -> dict:
    """The production row: delivered, parked in `needs_review`, assumptions the OS is confirming"""
    wo = store.create_work_order("add feature X", status="needs_review")
    store.update_work_order(wo["id"], result_summary="done")
    store.record_provisional(store.add_assumption(wo["id"], "one call I made"),
                             verdict="accept")
    return store.get_work_order(wo["id"])


@pytest.fixture
def _os_reviews_assumptions(monkeypatch):
    """`invariants.true_blockers` reads `ops.auto_review_at`, which needs a live catalog"""
    monkeypatch.setattr(ops, "auto_review_at", lambda _path: True)


def test_a_needs_review_round_held_on_ci_names_the_panel(jarvis_home, project,
                                                         _os_reviews_assumptions):
    """#1199: `work_orders_awaiting_validation` filters on `OPEN_STATUSES`, not on `validating`"""
    store = ProjectStore(project)
    wo = _delivered_with_a_confirming_assumption(store)
    assert true_blockers_of(store, wo) == [
        invariants.NO_ACTOR_BLOCKER.format(status="needs_review")], "the premise"

    row = _ci_held_round(store, wo["id"])

    actor = ops.scheduled_actor(store, row)
    assert actor is not None and actor.pass_name == "Daemon.validation_tick"
    assert true_blockers_of(store, row) == []


def test_every_open_status_carries_the_panel_pass(jarvis_home, project):
    """The registration rule of kn-4b2304ef: a pass with no status filter is registered under all of them"""
    for status in OPEN_STATUSES:
        assert ops.JUDGES_ROUND in ops.SCHEDULED_ACTORS[status].passes, status


def true_blockers_of(store: ProjectStore, wo: dict) -> list[str]:
    return invariants.true_blockers(store, store.get_work_order(wo["id"]))


# -- `running`: a DONE turn names the settler only where the settler WRITES

def _done_turn(store: ProjectStore, wo_id: str, result: str | None = None) -> None:
    t = store.create_turn(wo_id, "dispatch", "go")
    store.finish_turn(int(t["id"]), "done", result=result)


def _held_gate(store: ProjectStore, wo_id: str) -> None:
    store.add_approval(wo_id, "pr_merge", GATED, status="awaiting_case")


def _pending_gate(store: ProjectStore, wo_id: str) -> None:
    store.add_approval(wo_id, "pr_merge", GATED)


def test_a_done_turn_the_settler_will_not_move_does_not_name_the_settler(
        jarvis_home, project, settler):
    """The defect's own row: a worker holding a HELD gate, its turn done hours ago"""
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="running")
    _done_turn(store, wo["id"])
    _held_gate(store, wo["id"])
    row = _age_past_grace(store, wo["id"])

    actor = ops.scheduled_actor(store, row)

    assert actor is not None and actor.predicate == "daemon.reviews_gate"  # #1109, not the settler
    settler(store)
    moved = store.get_work_order(wo["id"])
    assert (moved["status"], bool(moved["needs_attention"])) == ("running", False)


#: (what makes the shape, the status it must still be in)
WAIT_SHAPES = [
    pytest.param("running", _held_gate, "running", id="held-gate"),
    pytest.param("waiting_input", _pending_gate, "waiting_input", id="already-parked"),
    pytest.param("idle", lambda store, wo_id: None, "idle", id="manager-idle"),
    pytest.param("waiting_input", _pending_gate, "waiting_input", id="manager-asked"),
]


@pytest.mark.parametrize(("status", "make", "after"), WAIT_SHAPES)
def test_each_wait_shape_behaves_as_the_pass_behaves(jarvis_home, project, settler,
                                                     status, make, after, request):
    manager = "manager" in request.node.callspec.id
    store = ProjectStore(project)
    kw: dict = {"status": status}
    if manager or status == "idle":
        kw["kind"] = "manager"
        kw["parent_id"] = store.create_feature_order("a feature")["id"]
    wo = store.create_work_order("add feature X", **kw)
    _done_turn(store, wo["id"])
    make(store, wo["id"])
    row = _age_past_grace(store, wo["id"])

    assert settles_turn(store, row) is False

    settler(store)
    assert store.get_work_order(wo["id"])["status"] == after


#: (status, kind, what makes the shape, the status the settler leaves)
SUMMARY_SHAPES = [
    pytest.param("running", None, _held_gate, "completed", id="summary-held-gate"),
    pytest.param("waiting_input", "manager", _pending_gate, "completed",
                 id="summary-manager-asked"),
]


@pytest.mark.parametrize(("status", "kind", "make", "after"), SUMMARY_SHAPES)
def test_a_summary_outranks_the_wait_shape_in_both_readers(jarvis_home, project,
                                                           settler, status, kind, make,
                                                           after):
    """A `result_summary` is the FIRST question both readers ask (#264)"""
    store = ProjectStore(project)
    kw: dict = {"status": status}
    if kind == "manager":
        kw["kind"] = kind
        kw["parent_id"] = store.create_feature_order("a feature")["id"]
    wo = store.create_work_order("add feature X", **kw)
    if kind == "manager":
        feature = store.get_feature_order(kw["parent_id"])
        assert feature["status"] not in FO_TERMINAL_STATUSES, \
            "the premise: the feature is LIVE, so nothing closes the manager"
    _done_turn(store, wo["id"])
    make(store, wo["id"])
    assert user_facing_wait(store, wo["id"]) is (kind == "manager"), \
        ("the premise: a PENDING gate is a user-facing wait and a HELD one is not — "
         "the manager arm's hold, and the `waiting_input` write's chooser")
    store.update_work_order(wo["id"], result_summary="shipped the export")
    row = _age_past_grace(store, wo["id"])

    assert settles_turn(store, row) is True

    settler(store)
    assert store.get_work_order(wo["id"])["status"] == after


#: (status, kind, what makes the shape, the status the settler leaves)
WRITE_SHAPES = [
    pytest.param("running", None, _pending_gate, "waiting_input", id="arm-parks-it"),
    pytest.param("waiting_input", "manager", lambda store, wo_id: None, "idle",
                 id="arm-idles-the-manager"),
]


@pytest.mark.parametrize(("status", "kind", "make", "after"), WRITE_SHAPES)
def test_each_arm_that_writes_is_reached_on_its_own_row(jarvis_home, project, settler,
                                                        status, kind, make, after):
    """`settles_turn` True and the arm's write, for a row with NO `result_summary` (#264)"""
    store = ProjectStore(project)
    kw: dict = {"status": status}
    if kind == "manager":
        kw["kind"] = kind
        kw["parent_id"] = store.create_feature_order("a feature")["id"]
    wo = store.create_work_order("add feature X", **kw)
    _done_turn(store, wo["id"])
    make(store, wo["id"])
    row = _age_past_grace(store, wo["id"])

    assert settles_turn(store, row) is True

    settler(store)
    assert store.get_work_order(wo["id"])["status"] == after


def test_a_done_turn_the_settler_does_move_still_names_it(jarvis_home, project,
                                                          settler):
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="running")
    _done_turn(store, wo["id"])
    row = _age_past_grace(store, wo["id"])

    assert settles_turn(store, row) is True

    settler(store)
    assert store.get_work_order(wo["id"])["status"] == "needs_review"


def test_a_queued_message_is_the_delivery_passs_row_not_the_settlers(jarvis_home,
                                                                     project, settler):
    """The fourth branch that writes nothing for a done turn"""
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X", status="running")
    _done_turn(store, wo["id"])
    # A session of its own, which `worker_session.delivery_hold` requires: nothing carries a queued...
    store.update_work_order(wo["id"], session_id="sess-1")
    store.queue_message(wo["id"], "carry on")
    row = _age_past_grace(store, wo["id"])

    assert settles_turn(store, row) is False
    actor = ops.scheduled_actor(store, row)
    assert actor is not None and actor.pass_name == "Daemon.deliver_messages"


@pytest.mark.parametrize("status", ["needs_review", "budget_exhausted"])
def test_a_none_because_status_blocks_on_the_user(jarvis_home, project, status):
    store = ProjectStore(project)
    assert ops.SCHEDULED_ACTORS[status].none_because
    wo = _row(store, status, "jarvis")
    assert (ops.scheduled_actor(store, wo),
            invariants.true_blockers(store, wo) != []) == (None, True)


# -- a HELD gate request outlives the status it was filed under

@pytest.fixture()
def gate_sweeper(fake_claude, catalog_file, project):
    """`sweep(store)` — `Daemon.abandon_unargued_gates` over a gates-enabled project"""
    data = json.loads(catalog_file.read_text(encoding="utf-8"))
    data["projects"][0]["gates"] = ["pr_merge"]
    catalog_file.write_text(json.dumps(data), encoding="utf-8")
    catalog = load_catalog(catalog_file)
    daemon, spec = Daemon(catalog), catalog.projects[0]
    assert spec.gates, "the premise: the project gates `pr_merge`, so the sweep runs"

    def run(store: ProjectStore) -> None:
        daemon.abandon_unargued_gates(spec, store)

    return run


def _hold_a_request(store: ProjectStore, wo_id: str) -> dict:
    return store.add_approval(wo_id, "pr_merge", GATED, status="awaiting_case")


def _expire_the_case(store: ProjectStore, approval_id: int) -> None:
    """Wind the filing back past `case_ttl_seconds` rather than sleeping through it"""
    store.conn.execute("UPDATE approvals SET ts = ts - ? WHERE id=?",
                       (gates.DEFAULT_CASE_TTL_SECONDS + 60, approval_id))


def test_a_held_request_on_a_running_ungoverned_row_names_the_gate_sweep(
        jarvis_home, project, gate_sweeper):
    """The live shape on main, PAIRED WITH ITS PASS"""
    store = ProjectStore(project)
    assert ("running", UNGOVERNED_ORIGINS[0]) in EXPECT_NEITHER, \
        "the premise: with no request this same row correctly derives neither"
    wo = _row(store, "running", UNGOVERNED_ORIGINS[0])
    approval = _hold_a_request(store, wo["id"])

    actor = ops.scheduled_actor(store, wo)
    assert actor is not None and actor.predicate == "daemon.reviews_gate"

    # The other half of the predicate rule: the pass named really does act on THIS row
    _expire_the_case(store, approval["id"])
    gate_sweeper(store)

    closed = store.get_approval(approval["id"])
    assert (closed["status"], closed["closed_as"]) == ("expired", "abandoned")


@pytest.mark.parametrize("status", ["needs_review", "budget_exhausted"])
def test_a_held_request_names_an_actor_on_a_none_because_status(jarvis_home, project,
                                                                status):
    """Those two statuses' `None` was unconditional; it is now the predicate's answer"""
    store = ProjectStore(project)
    wo = _row(store, status, "jarvis")
    _hold_a_request(store, wo["id"])

    actor = ops.scheduled_actor(store, wo)

    assert actor is not None and actor.predicate == "daemon.reviews_gate"
    assert ops.SCHEDULED_ACTORS[status].none_because
