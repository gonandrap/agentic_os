"""An alarm can name a FEATURE ORDER as its subject and a probe as its source — §1 of
docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.

NOTHING RAISES A HEALTH FINDING YET and no model is called anywhere in this module. The
fixtures write findings through `ProjectStore.add_finding` outright, which is the whole
of what this section ships: §4 is what will decide to call it.

THE FIXTURE THAT DISCRIMINATES IS `_diverged`. For every alarm on the tree today the
subject IS the carrier, so swapping "title comes from the subject" for "title comes from
the carrier" is a no-op on the entire suite. Only a feature and a carrier with different
titles and different statuses can tell the two rules apart, and only an attention flag on
the carrier alone can prove `live` did not move with them.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import NO_TURN, ProjectStore

FIRED = "the plan has not moved in three days and two children are failing"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(started):
    s = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        yield s
    finally:
        s.close()


def _feature(store, title="the feature", status="executing"):
    fo = store.create_feature_order(title)
    store.set_feature_status(fo["id"], status)
    return fo["id"]


def _diverged(store, *, flag=True):
    """One feature and one carrier that disagree about everything a surface renders.

    `the feature` is `executing`; `the carrier` is `running`; the attention flag is on
    the CARRIER only. Returns (fo_id, carrier_wo_id, alarm_id).
    """
    fo_id = _feature(store)
    carrier = store.create_work_order("the carrier", parent_id=fo_id, kind="manager",
                                      status="running")["id"]
    alarm = store.add_finding(carrier, kind="stalled-plan", reason=FIRED,
                              source="health", probe="stalled-plan",
                              subject_kind="feature_order", fo_id=fo_id)
    if flag:
        store.flag_attention(carrier, f"{fo_id}: {FIRED}")
    return fo_id, carrier, alarm["id"]


def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app
    return TestClient(create_app(), follow_redirects=False)


# -- the carrier ---------------------------------------------------------------------


def test_the_carrier_ladder_has_a_rung_for_every_shape_of_feature(store):
    """`ops.feature_event`'s manager-only rule returns None for essentially every
    feature on the fleet — a manager exists only where a plan was released with
    validation on — so the general rule needs the three lower rungs, and each is
    asserted against a feature that has ONLY that rung."""
    bare = _feature(store, "never planned", status="pending")
    assert store.carrier_for_feature(bare) is None

    planned = _feature(store, "planned only", status="planning")
    planner = store.create_work_order("plan it", kind="planner",
                                      parent_id=planned)["id"]
    store.update_feature_order(planned, plan_wo_id=planner)
    assert store.carrier_for_feature(planned)["id"] == planner

    running = _feature(store, "children only")
    store.create_work_order("first child", parent_id=running)
    newest = store.create_work_order("second child", parent_id=running)["id"]
    assert store.carrier_for_feature(running)["id"] == newest, "newest, not oldest"

    managed = _feature(store, "fully planned")
    store.update_feature_order(
        managed,
        plan_wo_id=store.create_work_order("plan it", kind="planner",
                                           parent_id=managed)["id"])
    store.create_work_order("a child", parent_id=managed)
    manager = store.create_work_order("manage it", kind="manager",
                                      parent_id=managed)["id"]
    assert store.carrier_for_feature(managed)["id"] == manager, "manager outranks both"


def test_a_finding_whose_subject_and_fo_id_disagree_is_refused(store):
    """The pairing the schema cannot carry — no CHECK, because `_migrate` runs on every
    store open over live databases. Both directions, because enforcing one and not the
    other still lets a feature finding land with nothing to point at."""
    fo_id = _feature(store)
    carrier = store.create_work_order("the carrier", parent_id=fo_id)["id"]

    with pytest.raises(ValueError) as missing:
        store.add_finding(carrier, kind="stalled-plan", reason=FIRED,
                          subject_kind="feature_order")
    assert "subject_kind" in str(missing.value) and "fo_id" in str(missing.value)

    with pytest.raises(ValueError) as spurious:
        store.add_finding(carrier, kind="stalled-plan", reason=FIRED, fo_id=fo_id)
    assert "subject_kind" in str(spurious.value) and fo_id in str(spurious.value)

    # The positive partner: the pairing that agrees lands, so the two refusals above
    # are not simply a function that rejects everything.
    ok = store.add_finding(carrier, kind="stalled-plan", reason=FIRED,
                           subject_kind="feature_order", fo_id=fo_id)
    assert (ok["subject_kind"], ok["fo_id"], ok["seq"]) == ("feature_order", fo_id,
                                                            NO_TURN)


def test_a_feature_finding_is_claimable_and_the_claim_carries_its_subject(store):
    """`claim_next_alarm`'s WHERE is on `status` alone, so "it was claimable" grades
    nothing on its own — §4 reads the claimed dict, and every field it will branch on
    has to survive the RETURNING."""
    _, _, alarm_id = _diverged(store)

    claimed = store.claim_next_alarm()

    assert claimed is not None and claimed["id"] == alarm_id
    assert claimed["subject_kind"] == "feature_order"
    assert claimed["fo_id"] and claimed["source"] == "health"
    assert claimed["probe"] == "stalled-plan"
    assert claimed["seq"] == NO_TURN


def test_findings_about_one_feature_read_back_together_across_two_carriers(store):
    """`alarms_of` is by carrier and `alarms_for_feature` is by subject. A feature
    reached through two carriers has its record in two places, and only the second read
    puts it back together."""
    fo_id = _feature(store)
    first = store.create_work_order("first carrier", parent_id=fo_id)["id"]
    second = store.create_work_order("second carrier", parent_id=fo_id)["id"]
    for carrier in (first, second):
        store.add_finding(carrier, kind="stalled-plan", reason=FIRED, source="health",
                          probe="stalled-plan", subject_kind="feature_order",
                          fo_id=fo_id)

    assert len(store.alarms_of(first)) == 1
    assert len(store.alarms_for_feature(fo_id)) == 2


# -- the published dict --------------------------------------------------------------


def test_the_dict_gains_four_keys_and_the_subject_id_is_right_for_both_kinds(store):
    """`kn-4d8449f1` closed this dict at sixteen and four surfaces bind rather than
    validate it. Both subject kinds in ONE test: `subject_id` falling back to `wo_id`
    is the half that keeps every existing surface rendering, and asserting only the
    feature case would leave it ungraded."""
    fo_id, _, finding = _diverged(store)
    legacy = store.create_work_order("a burning turn")["id"]
    store.add_alarm(legacy, "long-turn", 1, "still being billed")
    store.close()

    rows = {r["id"]: r for r in ops.list_cost_alarms()}
    assert len(rows) == 2
    assert len(rows[finding]) == 20

    health = rows[finding]
    assert (health["source"], health["probe"]) == ("health", "stalled-plan")
    assert health["subject_kind"] == "feature_order"
    assert health["subject_id"] == fo_id

    cost = next(r for r in rows.values() if r["id"] != finding)
    assert (cost["source"], cost["probe"]) == ("cost", None)
    assert cost["subject_kind"] == "work_order"
    assert cost["subject_id"] == cost["wo_id"] == legacy


def test_the_title_and_status_are_the_subject_s_while_live_stays_the_carrier_s(store):
    """The one substitution that makes every template render a feature finding with no
    template change — and the one field that must NOT follow it. Acking clears the ask
    and must not erase the record, so `live` stays the carrier's attention flag, which
    is the only flag with an `acknowledged_blockers` column to make an ack stick."""
    _, carrier, finding = _diverged(store)
    store.close()

    before = next(r for r in ops.list_cost_alarms() if r["id"] == finding)
    assert before["title"] == "the feature"
    assert before["status"] == "executing", "the FEATURE's status, not the carrier's"
    assert before["wo_id"] == carrier, "and the carrier is still on the record"
    assert before["live"] is True

    ops.ack_attention(carrier, project_name="proj_a")

    after = next(r for r in ops.list_cost_alarms() if r["id"] == finding)
    assert after["live"] is False, "the ask is answered"
    assert after["title"] == "the feature", "and the record of what it was is not"
    assert after["status"] == "executing"


def test_the_alarm_detail_read_resolves_the_subject_with_no_fo_id_argument(store):
    """`ops._find_alarm` reaches `alarms_across(wo_id=…)` and never passes `fo_id`, so
    a join made conditional on that filter would leave this page — and the badge, and
    `jarvis alarms show` — showing the CARRIER's title. This is that read, verbatim."""
    _, _, finding = _diverged(store)
    store.close()

    detail = ops.alarm_detail(finding)

    assert detail["title"] == "the feature"
    assert detail["subject_kind"] == "feature_order"


def test_a_legacy_row_survives_three_store_opens_with_the_defaults_a_surface_binds(
        store, started):
    """The real risk is not the column count, it is a default landing somewhere a
    surface reads. `_migrate` runs on every open, so the row is read after three."""
    wo_id = store.create_work_order("a burning turn")["id"]
    store.add_alarm(wo_id, "long-turn", 1, "still being billed")
    path = store.project_path
    store.close()
    for _ in range(3):
        ProjectStore(path).close()

    row = ops.list_cost_alarms()[0]

    assert row["subject_kind"] == "work_order"
    assert row["source"] == "cost"
    assert row["subject_id"] == wo_id
    assert row["probe"] is None


# -- the reads -----------------------------------------------------------------------


def test_the_badge_and_the_page_count_subjects_and_not_carriers(store):
    """BOTH HALVES OR THE TEST GRADES NOTHING. "several subjects sharing one carrier
    count as several" is satisfied by deleting the `set()` and counting rows; only
    "findings on ONE feature through TWO carriers count as one" proves it is still a
    set, and only over subjects.

    THREE features on one carrier and ONE across two, deliberately lopsided: with the
    symmetric fixture (two and two) subjects and carriers both come to three and the
    wrong key is green. Here it is 4 subjects, 3 carriers, 5 rows — one number each."""
    from jarvis.ui.app import alarm_badge

    shared = store.create_work_order("one carrier, three features",
                                     status="running")["id"]
    for title in ("feature one", "feature two", "feature three"):
        fo_id = _feature(store, title)
        store.add_finding(shared, kind="stalled-plan", reason=FIRED, source="health",
                          probe="stalled-plan", subject_kind="feature_order",
                          fo_id=fo_id)
    store.flag_attention(shared, "three features are stalled")

    split = _feature(store, "one feature, two carriers")
    for name in ("carrier one", "carrier two"):
        carrier = store.create_work_order(name, parent_id=split, status="running")["id"]
        store.add_finding(carrier, kind="stalled-plan", reason=FIRED, source="health",
                          probe="stalled-plan", subject_kind="feature_order",
                          fo_id=split)
        store.flag_attention(carrier, f"{split}: {FIRED}")
    store.close()

    assert len(ops.list_cost_alarms()) == 5, "five findings, and the count is not five"
    assert alarm_badge() == 4, "three shared-carrier subjects, plus one split subject"

    page = _client().get("/alarms").text
    # The ack form is per group, so counting its actions counts the groups the page
    # decided on — the badge and the page could otherwise disagree silently.
    assert page.count('/ack"') == 4


def test_the_page_links_a_feature_finding_at_the_feature(store):
    """A reader who followed a link about a feature must land on the feature. The
    carrier is where the finding was FILED and is plumbing."""
    fo_id, carrier, finding = _diverged(store)
    store.close()

    page = _client().get(f"/alarms/proj_a/{finding}")

    assert page.status_code == 200
    block = page.text[page.text.index('class="panel"'):page.text.index("Your review")]
    assert f'href="/fo/proj_a/{fo_id}"' in block
    assert 'href="/wo/' not in block, "not the carrier"
    assert carrier not in block


def test_the_cli_filters_by_feature_and_by_source(store, capsys):
    """`jarvis alarms --fo` and `--source`. The CLI is the OS: §6 and a future PR body
    both need these reads, so they exist here rather than on the page alone."""
    from jarvis import cli

    fo_id, _, finding = _diverged(store)
    burning = store.create_work_order("a burning turn")["id"]
    store.add_alarm(burning, "long-turn", 1, "still being billed")
    store.close()

    assert cli.main(["alarms", "--fo", fo_id]) == 0
    only_feature = capsys.readouterr().out
    assert finding in only_feature and burning not in only_feature

    assert cli.main(["alarms", "--source", "health"]) == 0
    assert burning not in capsys.readouterr().out

    assert cli.main(["alarms", "--source", "cost"]) == 0
    only_cost = capsys.readouterr().out
    assert burning in only_cost and finding not in only_cost


def test_jarvis_alarms_show_says_no_turn_and_never_prints_turn_minus_one(store,
                                                                        capsys):
    """`seq` is NOT NULL and a subject-level finding stores the sentinel, so without a
    reading of its own the first health finding prints `turn -1` at the user. The cost
    alarm in the same test is what proves the turn number did not simply disappear."""
    from jarvis import cli

    _, _, finding = _diverged(store)
    burning = store.create_work_order("a burning turn")["id"]
    cost = store.add_alarm(burning, "long-turn", 1, "still being billed")["id"]
    store.close()

    assert cli.main(["alarms", "show", finding]) == 0
    health_out = capsys.readouterr().out
    assert "no turn" in health_out
    assert "turn -1" not in health_out

    assert cli.main(["alarms", "show", cost]) == 0
    assert "turn 1" in capsys.readouterr().out


# -- the vocabularies ----------------------------------------------------------------


def test_the_two_hand_maintained_event_vocabularies_agree():
    """`timeline` is a leaf and may not import a store, so the list is written down
    twice. A kind in only one of them gets no `_ref`, and every deep link on the page
    dies with no error anywhere."""
    from jarvis import timeline
    from jarvis.project_store import ALARM_EVENT_KINDS

    assert timeline.ALARM_KINDS == frozenset(ALARM_EVENT_KINDS)
    assert len(ALARM_EVENT_KINDS) == len(set(ALARM_EVENT_KINDS)), "no duplicates"


def test_the_values_later_sections_write_are_declared_here():
    """Declared now, including the ones nothing in this diff writes: two sections
    editing the same tuple is a conflict for no reason. `update_alarm` asserts against
    these tuples, so a missing value is a sibling's assertion failure, not a bad row."""
    from jarvis.project_store import (ALARM_SOURCES, ALARM_STATUSES, ALARM_SUBJECTS,
                                      ALARM_VERDICTS)

    assert "proposed" in ALARM_STATUSES
    assert "propose" in ALARM_VERDICTS
    assert ALARM_SUBJECTS == ("work_order", "feature_order")
    assert ALARM_SOURCES == ("cost", "health")
