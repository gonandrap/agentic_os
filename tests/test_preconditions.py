"""A precondition is a row, not prose.

docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md. wo-314221b5 was
filed with `DO NOT START until …` as the first line of its brief, `depends_on = []` and
nothing else holding it, and was claimed, dispatched and run for 0.93 USD. The fix is a
row the claim SQL can evaluate (§2.1-§2.2), written only by an explicit flag (§2.3), with
the prose variant DETECTED and never attached (§2.4-§2.5).
"""

from __future__ import annotations

import inspect
import json

import pytest

from jarvis import cli, gaps, invariants, ops, preconditions, timeline
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

HOLD_BRIEF = "DO NOT START until SCHEDULED_ACTORS[validating] names the held-round actor"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def central(jarvis_home):
    c = CentralStore()
    yield c
    c.close()


# -- §2.1: the rows ------------------------------------------------------------------

def test_a_precondition_round_trips_and_a_repeat_is_idempotent(store):
    wo = store.create_work_order("the 5a predicate fix")
    store.add_precondition(wo["id"], " bl-d5baa28b ", note="the predicate fix")
    store.add_precondition(wo["id"], "bl-d5baa28b", note="the predicate fix")

    rows = store.open_preconditions(wo["id"])
    assert [(r["ref"], r["note"], r["status"], r["cleared_by"]) for r in rows] == [
        ("bl-d5baa28b", "the predicate fix", "open", "")]
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert kinds.count("precondition_added") == 1


def test_clearing_a_row_writes_the_event_and_hides_it(store):
    wo = store.create_work_order("the 5a predicate fix")
    store.add_precondition(wo["id"], "bl-d5baa28b")

    store.clear_precondition("bl-d5baa28b", by="user")

    assert store.open_preconditions(wo["id"]) == []
    cleared = [e for e in store.list_events(wo["id"])
               if e["kind"] == "precondition_cleared"]
    assert len(cleared) == 1


def test_deleting_the_work_order_cascades_the_rows(store):
    wo = store.create_work_order("the 5a predicate fix")
    store.add_precondition(wo["id"], "bl-d5baa28b")

    store.delete_work_order(wo["id"])

    assert store.conn.execute("SELECT COUNT(*) c FROM preconditions").fetchone()["c"] == 0


# -- §2.3: the only writer is the flag -----------------------------------------------

def test_the_flag_writes_the_rows_and_repeats_cleanly(started, store):
    first = ops.create_work_order("proj_a", "the 5a predicate fix",
                                  blocked_by=["bl-d5baa28b", "bl-d5baa28b"])

    rows = store.open_preconditions(first["id"])
    assert [r["ref"] for r in rows] == ["bl-d5baa28b"]
    assert store.claim_next_pending() is None


def test_the_repeatable_flag_reaches_the_rows_through_the_cli(started, store, capsys):
    assert cli.main(["wo", "create", "proj_a", "wire the actor",
                     "--description", HOLD_BRIEF,
                     "--blocked-by", "bl-d5baa28b",
                     "--blocked-by", "kn-5845939d", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["blocked_by"] == ["bl-d5baa28b", "kn-5845939d"]
    assert [r["ref"] for r in store.open_preconditions(out["created"])] == [
        "bl-d5baa28b", "kn-5845939d"]
    assert "unblock" in out["note"]


@pytest.mark.parametrize("phrase", preconditions.STOP_PHRASES)
def test_a_brief_stating_a_hold_is_created_normally_with_the_flag(started, store,
                                                                  phrase):
    wo = ops.create_work_order("proj_a", "wire it",
                               description=f"{phrase} the predicate fix ships.",
                               blocked_by=["bl-d5baa28b"])

    assert store.get_work_order(wo["id"])["status"] == "pending"


def test_the_storage_layer_infers_nothing(store):
    """`ProjectStore.create_work_order` takes no policy: the same brief, no row."""
    wo = store.create_work_order("wire it", description=HOLD_BRIEF)

    assert store.open_preconditions(wo["id"]) == []
    assert "blocked_by" not in inspect.signature(
        ProjectStore.create_work_order).parameters


# -- §2.4: the refusal ---------------------------------------------------------------

@pytest.mark.parametrize("phrase", preconditions.STOP_PHRASES)
def test_an_unwired_hold_is_refused_and_leaves_no_row(started, store, phrase):
    sentence = f"{phrase} the 5a predicate fix ships"
    with pytest.raises(ops.OpsError) as e:
        ops.create_work_order("proj_a", "wire it", description=f"{sentence}.")

    assert sentence.lower() in str(e.value).lower()
    assert "--depends-on" in str(e.value) and "--blocked-by" in str(e.value)
    assert store.list_work_orders(statuses=None, include_hidden=True) == []


def test_a_hold_in_the_title_is_refused_too(started):
    with pytest.raises(ops.OpsError, match="stop condition"):
        ops.create_work_order("proj_a", HOLD_BRIEF)


def test_a_dependency_edge_is_wiring_enough(started, store):
    dep = ops.create_work_order("proj_a", "the predicate fix")
    wo = ops.create_work_order("proj_a", "wire it", description=HOLD_BRIEF,
                               depends_on=[dep["id"]])

    assert store.get_work_order(wo["id"])["status"] == "pending"


@pytest.mark.parametrize("text", [
    "wait for the predicate fix first",
    "after the predicate fix ships, wire the actor",
    "once the predicate fix lands, wire the actor",
    "blocked on the predicate fix",
    "needs the predicate fix",
])
def test_the_named_false_negatives_create_normally(started, text):
    """§2.4: precision over recall. These read as sequencing as often as a stop
    condition, and over-matching leaves an operator unable to file."""
    assert ops.create_work_order("proj_a", "wire it", description=text)


@pytest.mark.parametrize("text", ["HELD until", "held until   ", "nothing held until"])
def test_a_phrase_with_no_condition_after_it_names_no_hold(text):
    assert preconditions.states_hold("wire it", text) is None


def test_the_matched_sentence_is_the_line(started):
    assert preconditions.states_hold("wire it", f"- **{HOLD_BRIEF}**\nthen do the rest") \
        is not None


def test_an_empty_ref_is_refused(started):
    with pytest.raises(ops.OpsError):
        ops.create_work_order("proj_a", "wire it", blocked_by=["  "])


# -- §2.5: the detector --------------------------------------------------------------

def _unwired(store: ProjectStore) -> str:
    """The shape the creation refusal cannot reach: a planner child, or an order that
    predates the refusal — written through the store, as those are."""
    return store.create_work_order("wire the actor", description=HOLD_BRIEF)["id"]


def test_the_detector_fires_for_an_unwired_prose_hold(store):
    wo_id = _unwired(store)

    found = [v for v in invariants.check_precondition_is_wired(store)]

    assert [v.wo_id for v in found] == [wo_id]
    assert found[0].invariant == invariants.INV_PRECONDITION_UNWIRED
    assert HOLD_BRIEF.lower() in found[0].context["matched"].lower()
    assert found[0].owed == "" and found[0].repaired is False


def test_the_detector_is_silent_once_either_wiring_exists(store):
    row = store.create_work_order("wire the actor", description=HOLD_BRIEF)
    store.add_precondition(row["id"], "bl-d5baa28b")
    dep = store.create_work_order("the predicate fix")
    store.create_work_order("wire it too", description=HOLD_BRIEF,
                            depends_on=[dep["id"]])

    assert list(invariants.check_precondition_is_wired(store)) == []


def test_the_detector_is_silent_for_a_settled_order(store):
    wo_id = _unwired(store)
    store.set_status(wo_id, "completed")

    assert list(invariants.check_precondition_is_wired(store)) == []


def test_the_detector_is_registered_on_the_fast_cadence():
    assert invariants.check_precondition_is_wired in invariants.INVARIANTS
    assert invariants.check_precondition_is_wired not in invariants.SLOW_INVARIANTS
    assert (gaps.by_invariant()[invariants.INV_PRECONDITION_UNWIRED]
            == "unwired-precondition")


def test_the_violation_notifies_and_raises_no_attention_flag(project, catalog_file):
    store = ProjectStore(project)
    wo_id = _unwired(store)
    daemon = Daemon(load_catalog(catalog_file))
    spec = load_catalog(catalog_file).projects[0]

    daemon.check_invariants(spec, store)

    assert store.get_work_order(wo_id)["needs_attention"] == 0
    notes = [n for n in store.unrouted_notifications()
             if invariants.INV_PRECONDITION_UNWIRED in n["title"]]
    assert len(notes) == 1
    store.close()


# -- §2.6: re-derivation -------------------------------------------------------------

@pytest.mark.parametrize("status", ["promoted", "done", "dropped"])
def test_a_settled_backlog_item_clears_the_row(started, store, central, status):
    item = central.add_backlog("proj_a", "the 5a predicate fix")
    wo = ops.create_work_order("proj_a", "wire the actor", blocked_by=[item["id"]])
    central.mark_backlog(item["id"], status)
    spec = started.catalog.projects[0]

    started.clear_settled_preconditions(spec, store)

    assert store.open_preconditions(wo["id"]) == []
    cleared = [e for e in store.list_events(wo["id"])
               if e["kind"] == "precondition_cleared"]
    assert cleared and store.conn.execute(
        "SELECT cleared_by FROM preconditions WHERE wo_id=?",
        (wo["id"],)).fetchone()["cleared_by"] == "os"
    assert store.claim_next_pending()["id"] == wo["id"]


def test_an_open_backlog_item_does_not_clear_the_row(started, store, central):
    item = central.add_backlog("proj_a", "the 5a predicate fix")
    wo = ops.create_work_order("proj_a", "wire the actor", blocked_by=[item["id"]])
    spec = started.catalog.projects[0]

    started.clear_settled_preconditions(spec, store)

    assert [r["ref"] for r in store.open_preconditions(wo["id"])] == [item["id"]]


@pytest.mark.parametrize("ref", ["bl-nosuchitem", "kn-5845939d"])
def test_a_ref_the_os_cannot_derive_stays_open(started, store, ref):
    wo = ops.create_work_order("proj_a", "wire the actor", blocked_by=[ref])
    spec = started.catalog.projects[0]

    started.clear_settled_preconditions(spec, store)

    assert [r["ref"] for r in store.open_preconditions(wo["id"])] == [ref]


def test_the_tick_clears_before_it_dispatches():
    """§2.6: a hold cleared on this tick is claimed on this tick."""
    src = inspect.getsource(Daemon.tick)
    assert (src.index("clear_settled_preconditions")
            < src.index("self.hold_red_release")
            < src.index("self.dispatch_pending"))


# -- §2.6: `jarvis wo unblock` -------------------------------------------------------

def test_unblock_clears_the_rows_the_daemon_can_never_clear(started, store):
    wo = ops.create_work_order("proj_a", "wire the actor",
                               blocked_by=["kn-5845939d", "bl-nosuchitem"])

    out = ops.unblock_work_order(wo["id"])

    assert sorted(out["preconditions_cleared"]) == ["bl-nosuchitem", "kn-5845939d"]
    assert store.open_preconditions(wo["id"]) == []
    assert store.claim_next_pending()["id"] == wo["id"]


def test_unblock_refuses_a_live_backlog_hold_and_names_all(started, store, central):
    item = central.add_backlog("proj_a", "the 5a predicate fix")
    wo = ops.create_work_order("proj_a", "wire the actor", blocked_by=[item["id"]])

    with pytest.raises(ops.OpsError) as e:
        ops.unblock_work_order(wo["id"])
    assert item["id"] in str(e.value) and "--all" in str(e.value)

    out = ops.unblock_work_order(wo["id"], drop_all=True)
    assert out["preconditions_cleared"] == [item["id"]]
    assert store.open_preconditions(wo["id"]) == []


def test_unblock_on_an_order_with_neither_kind_of_hold_says_so(started):
    wo = ops.create_work_order("proj_a", "free")

    with pytest.raises(ops.OpsError, match="not blocked"):
        ops.unblock_work_order(wo["id"])


# -- §2.6: the timeline --------------------------------------------------------------

@pytest.mark.parametrize("kind", ["precondition_added", "precondition_cleared"])
def test_both_events_describe_themselves_and_are_not_debug(kind):
    label, _ = timeline._describe(kind, {"ref": "bl-d5baa28b", "by": "user",
                                         "note": "the predicate fix"})
    assert label and label != kind
    assert kind not in timeline.DEBUG_KINDS


# -- §2.7: surfacing -----------------------------------------------------------------

def test_the_status_label_names_the_hold_and_the_way_out(store):
    wo = store.create_work_order("wire the actor")
    store.add_precondition(wo["id"], "bl-d5baa28b", note="the predicate fix")

    label = invariants.status_label(store, store.get_work_order(wo["id"]))

    assert "bl-d5baa28b" in label and f"jarvis wo unblock {wo['id']}" in label
    assert "the predicate fix" in label


def test_the_precondition_label_outranks_a_dependency(store):
    dep = store.create_work_order("the predicate fix", status="running")
    wo = store.create_work_order("wire the actor", depends_on=[dep["id"]])
    store.add_precondition(wo["id"], "bl-d5baa28b")

    label = invariants.status_label(store, store.get_work_order(wo["id"]))

    assert "bl-d5baa28b" in label


def test_ops_open_preconditions_is_the_one_derivation(store):
    wo = store.create_work_order("wire the actor")
    store.add_precondition(wo["id"], "bl-d5baa28b")
    row = store.get_work_order(wo["id"])

    assert [p["ref"] for p in ops.open_preconditions(store, row)] == ["bl-d5baa28b"]
    # NOT widened: its entries render as work order links (§2.7).
    assert ops.blocked_by(store, row) == []


def test_the_health_sweep_reports_a_held_order_as_blocked(store):
    from jarvis import health

    wo = store.create_work_order("wire the actor")
    store.add_precondition(wo["id"], "bl-d5baa28b")

    class _Cfg:
        health_reassert_blockers = health.BLOCKERS

    blocker = health._work_order_blocker(store, store.get_work_order(wo["id"]), _Cfg())

    assert blocker == "dependency"
    assert "precondition" not in health.BLOCKERS


def test_waiting_on_names_the_hold(store):
    wo = store.create_work_order("wire the actor")
    store.add_precondition(wo["id"], "bl-d5baa28b")

    answer = ops.waiting_on(store, store.get_work_order(wo["id"]))

    assert answer["what"] == "precondition"
    assert answer["stalled"] is False
    assert "bl-d5baa28b" in answer["detail"]


def test_a_designed_wait_is_not_an_attention_item(store):
    wo = store.create_work_order("wire the actor")
    store.add_precondition(wo["id"], "bl-d5baa28b")
    row = store.get_work_order(wo["id"])

    assert invariants.true_blockers(store, row) == []
    actor = ops.scheduled_actor(store, row)
    assert actor is not None and actor.pass_name == "Daemon.dispatch_pending"
    assert "precondition" in actor.why
