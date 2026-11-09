"""Who asked for an order, stamped on each assumption question (docs/superpowers/specs/2026-11-07-neo-owns-os-origin-assumption-review.md §2)"""

from __future__ import annotations

import pytest

from jarvis import autoreview, ops, steward
from jarvis.project_store import ProjectStore
from tests.test_autoreview import FakeNeo

STAMP = {steward.STAMP_KEY: {"origin_kind": "bug_agent", "decision_id": 916}}


@pytest.fixture
def store(jarvis_home, tmp_path):
    s = ProjectStore(tmp_path / "proj")
    yield s
    s.close()


def who(store, wo):
    return ops.requestor(store, wo)


def test_a_root_order_a_person_typed_is_the_users(store):
    wo = store.create_work_order("t")
    assert who(store, wo) == ops.Requestor("user", "you")


def test_a_steward_stamped_root_is_the_os_and_names_the_decision(store):
    wo = store.create_work_order("t", metadata=STAMP)
    assert who(store, wo) == ops.Requestor("os", "steward: bug_agent, decision #916")


@pytest.mark.parametrize("origin,label", [("schedule", "schedule"),
                                          ("fleet_health", "fleet health"),
                                          ("neo", "neo rider"), ("os", "os")])
def test_an_os_origin_root_is_the_os_in_the_users_words(store, origin, label):
    wo = store.create_work_order("t", origin=origin)
    assert who(store, wo) == ops.Requestor("os", label)


@pytest.mark.parametrize("kind", ["feature", "improvement", "investigation", "decision"])
def test_a_child_of_an_order_the_user_filed_is_the_users(store, kind):
    """`launched_by_os(child)` is true; the ROOT decides"""
    fo = store.create_feature_order("f", kind=kind)
    child = store.create_work_order("c", origin="os", kind="planner", parent_id=fo["id"])

    assert ops.launched_by_os(child)
    assert who(store, child) == ops.Requestor("user", "you")


def test_a_child_of_an_os_filed_feature_order_is_the_os(store):
    fo = store.create_feature_order("f", origin="neo")
    child = store.create_work_order("c", parent_id=fo["id"])
    assert who(store, child) == ops.Requestor("os", "neo rider")


def test_a_legacy_jarvis_row_with_a_steward_stamp_is_the_os(store):
    fo = store.create_feature_order("f", origin="jarvis", metadata=STAMP)
    child = store.create_work_order("c", parent_id=fo["id"])
    assert who(store, child).kind == "os"
    assert who(store, store.create_work_order("t", origin="jarvis", metadata=STAMP)).kind == "os"


def test_the_overrule_fix_order_shape_is_the_users(store):
    """§5 files it origin `jarvis`, no parent, no trigger, no stamp; the link in metadata"""
    wo = store.create_work_order("fix", origin="jarvis",
                                 metadata={"overrules": {"wo_id": "wo-1", "n": 2}})
    assert who(store, wo) == ops.Requestor("user", "you")


class _Rows:
    """Rows the store's own writers refuse to make: a cycle, a work-order parent"""

    def __init__(self, *rows):
        self.rows = {r["id"]: r for r in rows}

    def get_feature_order(self, rid):
        raise KeyError(rid)

    def get_work_order(self, rid):
        return self.rows[rid]


def test_a_parent_cycle_reads_as_the_os_and_ends():
    a = {"id": "wo-a", "origin": "jarvis", "parent_id": "wo-b"}
    rows = _Rows(a, {"id": "wo-b", "origin": "jarvis", "parent_id": "wo-a"})
    assert who(rows, a).kind == "os"


def test_a_work_order_root_is_judged_by_launched_by_os():
    """A parentless work order: its legacy clause is right for work orders"""
    child = {"id": "wo-c", "origin": "os", "parent_id": "wo-r"}
    user = _Rows(child, {"id": "wo-r", "origin": "jarvis"})
    legacy = _Rows(child, {"id": "wo-r", "origin": "jarvis", "trigger_wo_id": "wo-x"})
    assert who(user, child).kind == "user"
    assert who(legacy, child) == ops.Requestor("os", "os")


# -- the stamp, on the row


def _asked(store):
    wo = store.create_work_order("t", origin="schedule")
    store.add_assumption(wo["id"], "named the helper")
    (a,) = store.all_assumptions(wo["id"])
    return store.get_work_order(wo["id"]), a


def test_a_ruling_question_stamps_the_row_and_names_the_source(store):
    wo, a = _asked(store)
    neo_ = FakeNeo()
    autoreview.propose(store, neo_, "p", wo, a, [a])

    assert store.all_assumptions(wo["id"])[0]["requestor"] == autoreview.OWNER_REQUESTOR
    assert "# Who asked for this work order\nschedule" in neo_.asked[0]


def test_a_confirmation_question_stamps_the_row_too(store):
    wo, a = _asked(store)
    neo_ = FakeNeo()
    autoreview.propose_confirmation(store, neo_, "p", wo, a, [a])

    assert store.all_assumptions(wo["id"])[0]["requestor"] == "os"
    assert "# Who asked for this work order\nschedule" in neo_.asked[0]


def test_a_user_order_is_stamped_user_and_names_no_source(store):
    wo = store.create_work_order("t")
    store.add_assumption(wo["id"], "named the helper")
    (a,) = store.all_assumptions(wo["id"])
    neo_ = FakeNeo()
    autoreview.propose(store, neo_, "p", wo, a, [a])

    assert store.all_assumptions(wo["id"])[0]["requestor"] == "user"
    assert "Who asked" not in neo_.asked[0]


# -- a user-origin packet is byte-identical to before §2 (captured before the edit)


GOLD_WO = {"id": "wo-1", "status": "needs_review", "title": "t", "description": "d",
           "result_summary": "did it"}
GOLD_A = {"id": 3, "n": 2, "status": "pending", "content": "named the helper",
          "neo_question_id": None, "provisional_verdict": "accept",
          "provisional_model": "m", "provisional_reason": "fine"}
GOLD_S = {"id": 4, "n": 3, "status": "pending", "content": "other"}

GOLDEN_RULING = 'ASSUMPTION REVIEW — rule on assumption #2 of wo-1 in p, and on nothing else.\n\n# The assumption\nnamed the helper\n\n# The work order it was recorded against\nt\nd\n\n# THE SPEC SECTION THIS WORK ORDER WAS BUILT TO — authority you may CITE\n  (no spec section — this work order was not planned against one)\n\n# What the worker says it delivered\ndid it\n\n# Other assumptions on this order — do not rule on these: #3 [pending]\n\n# What has already been decided on this work order — authority you may CITE\n  (no prior decisions recorded)\n\nAnswer with `escalate`, `verdict` (`approve`; `deny` sends it to the user), `stakes` (`routine`|`high`), one-line `reason`. Where the record or spec settles it, cite the id, or cite the section item, rather than escalate.'  # noqa: E501

GOLDEN_EARLY = 'ASSUMPTION REVIEW — rule on assumption #2 of wo-1 in p, and on nothing else.\n\n# The assumption\nnamed the helper\n\n# The work order it was recorded against\nt\nd\n\n# THE SPEC SECTION THIS WORK ORDER WAS BUILT TO — authority you may CITE\n  (no spec section — this work order was not planned against one)\n\n# THE WORK ORDER IS STILL RUNNING\nNo result yet: you rule on an intention, provisionally.\n\n# Other assumptions on this order — do not rule on these: #3 [pending]\n\n# What has already been decided on this work order — authority you may CITE\n  (no prior decisions recorded)\n\nAnswer with `escalate`, `verdict` (`approve`; `deny` OBJECTS, `reason` reaches the worker), `stakes` (`routine`|`high`), one-line `reason`. Where the record or spec settles it, cite the id, or cite the section item, rather than escalate. Bundles independent claims, or too big to rule on? Set `refine` true; `reason` says what to split or narrow, and goes to the asker.'  # noqa: E501

GOLDEN_CONFIRM = "ASSUMPTION REVIEW — CONFIRM an earlier reading of assumption #2 of wo-1 in p, against the result that has now been delivered, and rule on nothing else.\n\n# The assumption\nnamed the helper\n\n# The reading formed WHILE THE WORKER WAS STILL TYPING\nverdict: accept (model: m)\nfine\nThat reading had no result summary in front of it. You do. NEITHER reading sees the diff: you are confirming a reading against what the worker says it DELIVERED, and whether the code matches this sentence is the validation panel's judgement, not yours.\n\n# The work order it was recorded against\nt\nd\n\n# THE SPEC SECTION THIS WORK ORDER WAS BUILT TO — authority you may CITE\n  (no spec section — this work order was not planned against one)\n\n# What the worker says it delivered\ndid it\n\n# The work order's other assumptions, for context only — do not rule on these\n  #3 [pending] other\n\n# What has already been decided on this work order — authority you may CITE\n  (no prior decisions recorded)\n\nYou are CONFIRMING that earlier reading against the delivered result. Confirming settles this assumption in the user's name; anything else leaves it with them, carrying both readings.\n\nAnswer with `escalate`, `verdict` (`approve` to confirm it, `deny` to send it to the user), `stakes` (`routine` or `high`) and a one-line `reason`. When the decision record above settles it, say which id in `reason` instead of escalating to ask for it again; when the spec section does, cite the section."  # noqa: E501


def test_the_default_builders_are_byte_identical_to_before():
    sib = [GOLD_A, GOLD_S]
    assert autoreview._ruling_question("p", GOLD_WO, GOLD_A, sib) == GOLDEN_RULING  # noqa: SLF001
    assert autoreview._ruling_question(   # noqa: SLF001
        "p", GOLD_WO, GOLD_A, sib, early=True) == GOLDEN_EARLY
    assert autoreview._confirm_question("p", GOLD_WO, GOLD_A, sib) == GOLDEN_CONFIRM  # noqa: SLF001


def test_a_label_adds_one_block_after_the_work_order_and_nothing_else():
    sib = [GOLD_A, GOLD_S]
    block = "# Who asked for this work order\nmorpheus\n\n"
    anchor = "# THE SPEC SECTION"
    for gold, out in (
            (GOLDEN_RULING, autoreview._ruling_question(   # noqa: SLF001
                "p", GOLD_WO, GOLD_A, sib, requestor_label="morpheus")),
            (GOLDEN_CONFIRM, autoreview._confirm_question(   # noqa: SLF001
                "p", GOLD_WO, GOLD_A, sib, requestor_label="morpheus"))):
        assert out == gold.replace(anchor, block + anchor, 1)


# -- the column, stamped once at create (docs/superpowers/specs/2026-11-09-yours-requestor-stamp-and-tree-notice.md §2)


@pytest.fixture
def two(jarvis_home, tmp_path, monkeypatch):
    """Two registered projects, so a finding can be filed across them"""
    paths = {"proj_a": tmp_path / "a", "proj_b": tmp_path / "b"}
    monkeypatch.setattr(ops, "registered_project_paths", lambda **_: paths)
    return paths


def col(path, order_id):
    s = ProjectStore(path)
    try:
        try:
            return s.get_feature_order(order_id)["requestor"]
        except KeyError:
            return s.get_work_order(order_id)["requestor"]
    finally:
        s.close()


def test_a_cli_shaped_create_is_the_users(two):
    wo = ops.create_work_order("proj_a", "t")
    fo = ops.create_feature_order("proj_a", "f", description="d")
    do = ops.create_decision_order("proj_a", "which", "compare")
    io = ops.create_improvement_order("proj_a", "slow", description="d", refs=["wo-1"])
    for row in (wo, fo, do, io):
        assert col(two["proj_a"], row["id"]) == "user"


def test_the_same_create_inside_a_worker_session_is_the_os(two, monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-worker")
    wo = ops.create_work_order("proj_a", "t")
    fo = ops.create_feature_order("proj_a", "f", description="d")
    assert col(two["proj_a"], wo["id"]) == col(two["proj_a"], fo["id"]) == "os"


def test_an_explicit_stamp_wins_and_a_stray_value_is_refused(two, monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-worker")
    wo = ops.create_work_order("proj_a", "t", requestor="user")
    assert col(two["proj_a"], wo["id"]) == "user"
    with pytest.raises(ops.OpsError, match="requestor"):
        ops.create_work_order("proj_a", "t", requestor="jarvis")
    s = ProjectStore(two["proj_a"])
    try:
        with pytest.raises(ValueError, match="requestor"):
            s.create_feature_order("f", requestor="you")
    finally:
        s.close()


def test_plan_children_and_the_manager_inherit_a_user_feature(two, monkeypatch):
    fo = ops.create_feature_order("proj_a", "f", description="d")
    # The daemon has no JARVIS_WO_ID either, but its rows copy the parent, never guess
    monkeypatch.setenv("JARVIS_WO_ID", "wo-planner")
    s = ProjectStore(two["proj_a"])
    try:
        kids = s.create_plan_children(fo["id"], [
            {"key": "a", "title": "A", "description": "d", "needs": []}], manager=True)
        rows = s.feature_children(fo["id"], kinds=("worker", "manager"))
    finally:
        s.close()
    assert len(kids) == 1 and len(rows) == 2
    assert {r["requestor"] for r in rows} == {"user"}


def test_a_steward_stamped_root_is_the_os(two):
    wo = ops.create_work_order("proj_a", "t", metadata=STAMP)
    assert col(two["proj_a"], wo["id"]) == "os"


@pytest.mark.parametrize("target", ["proj_a", "proj_b"])
def test_a_finding_order_of_a_user_io_is_the_users_even_stamped(two, target):
    from types import SimpleNamespace

    io = ops.create_improvement_order("proj_a", "slow", description="d", refs=["wo-1"])
    decision = SimpleNamespace(stamp=STAMP[steward.STAMP_KEY], budget_usd=None,
                               decision_id=0)
    f = {"key": "k", "proposed_orders": [
        {"title": "fix it", "description": "d", "project": target},
        {"title": "big fix", "description": "d", "project": target, "type": "feature"}]}
    made, error = ops._file_finding_orders("proj_a", io["id"], f, decision)  # noqa: SLF001
    assert not error and len(made) == 2
    assert [col(two[target], m["id"]) for m in made] == ["user", "user"]


def test_a_morpheus_shaped_investigation_is_the_os(two):
    """`origin='jarvis'` like morpheus files it, in a process with no JARVIS_WO_ID"""
    wo = ops.create_work_order("proj_a", "stuck one")
    inv = ops.create_investigation_order("proj_a", wo["id"], "it is stuck", origin="jarvis",
                                         stamp=STAMP[steward.STAMP_KEY])
    assert col(two["proj_a"], inv["id"]) == "os"
    bare = ops.create_investigation_order("proj_b", ops.create_work_order("proj_b", "x")["id"],
                                          "stuck", origin="jarvis", requestor="os")
    assert col(two["proj_b"], bare["id"]) == "os"


def test_the_column_is_read_first(store):
    """A stamped row is never re-derived from origin or a steward stamp"""
    user = store.create_work_order("t", origin="schedule", metadata=STAMP, requestor="user")
    assert who(store, user) == ops.Requestor("user", "you")
    os_ = store.create_work_order("t", origin="schedule", requestor="os")
    assert who(store, os_) == ops.Requestor("os", "schedule")
    typed = store.create_work_order("t", requestor="os")
    assert who(store, typed) == ops.Requestor("os", "os")


def test_a_null_legacy_finding_order_follows_its_io(store):
    io = store.create_feature_order("slow", kind="improvement")
    found = store.create_work_order("fix", metadata={ops.ORIGIN_IO_KEY: io["id"], **STAMP})
    assert found["requestor"] is None
    assert who(store, found) == ops.Requestor("user", "you")
    lost = store.create_work_order("fix", metadata={ops.ORIGIN_IO_KEY: "io-gone"})
    assert who(store, lost) == ops.Requestor("os", "os")
