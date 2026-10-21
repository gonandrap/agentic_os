"""`jarvis neo stats` headlines the assumption ROW; kn-18a6444b is its fixture (io-51469add)"""

from __future__ import annotations

import re

import pytest

from jarvis import autoreview, cli, db, ops
from jarvis.central_store import CentralStore
from jarvis.daemon import OBJECTION_ALARM_MIN_SENT, Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

# -- THE RECIPE, inlined verbatim from kn-18a6444b so the number cannot silently change meaning

ROW = re.compile(r"^  - #(\d+) (accepted|rejected|pending)\b")
CLAUSE = re.compile(r"^  - #\d+ (?:accepted|rejected|pending)(?: by (you|the OS|the os))?")
QREF = re.compile(r"assumption #(\d+) of (wo-[0-9a-f]{8})")


def recipe_buckets(lines_by_wo: dict[str, list[str]],
                   question_texts: list[str]) -> dict[str, dict[str, int]]:
    """kn-18a6444b's classifier over `jarvis wo show` lines, per bucket, settled and pending apart"""
    questions_by_row = {(m.group(2), int(m.group(1)))
                        for text in question_texts for m in QREF.finditer(text)}
    out = {"settled": {"neo_decided": 0, "neo_escalated": 0, "never_reached_neo": 0},
           "pending": {"neo_escalated": 0, "never_reached_neo": 0}}
    for wo_id, lines in lines_by_wo.items():
        for line in lines:
            row = ROW.match(line)
            if not row:
                continue
            n, verdict = int(row.group(1)), row.group(2)
            by = CLAUSE.match(line).group(1)
            who = ("pending" if verdict == "pending"
                   else "os" if by in ("the OS", "the os") else "user")
            bucket = ("neo_decided" if who == "os"
                      else "neo_escalated" if (wo_id, n) in questions_by_row
                      else "never_reached_neo")
            out["pending" if who == "pending" else "settled"][bucket] += 1
    return out


@pytest.fixture()
def fleet(jarvis_home, project):
    central = CentralStore()
    central.upsert_project("proj_a", str(project))
    yield central
    central.close()


WO_ID = "wo-0000aaaa"


def _ask(neo: NeoStore, store: ProjectStore, aid: int, n: int, *,
         answered_by: str = "neo", answer: str = "") -> int:
    """A Neo question carrying the recipe's only join key, linked to the row"""
    q = neo.ask("proj_a", WO_ID, f"ASSUMPTION REVIEW — rule on assumption #{n} of {WO_ID} "
                                  f"in proj_a", kind="assumption")
    neo.conn.execute("UPDATE questions SET status='answered', answered_by=?, answer=? "
                     "WHERE id=?", (answered_by, answer, q["id"]))
    store.link_assumption_question(aid, int(q["id"]))
    return int(q["id"])


@pytest.fixture()
def seeded(fleet, project):
    """EVERY case the recipe's three traps name, one row each"""
    store, neo = ProjectStore(project), NeoStore()
    try:
        store.create_work_order(wo_id=WO_ID, title="the order", description="")
        ids = [store.add_assumption(WO_ID, f"assumption {i}") for i in range(1, 9)]
        # Case one: Neo decided it
        _ask(neo, store, ids[0], 1)
        store.review_assumption(ids[0], "accepted", decided_by="neo", model="opus")
        # Trap one: the user accepted, with a provisional accept "by the OS" later in the line
        _ask(neo, store, ids[1], 2)
        store.record_provisional(ids[1], verdict="accept", reason="fine", model="opus")
        store.review_assumption(ids[1], "accepted", decided_by="user")
        # Trap one again: an objection "by the OS", rejected by the user
        _ask(neo, store, ids[2], 3)
        store.record_provisional(ids[2], verdict="object", reason="no", model="opus")
        store.review_assumption(ids[2], "rejected", decided_by="user")
        # Trap three: the OS closed the question as SUPERSEDED, the user accepted
        _ask(neo, store, ids[3], 4, answered_by="os",
             answer="SUPERSEDED — assumption 1014 is accepted")
        store.review_assumption(ids[3], "accepted", decided_by="user")
        # Trap two: held by the high-stakes net before any call, settled by the user
        store.add_event(WO_ID, "autoreview_held", {
            "code": autoreview.HELD_HIGH_STAKES, "assumption_id": ids[4], "n": 5,
            "reason": "assumption #5 mentions 'migration'"})
        store.review_assumption(ids[4], "accepted", decided_by="user")
        # Never reached, a legacy row with no decider at all ("you" on the record)
        store.review_assumption(ids[5], "accepted")
        # Unsettled: the seventh never reached Neo, the eighth is with it
        _ask(neo, store, ids[7], 8)
        # Aged together, so `n` (a position by `ts`) is the n each question was asked about
        store.conn.execute("UPDATE assumptions SET ts=ts-? WHERE wo_id=?",
                           (4 * 86400, WO_ID))
        lines = ["  - " + ops.assumption_line(a)
                 for a in ops.assumptions_with_rulings(store, WO_ID)]
        texts = [q["question"] for q in neo.list_questions()]
    finally:
        neo.close()
        store.close()
    return lines, texts


def test_the_row_rate_is_the_recipes_rate(seeded):
    """THE FIXTURE: kn-18a6444b's classifier over the rendered record agrees with the report"""
    lines, texts = seeded
    recipe = recipe_buckets({WO_ID: lines}, texts)

    a = ops.neo_stats_report(project="proj_a")["assumptions"]

    assert {k: a[k] for k in ops.ASSUMPTION_BUCKETS} == recipe["settled"]
    assert a["unsettled"]["reached_neo"] == recipe["pending"]["neo_escalated"]
    assert a["unsettled"]["never_reached_neo"] == recipe["pending"]["never_reached_neo"]
    # ...and the recipe's figures, pinned so neither can drift alone
    assert recipe["settled"] == {"neo_decided": 1, "neo_escalated": 3,
                                 "never_reached_neo": 2}
    assert a["rows"] == 8 and a["settled"] == 6
    assert a["rates"]["neo_decided"] == pytest.approx(1 / 6, abs=1e-4)
    assert a["never_reached_by_hold"] == {autoreview.HELD_HIGH_STAKES: 1,
                                          "no hold recorded": 1}
    assert a["unsettled"]["count"] == 2
    assert a["unsettled"]["max_age_days"] == pytest.approx(4.0, abs=0.1)


def test_a_whole_line_substring_test_would_have_got_it_wrong(seeded):
    """TRAP 1 is real in this fixture: 'by the OS' appears on rows the OS did NOT decide"""
    lines, _ = seeded
    substring_os = sum(1 for line in lines
                       if ROW.match(line) and "by the OS" in line
                       and not line.startswith("  - #8 "))
    assert substring_os > 1


def test_the_question_rate_is_labelled_plumbing(seeded):
    res = ops.neo_stats_report(project="proj_a")
    assert "PLUMBING" in res["questions_note"]


def test_the_terminal_leads_with_the_row_rate(seeded, capsys):
    cli._print_neo_stats(ops.neo_stats_report(project="proj_a"), as_json=False)
    out = capsys.readouterr().out
    assert out.index("ASSUMPTIONS") < out.index("QUESTIONS — plumbing")
    assert "never reached Neo" in out and "still unsettled" in out


def test_an_ask_cleared_by_a_dropped_ruling_still_counts_as_reached(fleet, project):
    """The link is cleared on a transient drop; the ask event is what remembers it"""
    store = ProjectStore(project)
    try:
        store.create_work_order(wo_id=WO_ID, title="t", description="")
        aid = store.add_assumption(WO_ID, "x")
        store.add_event(WO_ID, "autoreview_asked", {"assumption_id": aid, "n": 1,
                                                    "neo_question_id": 5})
        store.add_event(WO_ID, "autoreview_escalated", {
            "assumption_id": aid, "n": 1, "dropped": "status", "re_asked": True})
        store.review_assumption(aid, "accepted", decided_by="user")
    finally:
        store.close()
    a = ops.neo_stats_report(project="proj_a")["assumptions"]
    assert a["neo_escalated"] == 1 and a["never_reached_neo"] == 0
    assert a["dropped_rulings"] == {"total": 1, "re_asked": 1}


# -- objections: sent, delivered, ACTED ON, withdrawn (io-ce566253)


def _objection(store: ProjectStore, wo_id: str, envelope: int, *, delivered: bool = False,
               withdrawn: bool = False) -> int:
    aid = store.add_assumption(wo_id, f"objected {envelope}")
    store.record_objection(aid, envelope_id=envelope, transport="queue")
    if delivered:
        store.mark_objection_delivered(aid)
    if withdrawn:
        store.withdraw_objection(aid)
    return aid


def test_objections_count_what_the_worker_did_with_one(fleet, project):
    store = ProjectStore(project)
    try:
        store.create_work_order(wo_id=WO_ID, title="t", description="")
        _objection(store, WO_ID, 1, delivered=True)
        # The worker's next act after delivery: a new assumption is `objection_response`'s answer
        store.add_assumption(WO_ID, "changed course after the objection")
        _objection(store, WO_ID, 2, withdrawn=True)
    finally:
        store.close()

    o = ops.neo_stats_report(project="proj_a")["objections"]
    assert (o["sent"], o["delivered"], o["acted_on"], o["withdrawn"]) == (2, 1, 1, 1)
    assert o["withdrawal_rate"] == pytest.approx(0.5)


@pytest.fixture()
def daemon(jarvis_home, catalog_file, project, fleet):
    from jarvis.catalog import load_catalog

    return Daemon(load_catalog(catalog_file))


def _inbox(project: str) -> list[dict]:
    central = CentralStore()
    try:
        return [r for r in central.unacked_inbox() if r["project"] == project]
    finally:
        central.close()


def test_a_majority_withdrawn_reaches_the_inbox_once(daemon, project):
    spec = daemon.catalog.project("proj_a")
    store = ProjectStore(project)
    try:
        store.create_work_order(wo_id=WO_ID, title="t", description="")
        for i in range(OBJECTION_ALARM_MIN_SENT):
            _objection(store, WO_ID, 10 + i, withdrawn=True)

        daemon.check_objection_withdrawals(spec, store)
        daemon.check_objection_withdrawals(spec, store)
    finally:
        store.close()

    (item,) = [r for r in _inbox("proj_a") if "objections" in r["title"]]
    assert "withdrawn undelivered" in item["body"]


def test_below_the_threshold_or_the_sample_nothing_is_raised(daemon, project):
    spec = daemon.catalog.project("proj_a")
    store = ProjectStore(project)
    try:
        store.create_work_order(wo_id=WO_ID, title="t", description="")
        _objection(store, WO_ID, 1, withdrawn=True)
        _objection(store, WO_ID, 2, delivered=True)
        daemon.check_objection_withdrawals(spec, store)
        assert not [r for r in _inbox("proj_a") if "objections" in r["title"]]
        _objection(store, WO_ID, 3, withdrawn=True)
        # 2 of 3 — a majority, but the project turned the alarm off
        spec.validation.objection_withdrawal_alarm_rate = 1.0
        daemon.check_objection_withdrawals(spec, store)
        assert not [r for r in _inbox("proj_a") if "objections" in r["title"]]
        spec.validation.objection_withdrawal_alarm_rate = 0.5
        daemon.check_objection_withdrawals(spec, store)
    finally:
        store.close()
    assert [r for r in _inbox("proj_a") if "objections" in r["title"]]


def test_the_threshold_is_a_fleet_default_a_project_can_override():
    from jarvis.catalog import ValidationConfig, _parse_validation, CatalogError

    fleet_cfg = _parse_validation({"objection_withdrawal_alarm_rate": 0.7})
    assert fleet_cfg.objection_withdrawal_alarm_rate == 0.7
    assert _parse_validation({}, base=fleet_cfg).objection_withdrawal_alarm_rate == 0.7
    assert _parse_validation({"objection_withdrawal_alarm_rate": 0.2},
                             base=fleet_cfg).objection_withdrawal_alarm_rate == 0.2
    assert ValidationConfig().objection_withdrawal_alarm_rate == 0.5
    with pytest.raises(CatalogError):
        _parse_validation({"objection_withdrawal_alarm_rate": 2})
