"""The self-evolution registry's rows: `detectors`, `remedy_rules`, `rule_fires`.

Three properties carry the weight here, and each has a block below.

1. **Nothing unvalidated reaches the table.** A condition is parsed on INSERT, a
   `gap_class` must be a slug, a remedy names a primitive from the closed
   `remedies.REMEDIES`. A row the engine cannot read is a row that silently answers a
   question nobody asked.
2. **Rows are never deleted.** Retraction writes `retired_at` and a reason; the row stays
   listed with `include_retired=True`. Same discipline as `gate_rules` and the knowledge
   base, for the same reason: what the OS believed and when is evidence.
3. **The tables open on a LIVE database.** `test_a_database_written_before_these_tables_
   existed_opens_and_works` builds an `os.db` from the previous release's `SCHEMA` and
   opens the current `CentralStore` on it. A new table is untested until that passes.

Spec: docs/specs/2026-09-27-self-evolution.md §3.1, §3.3, §3.4.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import textwrap

import pytest

from jarvis import db, rules
from jarvis.central_store import CentralStore

COND = {"all": [{"field": "status", "op": "eq", "value": "needs_review"},
                {"field": "seconds_in_status", "op": "gte", "value": 3600}]}


@pytest.fixture()
def store(jarvis_home):
    s = CentralStore()
    yield s
    s.close()


def _detector(store, **kw):
    kw.setdefault("gap_class", "stale-panel-hold")
    kw.setdefault("condition", COND)
    return store.add_detector(**kw)


# -- nothing unvalidated reaches the table ----------------------------------------------


def test_a_detector_round_trips_and_is_created_in_dry_run(store):
    det = _detector(store, project="proj_a", summary="a hold nobody clears",
                    source="io", io_id="io-1", fix_wo_id="wo-1",
                    issue_url="https://example/786", pr_url="https://example/pr",
                    arm_threshold=5, seed_version=1)
    assert det["id"].startswith("dt-")
    assert det["status"] == rules.DRY_RUN
    assert det["arm_threshold"] == 5
    assert det["hits"] == 0 and det["last_fired"] is None
    # stored canonically, so the column holds what `parse_condition` returned
    assert json.loads(det["condition"]) == rules.parse_condition(COND)
    assert store.get_detector(det["id"]) == det
    assert store.get_detector("dt-nope") is None


def test_there_is_no_argument_that_creates_an_armed_detector(store):
    with pytest.raises(TypeError):
        store.add_detector(gap_class="g", condition=COND, status=rules.ARMED)
    assert _detector(store)["status"] == rules.DRY_RUN


def test_an_invalid_condition_is_refused_at_insert_and_the_table_stays_empty(store):
    with pytest.raises(rules.RulesError) as e:
        store.add_detector(gap_class="g", condition={"all": [
            {"field": "no_such_field", "op": "eq", "value": 1}]})
    assert "no_such_field" in str(e.value)
    assert store.list_detectors() == []


def test_a_gap_class_that_is_not_a_slug_is_refused(store):
    with pytest.raises(ValueError) as e:
        store.add_detector(gap_class="Stale Panel Hold", condition=COND)
    assert "gap_class" in str(e.value)
    assert store.list_detectors() == []


def test_a_condition_may_be_given_as_json_text(store):
    det = _detector(store, condition=json.dumps(COND))
    assert json.loads(det["condition"]) == rules.parse_condition(COND)


def test_a_remedy_rule_names_a_primitive_from_the_closed_registry(store):
    det = _detector(store)
    rule = store.add_remedy_rule(det["id"], "nudge", params={},
                                 argument="ask it where it is")
    assert rule["id"].startswith("rm-")
    assert rule["status"] == rules.DRY_RUN
    assert rule["detector_id"] == det["id"]
    assert store.remedy_rules_for(det["id"]) == [rule]

    with pytest.raises(ValueError) as e:
        store.add_remedy_rule(det["id"], "rm_minus_rf")
    assert "rm_minus_rf" in str(e.value)
    assert len(store.remedy_rules_for(det["id"])) == 1


def test_a_remedy_rule_refuses_an_unknown_or_retracted_detector(store):
    with pytest.raises(KeyError):
        store.add_remedy_rule("dt-nope", "nudge")
    det = _detector(store)
    store.retract_detector(det["id"], "superseded")
    with pytest.raises(ValueError) as e:
        store.add_remedy_rule(det["id"], "nudge")
    assert "retracted" in str(e.value)


# -- scope: '' is every project ---------------------------------------------------------


def test_list_detectors_for_a_project_returns_its_rows_and_the_fleet_wide_ones(store):
    mine = _detector(store, project="proj_a")
    fleet = _detector(store, project="", gap_class="red-base-inherited")
    _detector(store, project="proj_b", gap_class="overtaken-release-order")
    ids = [d["id"] for d in store.list_detectors(project="proj_a")]
    assert set(ids) == {mine["id"], fleet["id"]}
    assert [d["id"] for d in store.detectors_for_gap("red-base-inherited", "proj_a")] \
        == [fleet["id"]]
    assert store.detectors_for_gap("overtaken-release-order", "proj_a") == []


def test_list_detectors_filters_by_gap_class_and_status(store):
    a = _detector(store, gap_class="stale-panel-hold")
    _detector(store, gap_class="red-base-inherited")
    assert [d["id"] for d in store.list_detectors(gap_class="stale-panel-hold")] == \
        [a["id"]]
    assert len(store.list_detectors(status=rules.DRY_RUN)) == 2
    assert store.list_detectors(status=rules.ARMED) == []


# -- retraction never deletes -----------------------------------------------------------


def test_retracting_a_detector_hides_it_retracts_its_remedies_and_never_deletes(store):
    det = _detector(store)
    rule = store.add_remedy_rule(det["id"], "nudge")
    out = store.retract_detector(det["id"], "the gap was fixed in code")

    assert out["status"] == rules.RETRACTED
    assert out["retired_at"] and out["retired_reason"] == "the gap was fixed in code"
    assert store.list_detectors() == []
    assert [d["id"] for d in store.list_detectors(include_retired=True)] == [det["id"]]
    assert store.get_detector(det["id"]) is not None

    assert store.remedy_rules_for(det["id"]) == []
    retired = store.remedy_rules_for(det["id"], include_retired=True)
    assert [r["id"] for r in retired] == [rule["id"]]
    assert retired[0]["status"] == rules.RETRACTED
    assert det["id"] in retired[0]["retired_reason"]

    with pytest.raises(ValueError):
        store.retract_detector(det["id"], "again")


def test_retracting_a_remedy_rule_never_deletes_and_refuses_a_second_time(store):
    det = _detector(store)
    rule = store.add_remedy_rule(det["id"], "unblock")
    out = store.retract_remedy_rule(rule["id"], "the primitive changed shape")
    assert out["status"] == rules.RETRACTED and out["retired_at"]
    assert store.remedy_rules_for(det["id"]) == []
    assert len(store.remedy_rules_for(det["id"], include_retired=True)) == 1
    with pytest.raises(ValueError):
        store.retract_remedy_rule(rule["id"], "again")
    with pytest.raises(KeyError):
        store.retract_remedy_rule("rm-nope", "never existed")


# -- fires: what counts as a hit, and the dedupe memory ---------------------------------


def _fire(store, det, outcome, **kw):
    kw.setdefault("project", "proj_a")
    kw.setdefault("order_id", "wo-1")
    kw.setdefault("order_kind", "work_order")
    kw.setdefault("fingerprint", "fp-1")
    kw.setdefault("mode", rules.DRY_RUN)
    return store.record_rule_fire(detector_id=det["id"], outcome=outcome, **kw)


def test_only_a_hit_increments_the_counter(store):
    det = _detector(store)
    _fire(store, det, rules.RECORDED)
    assert store.get_detector(det["id"])["hits"] == 1
    assert store.get_detector(det["id"])["last_fired"] is not None

    for outcome in (rules.UNREADABLE, rules.REFUSED, rules.CLEARED):
        _fire(store, det, outcome)
    assert store.get_detector(det["id"])["hits"] == 1

    for outcome in (rules.PROPOSED, rules.APPLIED):
        _fire(store, det, outcome)
    assert store.get_detector(det["id"])["hits"] == 3


def test_an_unknown_outcome_is_refused(store):
    det = _detector(store)
    with pytest.raises(ValueError):
        _fire(store, det, "fixed")


def test_detail_longer_than_the_cap_is_stored_bounded(store):
    det = _detector(store)
    fire = _fire(store, det, rules.RECORDED, detail="x" * (rules.FACTS_CHARS + 500))
    assert len(fire["detail"]) <= rules.FACTS_CHARS + 80
    assert fire["detail"] == rules.bound("x" * (rules.FACTS_CHARS + 500))


def test_open_rule_fire_is_the_newest_uncleared_one_and_closing_ends_it(store):
    det = _detector(store)
    first = _fire(store, det, rules.RECORDED)
    second = _fire(store, det, rules.RECORDED)
    assert store.open_rule_fire(det["id"], "wo-1")["id"] == second["id"]
    assert store.open_rule_fire(det["id"], "wo-2") is None

    closed = store.close_rule_fire(second["id"], now=second["ts"] + 90)
    assert closed["cleared_at"] and closed["cleared_seconds"] == pytest.approx(90)
    assert store.get_detector(det["id"])["last_cleared"] == closed["cleared_at"]
    # the older one is still open, and is what the dedupe memory now answers with
    assert store.open_rule_fire(det["id"], "wo-1")["id"] == first["id"]

    with pytest.raises(ValueError):
        store.close_rule_fire(second["id"])
    with pytest.raises(KeyError):
        store.close_rule_fire(99999)


def test_list_rule_fires_is_newest_first_and_filters(store):
    det = _detector(store)
    other = _detector(store, gap_class="red-base-inherited")
    a = _fire(store, det, rules.RECORDED)
    b = _fire(store, det, rules.UNREADABLE, order_id="wo-2")
    c = _fire(store, other, rules.RECORDED)
    assert [f["id"] for f in store.list_rule_fires()] == [c["id"], b["id"], a["id"]]
    assert [f["id"] for f in store.list_rule_fires(detector_id=det["id"])] == \
        [b["id"], a["id"]]
    assert [f["id"] for f in store.list_rule_fires(order_id="wo-2")] == [b["id"]]
    assert [f["id"] for f in store.list_rule_fires(outcome=rules.UNREADABLE)] == [b["id"]]
    assert [f["id"] for f in store.list_rule_fires(project="proj_a", limit=1)] == [c["id"]]


# -- the migration ----------------------------------------------------------------------


def _previous_schema() -> str:
    """The `SCHEMA` literal as it was at HEAD — before these three tables existed."""
    src = subprocess.run(["git", "show", "HEAD:src/jarvis/central_store.py"],
                         capture_output=True, text=True, check=True).stdout
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "SCHEMA" for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("no SCHEMA assignment in HEAD:src/jarvis/central_store.py")


def test_a_database_written_before_these_tables_existed_opens_and_works(jarvis_home,
                                                                        tmp_path):
    """A new table is untested until a LIVE-SHAPED database opens on it.

    `CREATE TABLE IF NOT EXISTS` creates the three new tables in an `os.db` that predates
    them, which is exactly why they need no `ADDED_COLUMNS` entry — but "exactly why" is
    an argument, and this is the check. The old rows have to survive it too: a schema
    change that opens and silently drops what was already there passes every test that
    only writes.
    """
    path = tmp_path / "old-os.db"
    old = db.connect(path)
    old.executescript(_previous_schema())
    old.execute("""INSERT INTO gate_rules (id, ts, role, kind, test, pattern)
                   VALUES ('gr-old', 1.0, 'match', 'release', 'regex', 'shipit')""")
    old.execute("""INSERT INTO knowledge (id, ts, project, topic, content)
                   VALUES ('kn-old', 1.0, '', 'a topic', 'a thing believed earlier')""")
    old.commit()
    old.close()

    store = CentralStore(path)
    try:
        assert store.get_gate_rule("gr-old")["pattern"] == "shipit"
        assert store.get_knowledge("kn-old")["content"] == "a thing believed earlier"
        det = _detector(store, project="proj_a")
        store.add_remedy_rule(det["id"], "nudge")
        fire = _fire(store, det, rules.RECORDED)
        assert store.open_rule_fire(det["id"], "wo-1")["id"] == fire["id"]
    finally:
        store.close()


# -- scope: an io-learned rule may not police the fleet ----------------------------------


def test_an_io_learned_detector_may_not_be_fleet_wide(store):
    """§3: fleet-wide is the POWERFUL case, so it is the reviewed one."""
    with pytest.raises(ValueError) as e:
        _detector(store, source="io", project="")
    assert "io" in str(e.value) and "project" in str(e.value)
    assert store.list_detectors(include_retired=True) == []

    scoped = _detector(store, source="io", project="jarvis_os")
    assert scoped["project"] == "jarvis_os"


@pytest.mark.parametrize("source", ["builtin", "user"])
def test_builtin_and_user_detectors_may_be_fleet_wide(store, source):
    det = _detector(store, source=source, project="")
    assert det["project"] == "" and det["source"] == source


# -- a remedy row follows its detector ---------------------------------------------------


def test_add_remedy_rule_always_writes_dry_run(store):
    det = _detector(store)
    assert store.add_remedy_rule(det["id"], "nudge")["status"] == rules.DRY_RUN


def test_this_section_exposes_no_verb_that_arms_a_remedy_row(store):
    """Arming lands with the alarm bridge, in one transaction over the pair. Nothing
    here may set a remedy row to `armed` — that would make the pair a combination §3.3
    says cannot exist."""
    assert [n for n in dir(CentralStore) if "arm" in n.lower()] == []
    for verb in (CentralStore.add_remedy_rule, CentralStore.retract_remedy_rule,
                 CentralStore.retract_detector):
        body = ast.parse(textwrap.dedent(inspect.getsource(verb))).body[0]
        if ast.get_docstring(body) is not None:
            body.body = body.body[1:]       # the prose EXPLAINS arming; the code may not
        assert rules.ARMED not in ast.unparse(body)
        assert "ARMED" not in ast.unparse(body)


def test_retracting_a_detector_leaves_the_pair_effectively_retracted(store):
    det = _detector(store)
    rule = store.add_remedy_rule(det["id"], "nudge")
    store.retract_detector(det["id"], "the gap was fixed in code")

    dead_det = store.get_detector(det["id"])
    dead_rule = store.get_remedy_rule(rule["id"])
    assert dead_rule["status"] == rules.RETRACTED
    assert "the gap was fixed in code" in dead_rule["retired_reason"]
    assert "followed" in dead_rule["retired_reason"]
    assert rules.effective_status(dead_det, dead_rule) == rules.RETRACTED


def test_retract_remedy_rule_works_under_a_live_detector(store):
    det = _detector(store)
    rule = store.add_remedy_rule(det["id"], "nudge")
    store.retract_remedy_rule(rule["id"], "the remedy is replaced")

    live = store.get_detector(det["id"])
    assert live["status"] == rules.DRY_RUN and live["retired_at"] is None
    dead = store.get_remedy_rule(rule["id"])
    assert rules.effective_status(live, dead) == rules.RETRACTED
    assert rules.effective_status(live, store.add_remedy_rule(
        det["id"], "unblock")) == rules.DRY_RUN
