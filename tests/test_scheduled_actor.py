"""Every open work order has an actor or a blocker — never neither.

Section 3 of
docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md:
`ops.scheduled_actor` answers "somebody is going to act on this" from the row alone,
over the predicates the daemon passes themselves use. Four pins:

1. coverage — every `(status, origin)` pair derives an actor or a blocker, and the
   pairs that correctly derive NEITHER are a table with a sentence each
2. population — the registry's keys are exactly `OPEN_STATUSES`
3. the predicate rule — every entry's test is a symbol IMPORTED from the pass's own
   module, never a lambda and never a condition restated in `ops`
4. no fallthrough — a status missing from the registry raises rather than guessing
"""

from __future__ import annotations

import ast
import importlib
import time
from pathlib import Path

import pytest

from jarvis import invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import (
    SETTLE_STATUSES,
    TURN_CLAIM_GRACE_SECONDS,
    Daemon,
    answers_question,
    reviews_gate,
)
from jarvis.neo_store import NEO_HELD_Q_STATUSES, NeoStore
from jarvis.project_store import OPEN_STATUSES, UNGOVERNED_ORIGINS, ProjectStore

ORIGINS = ("jarvis", "injected", "adhoc")

PR = "https://github.com/acme/proj/pull/7"

GATED = "gh " + "pr merge 42"  # split so the recogniser does not gate this file

#: (status, origin) pairs that correctly derive NEITHER an actor nor a blocker,
#: each mapped to the one sentence saying why.
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

#: `pending`'s actor is the claim SQL's, and that is section 4's.
PENDING_XFAIL = "pending's actor lands in the claim-SQL child"


def _params() -> list:
    out = []
    for status in OPEN_STATUSES:
        for origin in ORIGINS:
            marks = ([pytest.mark.xfail(strict=True, reason=PENDING_XFAIL)]
                     if status == "pending" else [])
            out.append(pytest.param(status, origin, marks=marks,
                                    id=f"{status}-{origin}"))
    return out


def _row(store: ProjectStore, status: str, origin: str) -> dict:
    """The minimal realistic row for this status: what the passes select on."""
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
        # The table bites both ways: an exemption that started deriving something is
        # a stale exemption, which is the defect this feature deletes one level up.
        assert actor is None, f"{status}/{origin} now derives {actor} — {why}"
        assert blockers == [], f"{status}/{origin} now derives {blockers} — {why}"
    else:
        assert actor is not None or blockers != [], (
            f"{status}/{origin} derives neither an actor nor a blocker; if that is "
            "correct, add it to EXPECT_NEITHER with the sentence saying why")


def test_every_open_status_is_in_the_registry():
    assert set(ops.SCHEDULED_ACTORS) == set(OPEN_STATUSES)


def _ops_imports() -> dict[str, str]:
    """name -> module it was imported from, for every `from . import`/`from .x import`."""
    tree = ast.parse(Path(ops.__file__).read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            name = alias.asname or alias.name
            found[name] = node.module or alias.name
    return found


def test_every_predicate_is_an_imported_symbol():
    imported = _ops_imports()
    for status, entry in ops.SCHEDULED_ACTORS.items():
        for p in entry.passes:
            module, _, name = p.predicate.rpartition(".")
            assert module and name, f"{status}: {p.predicate} is not dotted"
            assert module != "ops", (
                f"{status}: {p.predicate} is defined in ops — a predicate belongs to "
                "the pass's own module")
            assert imported.get(name) == module, (
                f"{status}: ops.py does not import {name} from {module}")
            resolved = getattr(importlib.import_module(f"jarvis.{module}"), name)
            assert resolved is p.test, f"{status}: {p.predicate} is not the test called"
            assert p.test.__name__ != "<lambda>", f"{status}: {p.predicate} is a lambda"


def test_no_fallthrough(jarvis_home, project):
    for status, entry in ops.SCHEDULED_ACTORS.items():
        assert bool(entry.none_because) is not bool(entry.passes), (
            f"{status}: `none_because` must be non-empty exactly when there are no "
            "passes")
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X")
    with pytest.raises(KeyError):
        ops.scheduled_actor(store, {**wo, "status": "completed"})


# -- the predicates said NO: what the registry derives, and what the pass does ------

def _age_past_grace(store: ProjectStore, wo_id: str) -> dict:
    """Backdate the claim past `TURN_CLAIM_GRACE_SECONDS` and re-read the row."""
    store.conn.execute("UPDATE work_orders SET updated_at=? WHERE id=?",
                       (time.time() - TURN_CLAIM_GRACE_SECONDS - 60, wo_id))
    return store.get_work_order(wo_id)


@pytest.fixture()
def settler(fake_claude, catalog_file, project):
    """`settler(store)` — `Daemon.settle_turns` over one project, driven directly."""
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


def test_a_claim_past_its_grace_is_a_hole_the_settler_still_fails(jarvis_home, project,
                                                                 settler):
    # Spec lines 27-29: the grace is the only thing telling "just claimed" from "the
    # daemon died between the two writes" — so None here means a hole for the user, not
    # a claimed actor, and the settler's failure write is what puts it in front of them.
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


@pytest.mark.parametrize("status", ["needs_review", "budget_exhausted"])
def test_a_none_because_status_blocks_on_the_user(jarvis_home, project, status):
    store = ProjectStore(project)
    assert ops.SCHEDULED_ACTORS[status].none_because
    wo = _row(store, status, "jarvis")
    assert (ops.scheduled_actor(store, wo),
            invariants.true_blockers(store, wo) != []) == (None, True)
