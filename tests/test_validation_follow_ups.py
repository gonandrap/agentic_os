"""A non-blocking finding becomes a backlog item, and the work order says so.

Section 4 of docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md.

THE PAIRING THIS FILE RUNS ON. "It was filed" and "the submitter was not told" are the
two halves of the design and each is satisfied by the wrong code alone: a machine that
files nothing passes every silence assertion, and a machine that pushes every nit at the
worker passes every filing assertion. So the filing tests read the backlog AND the
message the worker received, in the same test, off the same round.

The validator is INJECTED, as it is everywhere else in this lineage: what the seats
classify is §3's measurement and the eval's, and a test that reached this code through a
real panel would be grading a model rather than the filing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from jarvis import cli, ops, timeline
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from tests.test_validation_loop import (  # noqa: F401  (the fixture and its helpers)
    Fleet, Validator, fleet, finish, write_catalog)


# -- the panel, driven by the test ----------------------------------------------------


def _seats(verdict: str) -> list[dict]:
    return [{"seat": seat, "status": "ok", "verdict": verdict, "model": "sonnet",
             "latency_ms": 12, "reply": f"the {seat} seat says {verdict}"}
            for seat in ("tester", "security")]


def follow_up(title: str, detail: str = "", seat: str = "maintainer") -> dict:
    """One entry of `decide`'s `follow_ups` key, as §3 returns it."""
    return {"seat": seat, "title": title, "detail": detail or f"about {title}",
            "round": 1}


def verdict(outcome: str, *titles: str, reason: str = "", seat: str = "maintainer",
            **kw) -> dict:
    return {"outcome": outcome, "reason": reason, "seats": _seats(
        "pass" if outcome == "passed" else "reject"),
        "follow_ups": [follow_up(t, seat=seat) for t in titles], **kw}


def backlog(project: str = "proj_a") -> list[dict]:
    """Every item on the project's backlog, WHATEVER ITS STATUS — the reading the dedupe
    itself has to make, so a test using `list_backlog`'s `open` default would go blind at
    exactly the moment the dedupe does."""
    central = CentralStore()
    try:
        return [r for r in central.list_backlog(project, status=None)]
    finally:
        central.close()


def sent(fleet, wo_id: str) -> str:
    """Everything the submitter has been told, concatenated. `user_to_agent` only: the
    worker's own turns land in the same table in the other direction."""
    store = fleet.store()
    try:
        return "\n".join(r["content"] for r in store.conn.execute(
            "SELECT content FROM wo_messages WHERE wo_id=? "
            "AND direction='user_to_agent'",
            (wo_id,)).fetchall())
    finally:
        store.close()


def judged(fleet, outcome: str, *titles: str, **kw) -> dict:
    """One work order through one real round with these follow-ups. Returns it."""
    fleet.daemon.validator = Validator(verdict(outcome, *titles, **kw))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    return wo


# -- 1. the filing --------------------------------------------------------------------


def test_a_rejected_round_files_its_follow_ups_and_still_sends_back_its_blockers(fleet):
    """The whole point, in one test: the round's non-blocking remarks become tickets and
    the submitter is sent back over its blocker alone.

    Both halves, because either is satisfied by the wrong machine. A panel that filed
    nothing would pass "the worker was told only the reason"; a panel that pushed every
    nit would pass "a backlog item exists"."""
    wo = judged(fleet, "rejected", "Name the retry budget in the docstring",
                reason="the new branch has no test")
    fleet.tick()  # deliver the feedback

    items = backlog()
    assert [i["title"] for i in items] == ["Name the retry budget in the docstring"]
    assert items[0]["origin_wo_id"] == wo["id"] and items[0]["origin_fo_id"] is None
    assert items[0]["status"] == "open"
    told = sent(fleet, wo["id"])
    assert "the new branch has no test" in told
    assert "retry budget" not in told, "a follow-up reached the submitter"


def test_a_passed_round_files_its_follow_ups_too(fleet):
    """The case that makes this a feature rather than a nicer rejection. A finding the
    seats judged the work shippable WITHOUT is exactly the finding that used to be lost —
    conditioning the filing on an outcome the seat could not see when it wrote would
    rebuild the treadmill in miniature."""
    wo = judged(fleet, "passed", "Fold the two parsers into one")

    assert [i["title"] for i in backlog()] == ["Fold the two parsers into one"]
    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    finally:
        store.close()


def test_the_backlog_row_names_the_seat_and_the_round(fleet):
    """THE ONE DOOR DELIBERATION LEAVES BY, and it is deliberate (§4.2). The backlog is
    the project's own record, read later by a person deciding whether to act, and which
    reviewer said this is exactly what they need — while the same fact may never reach
    the submitter, which the test above pins."""
    wo = judged(fleet, "passed", "Fold the two parsers into one", seat="architect")

    item = backlog()[0]
    assert item["origin_note"] == "validation follow-up · architect seat · round 1"
    # The provenance footer: the only context whoever picks this up will have.
    assert "round 1" in item["description"] and wo["id"] in item["description"]
    assert "github.com" in item["description"], "the pull request is not on the ticket"
    assert "about Fold the two parsers into one" in item["description"]


def test_a_verdict_with_no_follow_ups_key_at_all_files_nothing_and_raises_nothing(fleet):
    """THE OLD SHAPE. Every row already in `validation_opinions` is it, a model will
    sometimes answer in it anyway, and `Daemon.validator` is injectable — several suites
    inject fakes returning only the three keys that predate this."""
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass")})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert backlog() == []
    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
        assert store.events_of_kind(wo["id"], ops.FOLLOW_UPS_EVENT) == []
    finally:
        store.close()


def test_an_empty_title_is_not_a_ticket(fleet):
    """A finding with nothing to call it cannot be a backlog item anybody can act on,
    and an untitled row is worse than no row."""
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass"),
         "follow_ups": [{"seat": "tester", "title": "   ", "detail": "something"}]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert backlog() == []


# -- 2. the dedupe ---------------------------------------------------------------------


def _round_two(fleet, first: dict, second: dict, **cfg) -> dict:
    """Two real rounds on one work order, with different evidence each time.

    `**cfg` goes in WITH `max_rounds` rather than in a second call: `reconfigure`
    rewrites the whole catalog, so a later call silently drops an earlier one's field.
    """
    fleet.reconfigure(max_rounds=9, **cfg)
    validator = Validator(first, second)
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()  # deliver the feedback; the work order is running again
    fleet.change(wo["id"], "print('one')\nprint('two')\n")
    finish(fleet, wo["id"], summary="fixed")
    fleet.drain()
    assert [c["round"] for c in validator.calls] == [1, 2], "round 2 never ran"
    return wo


def test_round_two_re_raising_round_ones_nit_files_it_once(fleet):
    """Mechanism 1.2: a fresh reviewer re-reads the whole artifact and says the same
    thing again. Without this the feature converts a rejection treadmill into a backlog
    flood."""
    wo = _round_two(fleet,
                    verdict("rejected", "Name the retry budget", reason="no test"),
                    verdict("passed", "Name the retry budget"))

    assert [i["title"] for i in backlog()] == ["Name the retry budget"]


def test_the_title_is_matched_after_whitespace_is_collapsed(fleet):
    """The house normalisation — strip, collapse runs of whitespace — so a seat that
    wraps its sentence differently in round 2 does not file it twice."""
    wo = _round_two(fleet,
                    verdict("rejected", "Name the retry budget", reason="no test"),
                    verdict("passed", "  Name   the\n  retry budget  "))

    assert [i["title"] for i in backlog()] == ["Name the retry budget"]


def test_an_item_the_user_has_since_closed_is_still_not_re_filed(fleet):
    """THE TRAP THAT IS INVISIBLE UNTIL PRODUCTION. `list_backlog` defaults to
    `status='open'`; a dedupe built on that default stops seeing an item the user closed,
    dropped or promoted, and re-files it on every subsequent round for ever.

    Paired with the round-2 test above, which proves the dedupe works at all — this one
    alone would pass on a machine that never files anything twice because it never files
    anything."""
    fleet.reconfigure(max_rounds=9)
    validator = Validator(verdict("rejected", "Name the retry budget", reason="no test"),
                          verdict("passed", "Name the retry budget"))
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    filed = backlog()
    assert len(filed) == 1
    central = CentralStore()
    try:  # the user reads it and drops it, which is what a backlog is for
        central.mark_backlog(filed[0]["id"], "dropped")
    finally:
        central.close()

    fleet.tick()
    fleet.change(wo["id"], "print('one')\nprint('two')\n")
    finish(fleet, wo["id"], summary="fixed")
    fleet.drain()

    assert [i["status"] for i in backlog()] == ["dropped"], (
        "a closed follow-up was filed again")


def test_the_same_title_twice_in_one_round_is_one_ticket(fleet):
    """Two seats can reach the same nit in the same round, and the dedupe against the
    stored backlog cannot see a row this round has not written yet."""
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass"),
         "follow_ups": [follow_up("Name the retry budget", seat="tester"),
                        follow_up("Name the retry budget", seat="architect")]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert [i["title"] for i in backlog()] == ["Name the retry budget"]


def test_a_deferral_the_worker_filed_is_not_filed_again_by_the_panel(fleet):
    """"Has this already been filed" is a question about THE BACKLOG, not about what the
    panel filed — so a row the worker deferred under the same title counts. The same
    ticket twice is the same ticket twice whoever wrote it."""
    wo = fleet.dispatch()
    central = CentralStore()
    try:
        central.add_backlog("proj_a", "Name the retry budget",
                            origin_wo_id=wo["id"], origin_note="deferred by the worker")
    finally:
        central.close()

    fleet.daemon.validator = Validator(verdict("passed", "Name the retry budget"))
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert [i["origin_note"] for i in backlog()] == ["deferred by the worker"]


# -- 3. the cap ------------------------------------------------------------------------


SIX = ("one", "two", "three", "four", "five", "six")


def test_only_five_follow_ups_are_filed_per_round_and_the_rest_are_counted(fleet):
    """Title matching will never be reliable — a seat writes a slightly different
    sentence each round — so the damage is BOUNDED rather than chased. The failure mode
    is a few near-duplicate rows a user drops, not a backlog nobody can read."""
    wo = judged(fleet, "passed", *SIX)

    assert [i["title"] for i in backlog()] == list(SIX[:5])
    store = fleet.store()
    try:
        payload = store.events_of_kind(wo["id"], ops.FOLLOW_UPS_EVENT)[-1]
    finally:
        store.close()
    assert json.loads(payload["payload"])["dropped"] == 1


def test_the_cap_is_a_per_project_setting(fleet):
    """Every threshold a surface judges by is a catalog field with this block's ordinary
    field-level inheritance, never a module constant — and a project that names one keeps
    the fleet answer for every other field."""
    fleet.reconfigure(max_follow_ups=9, project_validation={"max_follow_ups": 2})
    wo = judged(fleet, "passed", *SIX)

    assert [i["title"] for i in backlog()] == ["one", "two"]


def test_a_dropped_finding_is_filed_by_a_later_round(fleet):
    """Over the cap is not the same as refused. Deduping BEFORE capping is what makes
    that true: the other order spends the whole cap on duplicates and never reaches the
    finding behind them, on every round, for ever."""
    wo = _round_two(fleet,
                    verdict("rejected", "one", "two", "three", reason="no test"),
                    verdict("passed", "one", "two", "three"), max_follow_ups=2)

    assert [i["title"] for i in backlog()] == ["one", "two", "three"]


# -- 4. the switch ---------------------------------------------------------------------


def test_with_filing_off_the_finding_is_discarded_and_still_does_not_reject(fleet):
    """`follow_ups: false` does NOT restore the old behaviour: §3 made the rejection
    change unconditional and this knob does not reach it. So `False` only chooses to
    throw the record away, which is why it ships True."""
    fleet.reconfigure(follow_ups=False)
    wo = judged(fleet, "passed", "Name the retry budget")

    assert backlog() == []
    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
        assert store.events_of_kind(wo["id"], ops.FOLLOW_UPS_EVENT) == []
    finally:
        store.close()


def test_it_ships_on(fleet):
    """The ruled carve-out to `ValidationConfig`'s ships-disabled rule (Neo, question
    309): this knob gates no behaviour change, only whether the record survives, and
    discarding is the one outcome nobody wants. Asserted on a catalog that names neither
    the field nor a project override, which is every fleet's."""
    from jarvis.catalog import ValidationConfig

    assert ValidationConfig().follow_ups is True
    assert fleet.spec.validation.follow_ups is True


# -- 5. the timeline -------------------------------------------------------------------


def test_the_filing_is_readable_on_the_timeline(fleet):
    """`timeline.event_level` returns `signal` for a kind it does not know, so an
    unregistered kind RENDERS — as the bare kind beside a JSON blob — and looks fine
    (`kn-3f133363`). One branch covers `jarvis wo show` and the dashboard, because both
    call `build_timeline`."""
    wo = judged(fleet, "passed", "Name the retry budget")

    store = fleet.store()
    try:
        rows = timeline.build_timeline(store.get_work_order(wo["id"]),
                                       store.list_events(wo["id"]), [])
    finally:
        store.close()
    line = next(e for e in rows if e["kind"] == ops.FOLLOW_UPS_EVENT)
    assert line["label"] == "Review filed 1 follow-up on the backlog"
    assert line["detail"].startswith("bl-")
    # NO SEAT NAME: the payload carries them and this surface is read by the submitter.
    assert "maintainer" not in line["label"] + line["detail"]


def test_the_timeline_is_not_folded_away_as_debug(fleet):
    """`DEBUG_KINDS` is what `jarvis wo show` hides by default. A filing is an outcome of
    the round, not plumbing."""
    assert ops.FOLLOW_UPS_EVENT not in timeline.DEBUG_KINDS


# -- 6. the surfaces -------------------------------------------------------------------


def test_the_named_key_list_carries_follow_ups_on_every_round(fleet):
    """`ops.validation_rounds` projects a NAMED KEY LIST — a key not in it is a key
    `jarvis wo show`, `jarvis fo show` and both dashboard pages can never see. ALWAYS
    PRESENT, even empty, which is the rule `assumptions` and `alarms` already follow: a
    key that comes and goes is one every consumer has to guard."""
    wo = _round_two(fleet,
                    verdict("rejected", reason="no test"),          # files nothing
                    verdict("passed", "Name the retry budget"))

    store = fleet.store()
    try:
        rounds = ops.validation_rounds(store, wo_id=wo["id"])
    finally:
        store.close()
    assert [r["follow_ups"]["filed"] for r in rounds][0] == []
    assert [i["title"] for i in rounds[1]["follow_ups"]["filed"]] == \
        ["Name the retry budget"]
    assert rounds[1]["follow_ups"]["filed"][0]["seat"] == "maintainer"
    assert all(r["follow_ups"]["dropped"] == 0 for r in rounds)


def test_round_line_counts_them_and_stays_silent_at_zero(fleet):
    """The one-line formatter both `show` commands share, so a count belongs there or
    nowhere. Silent at zero, which is every round on a fleet that has not turned the
    panel on — and it names no seat, because this is a surface the submitter reads."""
    wo = _round_two(fleet,
                    verdict("rejected", reason="no test"),
                    verdict("passed", "Name the retry budget"))

    store = fleet.store()
    try:
        lines = [ops.round_line(r)
                 for r in ops.validation_rounds(store, wo_id=wo["id"])]
    finally:
        store.close()
    assert "follow-up" not in lines[0]
    assert "1 follow-up filed" in lines[1]
    assert "maintainer" not in lines[1]


def test_round_line_says_how_many_went_over_the_cap(fleet):
    fleet.reconfigure(max_follow_ups=2)
    wo = judged(fleet, "passed", *SIX)

    store = fleet.store()
    try:
        line = ops.round_line(ops.validation_rounds(store, wo_id=wo["id"])[-1])
    finally:
        store.close()
    assert "2 follow-ups filed" in line and "4 over the cap" in line


def test_wo_show_prints_the_count(fleet, capsys):
    """`jarvis wo show`'s human view goes through a THIRD reader — `cli._readable_rounds`
    — which collapses each round to `ops.round_line` and drops the key when there are no
    rounds at all. Fetching it through the command is what proves the chain."""
    wo = judged(fleet, "passed", "Name the retry budget")
    capsys.readouterr()

    cli.main(["wo", "show", wo["id"]])

    out = capsys.readouterr().out
    assert "1 follow-up filed" in out
    assert "maintainer" not in out, "a seat name reached a default surface"


def test_validation_show_classifies_each_finding_and_names_the_ticket(fleet, capsys):
    """§4.7.3: making "why was this rejected" answerable without reading five JSON blobs.

    This is the DELIBERATION surface, so it is also the one place besides the backlog row
    where a seat's name may sit beside what it said."""
    fleet.daemon.validator = Validator({
        "outcome": "rejected", "reason": "the new branch has no test",
        "seats": [{"seat": "tester", "status": "ok", "verdict": "reject",
                   "model": "sonnet", "latency_ms": 12,
                   "reply": '{"verdict": "reject", "reason": "no test", "asks": [],'
                            ' "findings": ['
                            '{"severity": "blocker", "title": "No test covers retry",'
                            ' "detail": "add one"},'
                            '{"severity": "follow_up", "title": "Name the retry budget",'
                            ' "detail": "in the docstring"}]}'}],
        "follow_ups": [follow_up("Name the retry budget", seat="tester")]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    item = backlog()[0]
    capsys.readouterr()

    cli.main(["validation", "show", wo["id"]])

    out = capsys.readouterr().out
    assert "blocked: No test covers retry" in out
    assert f"follow-up {item['id']}: Name the retry budget" in out
    assert "tester" in out, "the deliberation surface withheld the seat"


def test_the_dashboard_work_order_page_shows_the_ticket_and_leaves_the_other_round_bare(
        fleet):
    """THE TEMPLATE IS A SURFACE YOU GO AND COUNT (`kn-99e37a4b`). The page does NOT read
    `ops.validation_rounds` — it is fed `ops.validation_detail`, a different projection —
    and `{% if filed.filed %}` is SILENTLY FALSY: drop the key from the projection the
    PAGE reads and the whole section disappears with no error and a green suite.

    Both rounds are rendered, because "the round that filed one says so" and "the round
    that filed none is bare" are different claims, and a template that printed the
    section on every round satisfies the first.
    """
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    wo = _round_two(fleet,
                    verdict("rejected", reason="no test"),
                    verdict("passed", "Name the retry budget"))
    item = backlog()[0]

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text

    assert page.count("Filed as follow-ups:") == 1, "one round filed, one did not"
    assert item["id"] in page
    assert "Name the retry budget" in page
    # ...and the raw event kind never reaches a reader.
    assert ops.FOLLOW_UPS_EVENT not in page


def test_the_dashboard_section_bites_when_the_projection_loses_the_key(fleet,
                                                                      monkeypatch):
    """The mutation that proves the assertion above is load-bearing. `validation_detail`
    is the projection THAT page reads; mutating `validation_rounds` instead would leave
    this test green, which is how you learn they were never one surface."""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    wo = judged(fleet, "passed", "Name the retry budget")
    real = ops.validation_detail
    monkeypatch.setattr(ops, "validation_detail", lambda *a, **k: {
        **real(*a, **k),
        "rounds": [{**r, "follow_ups": None} for r in real(*a, **k)["rounds"]]})

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text

    assert "Filed as follow-ups:" not in page


# -- 7. the feature order --------------------------------------------------------------


def test_both_validation_loops_file_through_the_one_function(fleet):
    """`_round_config`'s docstring records Neo's ruling (question 176) that the two loops
    are held identical. This is a STRUCTURAL claim about the daemon, not a restatement of
    one function in terms of itself: it asserts that each loop reaches the shared filer,
    and that it does so BEFORE the outcome branch — a follow-up is filed whether the
    round passed or was rejected."""
    src = Path(Daemon._validate_work_order.__code__.co_filename).read_text()
    tree = ast.parse(src)
    for name in ("_validate_work_order", "_validate_feature"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        calls = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_file_follow_ups"]
        assert len(calls) == 1, f"{name} does not file follow-ups exactly once"
        branch = next(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
                      and any(getattr(t, "id", "") == "outcome" for t in n.targets))
        assert calls[0] < branch, f"{name} files inside the outcome branch"


def test_a_feature_orders_follow_up_is_filed_against_the_feature(fleet):
    """The other unit. `origin_fo_id` rather than `origin_wo_id`, and the event goes to
    the manager's timeline through `ops.feature_event` — a feature order has no timeline
    of its own."""
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "the exporter", description=ASK)
    fleet.daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("alpha")))
    ops.review_plan(fo["id"], accept=True, decided_by="user")
    store = fleet.store()
    try:
        manager = store.manager_work_order(fo["id"])
        assert manager is not None
        rnd = store.open_validation_round(fo_id=fo["id"], fingerprint="ffff",
                                          summary="built it", evidence="ran pytest")
        filed = ops.file_validation_follow_ups(
            store, "proj_a", dict(rnd), [follow_up("Name the retry budget")],
            fleet.spec.validation, fo_id=fo["id"])
        assert len(filed["ids"]) == 1
        events = ops.feature_events_of_kind(store, fo["id"], ops.FOLLOW_UPS_EVENT)
        assert len(events) == 1 and events[0]["wo_id"] == manager["id"]
        rounds = ops.validation_rounds(store, fo_id=fo["id"])
        assert [i["title"] for i in rounds[0]["follow_ups"]["filed"]] == \
            ["Name the retry budget"]
    finally:
        store.close()

    item = backlog()[0]
    assert item["origin_fo_id"] == fo["id"] and item["origin_wo_id"] is None


# -- 8. what must not widen -------------------------------------------------------------


def test_a_filed_follow_up_never_becomes_the_submitters_side_effect(fleet):
    """`ops.side_effects_of` tells the panel what durable work the SUBMITTER did outside
    the repository. A backlog row the PANEL filed is the panel's own output: a later
    round that saw it there would judge its own remarks as the submitter's deliverable —
    and `side_effects_sha` is in `evidence.fingerprint`, so it would break the repeat
    guard too."""
    wo = judged(fleet, "passed", "Name the retry budget")

    assert backlog(), "the fixture filed nothing, so the claim is vacuous"
    assert ops.side_effects_of(wo["id"]) == []


def test_the_filer_cannot_take_a_round_down(fleet, monkeypatch):
    """The verdict has been paid for and every seat is already recorded by the time this
    runs, so a `CentralStore` that will not open must cost the follow-ups and nothing
    else."""
    def boom(*a, **k):
        raise RuntimeError("os.db is locked")

    monkeypatch.setattr(ops, "file_validation_follow_ups", boom)
    wo = judged(fleet, "rejected", "Name the retry budget", reason="no test")

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "rejected"
    finally:
        store.close()
    assert backlog() == []
