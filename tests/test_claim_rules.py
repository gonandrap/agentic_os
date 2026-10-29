"""One claimability rule, two queries, same SQL text (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md)"""

from __future__ import annotations

import pytest

from jarvis import db
from jarvis.project_store import CLAIM_RULES, ProjectStore


@pytest.fixture()
def store(jarvis_home, project):
    s = ProjectStore(project)
    yield s
    s.close()


class _SpyConn:
    """Delegates to the real connection, keeping every SQL string executed"""

    def __init__(self, conn):
        self._conn = conn
        self.sql: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.sql.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _oldest(store: ProjectStore, wo_id: str) -> None:
    store.conn.execute("UPDATE work_orders SET created_at=0 WHERE id=?", (wo_id,))


# -- the four refusals, each as the only pending row

def _held_by_dependency(store: ProjectStore) -> str:
    dep = store.create_work_order("dependency", status="running")
    return store.create_work_order("child", depends_on=[dep["id"]])["id"]


def _held_by_max_parallel(store: ProjectStore) -> str:
    fo = store.create_feature_order("CSV export", max_parallel=1)
    store.create_work_order("sibling", parent_id=fo["id"], status="running")
    return store.create_work_order("child", parent_id=fo["id"])["id"]


def _held_by_retry_after(store: ProjectStore) -> str:
    wo = store.create_work_order("backing off")
    store.hold_dispatch(wo["id"], db.now() + 600)
    return wo["id"]


def _held_by_precondition(store: ProjectStore) -> str:
    wo = store.create_work_order("the 5a predicate fix")
    store.add_precondition(wo["id"], "bl-d5baa28b")
    return wo["id"]


def _held_by_plan_assumption(store: ProjectStore) -> str:
    fo = store.create_feature_order("CSV export")
    planner = store.create_work_order("plan it", kind="planner", parent_id=fo["id"],
                                      status="completed")
    store.update_feature_order(fo["id"], plan_wo_id=planner["id"], status="executing")
    store.add_assumption(planner["id"], "the exporter writes CSV, not JSON")
    return store.create_work_order("child", parent_id=fo["id"])["id"]


def _held_by_spec_unmerged(store: ProjectStore) -> str:
    fo = store.create_feature_order("CSV export")
    planner = store.create_work_order("plan it", kind="planner", parent_id=fo["id"],
                                      status="waiting_pr_merge")
    store.update_work_order(planner["id"], pr_url="https://github.com/x/y/pull/9")
    store.update_feature_order(fo["id"], plan_wo_id=planner["id"], status="executing")
    return store.create_work_order("child", parent_id=fo["id"])["id"]


HOLDS = [
    ("dependency", _held_by_dependency),
    ("max_parallel", _held_by_max_parallel),
    ("retry_after", _held_by_retry_after),
    ("plan_assumption", _held_by_plan_assumption),
    # docs/superpowers/specs/2026-10-29-a-feature-spec-pull-request-lands-first.md §2
    ("spec_unmerged", _held_by_spec_unmerged),
    # §2.2 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md
    ("precondition", _held_by_precondition),
]


def _rule(code: str):
    return next(r for r in CLAIM_RULES if r.code == code)


@pytest.mark.parametrize(("code", "make"), HOLDS, ids=[c for c, _ in HOLDS])
def test_the_claim_sql_and_the_reader_agree_on_a_refusal(store, code, make):
    wo_id = make(store)

    assert store.claim_next_pending() is None
    row = store.get_work_order(wo_id)
    assert store.claim_refusal(row).code == code
    assert store.why_not_claimable(row) == _rule(code).why


def test_a_clean_row_is_claimed_and_the_reader_says_nothing(store):
    wo = store.create_work_order("ordinary work")

    assert store.why_not_claimable(wo) is None
    assert store.claim_next_pending()["id"] == wo["id"]


@pytest.mark.parametrize(("field", "value"), [("hidden", 1), ("status", "running")])
def test_the_selection_rule_refuses_a_row_the_claim_never_looks_at(store, field, value):
    wo = store.create_work_order("ordinary work")
    store.conn.execute(f"UPDATE work_orders SET {field}=? WHERE id=?",
                       (value, wo["id"]))

    row = store.get_work_order(wo["id"])
    assert store.claim_refusal(row).code == "selection"
    assert store.claim_refusal(row).delayed is False


def test_a_row_that_is_not_there_is_the_selection_rule_refusing(store):
    """Not None: a deleted id must never read as claimable and earn an actor"""
    assert store.claim_refusal({"id": "wo-nosuch"}) is _rule("selection")


# -- same text

def test_both_queries_are_built_from_the_same_rule_sql(store):
    wo = store.create_work_order("ordinary work")
    spy = _SpyConn(store.conn)
    store.conn = spy
    try:
        store.claim_refusal(wo)
        read_sql = spy.sql[-1]
        spy.sql.clear()
        store.claim_next_pending()
        update_sql = next(s for s in spy.sql if s.lstrip().startswith("UPDATE"))
    finally:
        store.conn = spy._conn

    for rule in CLAIM_RULES:
        assert rule.sql in update_sql, f"{rule.code} is not verbatim in the UPDATE"
        assert rule.sql in read_sql, f"{rule.code} is not verbatim in the reader"
    assert ";" not in update_sql, "the claim must stay ONE statement"


def test_every_rule_carries_a_reason(store):
    assert [r.code for r in CLAIM_RULES] == [
        "selection", "precondition", "retry_after", "dependency", "max_parallel",
        "plan_assumption", "spec_unmerged"]
    assert all(r.why.strip() for r in CLAIM_RULES)
    assert [r.delayed for r in CLAIM_RULES] == [False, True, True, True, True, True, True]


def test_a_merged_spec_releases_the_child(store):
    child = _held_by_spec_unmerged(store)
    planner = store.get_feature_order(store.get_work_order(child)["parent_id"])["plan_wo_id"]
    assert store.spec_hold(store.get_work_order(child))["planner_id"] == planner

    store.set_status(planner, "completed", trigger="merged")

    assert store.spec_hold(store.get_work_order(child)) is None
    assert store.claim_next_pending()["id"] == child


# -- §2.2: the precondition rule, ranked at index 1

def test_a_held_row_is_passed_over_and_nothing_at_all_is_written(store):
    """§3.2: the claim takes the younger free row and leaves the held one untouched"""
    held = _held_by_precondition(store)
    _oldest(store, held)
    free = store.create_work_order("unrelated")["id"]
    spans = store.conn.execute("SELECT COUNT(*) c FROM wo_state_spans "
                               "WHERE order_id=?", (held,)).fetchone()["c"]
    events = store.conn.execute("SELECT COUNT(*) c FROM wo_events "
                                "WHERE wo_id=?", (held,)).fetchone()["c"]

    assert store.claim_next_pending()["id"] == free
    assert store.claim_next_pending() is None

    row = store.get_work_order(held)
    assert row["status"] == "pending"
    assert row["dispatch_attempts"] == 0
    assert row["retry_after"] is None
    assert not row["needs_attention"]
    assert store.conn.execute("SELECT COUNT(*) c FROM wo_state_spans WHERE order_id=?",
                              (held,)).fetchone()["c"] == spans
    assert store.conn.execute("SELECT COUNT(*) c FROM wo_events WHERE wo_id=?",
                              (held,)).fetchone()["c"] == events


def test_the_precondition_outranks_a_live_dependency(store):
    """§3.3: index 1, so `claim_refusal` names the condition the order was CREATED with rather than a..."""
    dep = store.create_work_order("dependency", status="running")
    wo = store.create_work_order("child", depends_on=[dep["id"]])
    store.add_precondition(wo["id"], "bl-d5baa28b")

    row = store.get_work_order(wo["id"])
    assert store.claim_refusal(row).code == "precondition"
    assert store.why_not_claimable(row) == _rule("precondition").why


# -- kn-3bde1bf2: the hold is in the row SELECTION, so it starves nothing

@pytest.mark.parametrize(("code", "make"), [
    ("dependency", _held_by_dependency),
    ("plan_assumption", _held_by_plan_assumption),
    ("precondition", _held_by_precondition),
], ids=["dependency", "plan_assumption", "precondition"])
def test_the_oldest_held_row_does_not_starve_younger_ones(store, code, make):
    held = make(store)
    _oldest(store, held)
    young = [store.create_work_order("free one")["id"],
             store.create_work_order("free two")["id"]]

    claimed = [store.claim_next_pending(), store.claim_next_pending()]

    assert [c["id"] for c in claimed if c] == young
    assert store.get_work_order(held)["status"] == "pending"
