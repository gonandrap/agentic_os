"""`jarvis neo stats` — the report itself, against seeded stores.

Spec: docs/specs/2026-10-01-neo-observability.md §4. Neo was the only agent in the OS with
no aggregate of any kind: `NeoStore.counts()` is a GROUP BY status over all time, built for
a nav badge, so "is Neo escalating more than it used to, and what for" had no answer in the
data.

TWO RULES CARRY MOST OF THIS FILE, and they pull in opposite directions:

* `questions` is a CENSUS — every question has a row, so a count of zero is MEASURED and
  prints `0`. Only the ratios can be absent.
* `escalation_rate`, `questions_per_wo`, `questions_per_fo` and `p50_ms` are `None` — never
  `0.0` — when their denominator or sample is empty, and the renderers print "not
  recorded". Zero settled questions and zero escalations are different answers.

No model and no daemon: the report is arithmetic over two databases.
"""

from __future__ import annotations

import pytest

from jarvis import db, ops
from jarvis.central_store import CentralStore
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore


@pytest.fixture()
def fleet(jarvis_home, project):
    """One registered project, so `per_order` has a denominator to find."""
    central = CentralStore()
    central.upsert_project("proj_a", str(project))
    yield central
    central.close()


def seed_question(neo: NeoStore, *, status: str = "escalated", kind: str = "question",
                  project: str = "proj_a", wo_id: str = "wo-1",
                  answered_by: str | None = None, cause: str = "",
                  age_days: float = 0.0) -> int:
    """One question in whatever state the test needs, aged by hand."""
    q = neo.ask(project, wo_id, f"question {db.now()} {wo_id} {status}", kind=kind)
    neo.conn.execute(
        "UPDATE questions SET status=?, answered_by=?, escalation_cause=?, ts=? "
        "WHERE id=?",
        (status, answered_by, cause or None, db.now() - age_days * 86400, q["id"]))
    return int(q["id"])


def seed_call(central: CentralStore, *, kind: str = "neo_answer",
              project: str = "proj_a", latency_ms: int | None = None,
              age_days: float = 0.0, output: int = 100) -> int:
    row = central.add_agent_call(
        kind, project=project, wo_id="wo-1", model="claude-sonnet-4-5",
        latency_ms=latency_ms,
        usage={"input": 10, "cache_write": 20, "cache_read": 30, "output": output,
               "total_cost_usd": 0.01})
    central.conn.execute("UPDATE agent_calls SET ts=? WHERE id=?",
                         (db.now() - age_days * 86400, row))
    return row


# -- 1. counts and the splits ---------------------------------------------------------

def test_the_counts_and_the_three_splits_over_a_known_fixture(fleet, project):
    neo = NeoStore()
    try:
        seed_question(neo, status="answered", answered_by="neo")
        seed_question(neo, status="answered", answered_by="neo", kind="assumption")
        seed_question(neo, status="escalated", cause="high-stakes")
        seed_question(neo, status="failed", cause="transport-unreachable")
        seed_question(neo, status="queued")
        # SUPERSEDED: decided somewhere else, so neither Neo answering nor Neo handing
        # back — broken out and out of the rate's denominator.
        seed_question(neo, status="answered", answered_by="os")
    finally:
        neo.close()

    res = ops.neo_stats_report()
    q = res["questions"]
    assert (q["asked"], q["answered"], q["escalated"], q["failed"]) == (6, 2, 1, 1)
    assert (q["open"], q["superseded"]) == (1, 1)
    # escalated ALONE / (answered + escalated + failed) — unreachable is never blended in
    assert q["escalation_rate"] == pytest.approx(1 / 4)
    assert q["unreachable_rate"] == pytest.approx(1 / 4)
    assert res["by_kind"]["assumption"]["answered"] == 1
    assert res["by_kind"]["question"]["asked"] == 5
    assert res["by_project"]["proj_a"]["asked"] == 6
    assert [d["asked"] for d in res["by_day"]] == [6]
    assert res["scope"] == "fleet"


def test_a_triage_question_is_out_of_per_order_and_still_in_the_counts(fleet, project):
    """`kind='triage'` has no work order behind it and its `wo_id` is empty: dividing it
    by an order count is arithmetic over two populations (spec §4)."""
    store = ProjectStore(project)
    try:
        store.create_work_order(wo_id="wo-real", title="the order", description="")
    finally:
        store.close()
    neo = NeoStore()
    try:
        seed_question(neo, status="escalated", wo_id="wo-real")
        seed_question(neo, status="escalated", kind="triage", wo_id="")
    finally:
        neo.close()

    res = ops.neo_stats_report()
    assert res["questions"]["asked"] == 2
    assert res["by_kind"]["triage"]["escalated"] == 1
    # one question against one work order, and the triage row in neither half
    assert res["per_order"] == {"work_orders": 1, "questions_per_wo": 1.0,
                                "feature_orders": 0, "questions_per_fo": None}


# -- 2. absent is never zero ----------------------------------------------------------

def test_an_empty_fleet_reports_measured_zeroes_and_absent_ratios(fleet):
    res = ops.neo_stats_report()
    q = res["questions"]
    # A question count of zero is a MEASURED zero: "0 escalated" is a fact.
    assert (q["asked"], q["answered"], q["escalated"], q["failed"]) == (0, 0, 0, 0)
    # The ratios are absent, and absent is None — never 0.0.
    assert q["escalation_rate"] is None
    assert q["unreachable_rate"] is None
    assert res["per_order"]["questions_per_wo"] is None
    assert res["per_order"]["questions_per_fo"] is None
    assert res["causes"] == {"chosen": {}, "overridden": {}, "failed": {},
                             "not_recorded": 0}
    assert res["latency"]["by_kind"] == {} and res["latency"]["unmeasured"] == 0


def test_open_questions_alone_leave_the_rate_absent(fleet):
    """Open ones have no outcome yet; including them would make the rate fall whenever
    the queue is busy."""
    neo = NeoStore()
    try:
        seed_question(neo, status="queued")
        seed_question(neo, status="answering")
    finally:
        neo.close()

    res = ops.neo_stats_report()
    assert res["questions"]["open"] == 2
    assert res["questions"]["escalation_rate"] is None
    assert res["questions"]["unreachable_rate"] is None


def test_a_window_of_only_unreachable_questions_reports_no_escalation(fleet):
    """Spec §4: a question Neo was never reached for is UNREACHABLE and never escalated."""
    neo = NeoStore()
    try:
        seed_question(neo, status="failed", cause="transport-unreachable")
        seed_question(neo, status="failed", cause="attempts-exhausted")
    finally:
        neo.close()

    res = ops.neo_stats_report()
    for bucket in (res["questions"], res["by_kind"]["question"],
                   res["by_project"]["proj_a"], res["by_day"][-1]):
        assert bucket["escalated"] == 0
        assert bucket["failed"] == 2
        assert bucket["escalation_rate"] == 0.0
        assert bucket["unreachable_rate"] == 1.0


def test_a_mixed_window_splits_the_two_rates(fleet):
    neo = NeoStore()
    try:
        seed_question(neo, status="answered", answered_by="neo")
        seed_question(neo, status="answered", answered_by="neo")
        seed_question(neo, status="escalated", cause="high-stakes")
        seed_question(neo, status="failed", cause="transport-unreachable")
    finally:
        neo.close()

    res = ops.neo_stats_report()
    for bucket in (res["questions"], res["by_day"][-1],
                   res["by_kind"]["question"], res["by_project"]["proj_a"]):
        assert bucket["escalation_rate"] == pytest.approx(0.25)
        assert bucket["unreachable_rate"] == pytest.approx(0.25)


# -- 3. the cause split ---------------------------------------------------------------

def test_the_four_cause_buckets_never_add_into_each_other(fleet):
    """THREE CLASSES, because "who decided" has three answers (Neo, question 1170): Neo
    handed it back, Neo answered and the OS overrode it, or Neo never answered."""
    neo = NeoStore()
    try:
        seed_question(neo, status="escalated", cause="high-stakes")
        seed_question(neo, status="escalated", cause="high-stakes")
        seed_question(neo, status="escalated", cause="stakes-unclassified")
        seed_question(neo, status="escalated", cause="neo-denied")
        seed_question(neo, status="failed", cause="attempts-exhausted")
        seed_question(neo, status="escalated")            # predates cause recording
        # and an ANSWERED question is in no bucket at all
        seed_question(neo, status="answered", answered_by="neo")
    finally:
        neo.close()

    causes = ops.neo_stats_report()["causes"]
    assert causes["chosen"] == {"high-stakes": 2}
    assert causes["overridden"] == {"stakes-unclassified": 1, "neo-denied": 1}
    assert causes["failed"] == {"attempts-exhausted": 1}
    assert causes["not_recorded"] == 1
    assert sum(causes["chosen"].values()) + sum(causes["overridden"].values()) \
        + sum(causes["failed"].values()) + causes["not_recorded"] == 6


def test_a_member_a_release_dropped_lands_in_not_recorded(fleet):
    """The two classifier members `stakes.HIGH_UNREACHABLE` / `HIGH_UNPARSEABLE` could
    never be written and were dropped from the enum: a row carrying one is not a cause."""
    neo = NeoStore()
    try:
        qid = seed_question(neo, status="failed")
        neo.conn.execute("UPDATE questions SET escalation_cause='classifier-unreachable' "
                         "WHERE id=?", (qid,))
    finally:
        neo.close()

    causes = ops.neo_stats_report()["causes"]
    assert causes["chosen"] == causes["overridden"] == causes["failed"] == {}
    assert causes["not_recorded"] == 1


def test_the_report_names_the_class_the_counts_cannot_show(fleet):
    """Neo's ruling on 1170: a class nothing can ever write is better documented as
    invisible than left in the enum — so the report has to SAY what is not in it."""
    res = ops.neo_stats_report()
    note = res["causes_note"]
    assert note == ops.NEO_ESCALATION_INVISIBLE_NOTE
    assert "never became a Neo question" in note
    assert "not in these counts" in note


def test_a_cause_in_neither_tuple_is_counted_as_not_recorded(fleet):
    """A release that removed a member must not invent a ninth bucket (spec §4)."""
    neo = NeoStore()
    try:
        qid = seed_question(neo, status="escalated")
        neo.conn.execute("UPDATE questions SET escalation_cause='retired-member' "
                         "WHERE id=?", (qid,))
    finally:
        neo.close()

    causes = ops.neo_stats_report()["causes"]
    assert causes["chosen"] == {} and causes["failed"] == {}
    assert causes["not_recorded"] == 1


# -- 4. the window --------------------------------------------------------------------

def test_days_excludes_older_rows_from_every_section_including_spend(fleet):
    neo = NeoStore()
    try:
        seed_question(neo, status="escalated", cause="high-stakes", age_days=0.1)
        seed_question(neo, status="escalated", cause="user-decision", age_days=30)
    finally:
        neo.close()
    seed_call(fleet, latency_ms=1000, age_days=0.1)
    seed_call(fleet, latency_ms=9000, age_days=30)

    windowed = ops.neo_stats_report(days=7)
    assert windowed["questions"]["asked"] == 1
    assert windowed["causes"]["chosen"] == {"high-stakes": 1}
    assert windowed["spend"]["totals"]["calls"] == 1
    assert windowed["latency"]["by_kind"]["neo_answer"]["max_ms"] == 1000
    assert windowed["since"] is not None and windowed["days"] == 7

    everything = ops.neo_stats_report()
    assert everything["questions"]["asked"] == 2
    assert everything["spend"]["totals"]["calls"] == 2
    assert everything["since"] is None


# -- 5. latency -----------------------------------------------------------------------

def test_a_window_nobody_timed_reports_no_percentile_and_counts_the_blind_rows(fleet):
    seed_call(fleet, latency_ms=None)
    seed_call(fleet, latency_ms=None)

    lat = ops.neo_stats_report()["latency"]
    assert lat["by_kind"]["neo_answer"]["calls"] == 2
    assert lat["by_kind"]["neo_answer"]["measured"] == 0
    assert lat["by_kind"]["neo_answer"]["p50_ms"] is None
    assert lat["by_kind"]["neo_answer"]["p90_ms"] is None
    assert lat["by_kind"]["neo_answer"]["max_ms"] is None
    assert lat["unmeasured"] == 2


def test_measured_latencies_give_percentiles_beside_the_unmeasured_count(fleet):
    for ms in (100, 200, 300, 400, 5000):
        seed_call(fleet, latency_ms=ms)
    seed_call(fleet, latency_ms=None)

    lat = ops.neo_stats_report()["latency"]
    entry = lat["by_kind"]["neo_answer"]
    assert (entry["calls"], entry["measured"]) == (6, 5)
    assert entry["p50_ms"] == 300 and entry["max_ms"] == 5000
    assert entry["p90_ms"] >= entry["p50_ms"]
    assert lat["unmeasured"] == 1


# -- scope, pricing and the floor -----------------------------------------------------

def test_only_neo_kinds_reach_the_spend_half(fleet):
    """`agent_usage.NEO_KINDS`: the worker's own subprocesses and the user's looking are
    other classes by that module's own definitions."""
    seed_call(fleet, kind="neo_answer", latency_ms=10)
    seed_call(fleet, kind="worker_subprocess", latency_ms=10)
    seed_call(fleet, kind="observe_inspect", latency_ms=10)

    spend = ops.neo_stats_report()["spend"]
    assert set(spend["by_kind"]) == {"neo_answer"}
    assert spend["totals"]["calls"] == 1
    assert spend["totals"]["recorded_cost_usd"] == pytest.approx(0.01)
    # Two currencies, never blended: the CLI's own figure and the same tokens at list
    # prices (`ops._os_spend`'s rule).
    assert spend["totals"]["list_cost_usd"] > 0
    assert spend["totals"]["output"] == 100


def test_an_unregistered_project_is_refused(fleet):
    with pytest.raises(ops.OpsError):
        ops.neo_stats_report(project="nope")


def test_the_report_says_it_is_a_floor(fleet):
    res = ops.neo_stats_report()
    assert res["floor"] is True and res["floor_reason"] == ops.COST_FLOOR_NOTE


# -- `jarvis neo stats` ---------------------------------------------------------------
#
# The CLI renders the SAME dict the dashboard does, so these assert the rendering and
# nothing about the arithmetic.

def test_the_json_form_prints_the_ops_dict_unchanged(fleet, capsys):
    import json

    from jarvis import cli

    neo = NeoStore()
    try:
        seed_question(neo, status="escalated", cause="high-stakes")
    finally:
        neo.close()
    capsys.readouterr()
    assert cli.main(["neo", "stats", "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(json.dumps(ops.neo_stats_report()))


def test_the_human_rendering_of_an_empty_fleet_prints_no_measured_zero_rate(fleet,
                                                                           capsys):
    from jarvis import cli

    capsys.readouterr()
    assert cli.main(["neo", "stats"]) == 0

    out = capsys.readouterr().out
    assert "0%" not in out, "an absent rate must never render as a measured zero"
    assert "escalation rate   not recorded" in out
    # and the counts, which ARE measured zeroes, print as 0
    assert "escalated         0" in out


def test_the_rendering_reads_the_causes_as_three_answers_to_who_decided(fleet, capsys):
    """A flat list of labels is not the deliverable: each class carries the sentence that
    says what it MEANS for who decided, and the invisible class is named."""
    from jarvis import cli

    neo = NeoStore()
    try:
        seed_question(neo, status="escalated", cause="high-stakes")
        seed_question(neo, status="escalated", cause="stakes-unclassified")
        seed_question(neo, status="failed", cause="transport-unreachable")
    finally:
        neo.close()
    capsys.readouterr()
    assert cli.main(["neo", "stats"]) == 0

    out = capsys.readouterr().out
    assert "Neo chose to hand it back" in out
    assert "Neo answered and the OS overrode it" in out
    assert "Neo never answered" in out
    for member in ("high-stakes", "stakes-unclassified", "transport-unreachable"):
        assert member in out
    assert ops.NEO_ESCALATION_INVISIBLE_NOTE in out


def _trend_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines()
            if "asked" in ln and "escalated" in ln and "rate" in ln]


def test_the_trend_row_prints_the_unreachable_count_and_rate(fleet, capsys):
    """Spec §4: a bar may never contradict the counts beside it."""
    from jarvis import cli

    neo = NeoStore()
    try:
        seed_question(neo, status="failed", cause="transport-unreachable")
    finally:
        neo.close()
    capsys.readouterr()
    assert cli.main(["neo", "stats"]) == 0

    out = capsys.readouterr().out
    rows = _trend_lines(out)
    assert rows, out
    for line in rows:
        assert "never reached" in line, line
        assert "unreachable" in line, line
    assert "escalation rate   0%" in out and "unreachable rate  100%" in out


def test_an_absent_trend_rate_prints_not_recorded(fleet, capsys):
    from jarvis import cli

    neo = NeoStore()
    try:
        seed_question(neo, status="queued")
    finally:
        neo.close()
    capsys.readouterr()
    assert cli.main(["neo", "stats"]) == 0

    out = capsys.readouterr().out
    assert "0%" not in out, "an absent rate must never render as a measured zero"
    rows = _trend_lines(out)
    assert rows, out
    assert rows[-1].count("not recorded") == 2, rows[-1]
