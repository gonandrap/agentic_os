"""A validation round whose seats were KILLED BY A SIGNAL is re-judged, and investigated.

GitHub issue #1039. The live failure is wo-ab6c309d round 1: all four seats exited 143
(SIGTERM) within the same second, at the same elapsed time, with no bytes on either
stream — one external event, not four model failures. The empty detail classified as
nothing at all, so the round closed `escalated` and the order sat in `needs_review` for
nine hours with no rc, no co-incidence and no pointer to what else happened at 18.5s.

Spec: docs/superpowers/specs/
2026-10-14-a-killed-validation-panel-opens-an-investigation.md §4.

These drive the REAL `Daemon._validate_work_order`, like `tests/test_validation_auth_hold.py`
and for the same reason: the defect spans the transport's classification, the seat, the
panel's verdict and the round machine, and a test of any one alone passed throughout.
"""

from __future__ import annotations

import pytest

from jarvis import claude_cli, db, ops
from jarvis.claude_cli import Killed, KilledError
from jarvis.daemon import VALIDATION_KILLED_CAUSE, Daemon
from jarvis.invariants import true_blockers
from jarvis.project_store import ProjectStore
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)

#: The message `claude_cli._cli_failure` builds for the incident's own exit code.
KILLED_MESSAGE = "claude -p judge... was killed by a signal (rc=143) and wrote nothing"


def killed(rc: int = 143) -> KilledError:
    return KilledError(Killed(message=KILLED_MESSAGE, rc=rc))


def _rounds(store, wo_id):
    return [(r["round"], r["outcome"]) for r in store.validation_rounds(wo_id=wo_id)]


def _killed_events(store, wo_id):
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("cause") == VALIDATION_KILLED_CAUSE]


def _investigations(store):
    return [row for row in store.list_feature_orders(kind="investigation")]


def _submitted(fleet, **catalog):
    if catalog:
        fleet.reconfigure(**catalog)
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    return wo


# -- 1. the round is closed `failed` and re-judged, not parked -------------------------


def test_a_killed_panel_closes_the_round_failed_and_spends_no_round(fleet):
    """Spec §4, steps 2 and 3: `counted_validation_rounds` ignores a `failed` round, so
    the submitter pays nothing and the next tick picks the SAME round up."""
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet)

    fleet.drain()

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert _rounds(store, wo["id"]) == [(1, "failed")]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0
        assert fresh["status"] == "validating", "a kill settled the work order"
        assert fresh["needs_attention"] == 0
        assert true_blockers(store, fresh) == [], (
            "nobody judged the work and nobody was asked to")
        [event] = _killed_events(store, wo["id"])
        assert event["cause"] == VALIDATION_KILLED_CAUSE
        assert (event["rc"], event["round"], event["attempt"]) == (143, 1, 1)
        assert "rc=143" in event["error"]
    finally:
        store.close()


def test_the_same_round_is_judged_again_on_the_next_tick(fleet):
    """Spec §4, step 2: no new re-judge mechanism and no human anywhere in it — the
    submitter still spends round ONE."""
    fleet.daemon.validator = Validator(killed(), passed())
    wo = _submitted(fleet)

    fleet.drain()
    fleet.drain()

    store = fleet.store()
    try:
        assert _rounds(store, wo["id"]) == [(1, "passed")]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    finally:
        store.close()


def test_the_cause_is_its_own_and_never_transport(fleet):
    """Spec §4, step 3: every surface that reads these payloads tells the two apart only
    by this string, and `cause: "transport"` would be the OS stating an outage it never
    observed."""
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet)

    fleet.drain()

    store = fleet.store()
    try:
        causes = [db.from_json(e["payload"], {}).get("cause")
                  for e in store.events_of_kind(wo["id"], "validation_failed")]
        assert causes == [VALIDATION_KILLED_CAUSE]
        assert VALIDATION_KILLED_CAUSE == "killed"
    finally:
        store.close()


# -- 2. the investigation ---------------------------------------------------------------


def test_the_first_kill_files_an_investigation_carrying_the_facts_by_reference(fleet):
    """Spec §4a: what a fresh session with no memory of this needs — the seats by name
    with their rc, latency and status, the round, the moments, and the pointers to check.
    REFERENCE, NEVER PAYLOAD: no seat reply is copied in (rejected alternative 6)."""
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet)

    fleet.drain()

    store = fleet.store()
    try:
        [inv] = _investigations(store)
        why = inv["description"]
        assert wo["id"] in why and "rc=143" in why
        assert "round 1" in why
        assert "jarvis alarms" in why
        assert "retry" in why.lower(), "it must say a retry is not the answer"
    finally:
        store.close()


def test_a_second_kill_finds_the_first_investigation_and_files_nothing(fleet):
    """Spec §4a: `ops.live_investigation` is asked FIRST, so the filing is naturally
    idempotent across retries and no `OpsError` per tick reaches the log."""
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet)

    for _ in range(3):
        fleet.drain()

    store = fleet.store()
    try:
        assert len(_investigations(store)) == 1
        assert len(_killed_events(store, wo["id"])) == 3, (
            "the round close and the event must land on every attempt")
    finally:
        store.close()


def test_a_filing_that_raises_never_costs_the_round_its_record(fleet, monkeypatch):
    """Spec §4a: a lost race between two ticks must not kill the round. The close and the
    event are the work order's own record and land even when no investigation exists."""
    monkeypatch.setattr(ops, "create_investigation_order",
                        lambda *a, **k: (_ for _ in ()).throw(ops.OpsError("raced")))
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet)

    fleet.drain()

    store = fleet.store()
    try:
        assert _investigations(store) == []
        assert _rounds(store, wo["id"]) == [(1, "failed")]
        assert len(_killed_events(store, wo["id"])) == 1
        assert _killed_events(store, wo["id"])[0]["investigation"] == ""
    finally:
        store.close()


# -- 3. the bound is a catalog key, and the escalation points at the investigation ------


def test_at_the_bound_the_order_escalates_naming_the_investigation(fleet):
    """Spec §4b: at `validation.killed_retries` the user is asked, and the sentence
    attaches the investigation id so they have somewhere to go that is not a retry."""
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet, killed_retries=2)

    fleet.drain()
    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "validating"
        [inv] = _investigations(store)
    finally:
        store.close()

    fleet.drain()

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        row = store.latest_validation_round(wo_id=wo["id"])
        assert fresh["status"] == "needs_review"
        assert row["outcome"] == "escalated"
        assert "rc=143" in row["reason"] and "2 times in a row" in row["reason"]
        assert inv["id"] in row["reason"]
        assert _killed_events(store, wo["id"])[-1]["investigation"] == inv["id"]
    finally:
        store.close()


def test_the_escalation_says_so_when_no_investigation_could_be_filed(fleet, monkeypatch):
    """Spec §4b: it never names an id that does not exist."""
    monkeypatch.setattr(ops, "create_investigation_order",
                        lambda *a, **k: (_ for _ in ()).throw(ops.OpsError("raced")))
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet, killed_retries=1)

    fleet.drain()

    store = fleet.store()
    try:
        row = store.latest_validation_round(wo_id=wo["id"])
        assert row["outcome"] == "escalated"
        assert "rc=143" in row["reason"]
        assert "inv-" not in row["reason"], "it named an id that does not exist"
        assert "No investigation could be filed" in row["reason"]
    finally:
        store.close()


def test_zero_retries_escalates_on_the_first_kill(fleet):
    """Spec §4b: `0` is legal and is what a fleet that never wants a re-judge sets."""
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet, killed_retries=0)

    fleet.drain()

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"
    finally:
        store.close()


# -- 4. the two counters can never see each other (user ruling, Neo 1705 condition i) --


def test_kills_spend_none_of_the_transport_budget(fleet):
    """The pin: three kills under a bound of five must not escalate. If the kill were
    folded into the transport count, `VALIDATION_OUTAGE_LIMIT` would give up at three."""
    fleet.daemon.validator = Validator(killed())
    wo = _submitted(fleet, killed_retries=5)

    for _ in range(3):
        fleet.drain()

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "validating"
        assert len(_killed_events(store, wo["id"])) == 3
    finally:
        store.close()


def test_transport_outages_spend_none_of_the_kill_budget(fleet):
    """The other direction: two transport outages under `killed_retries: 1` must not
    escalate — the kill counter cannot see an outage's events either."""
    fleet.daemon.validator = Validator(claude_cli.ClaudeCliError("connection reset"))
    wo = _submitted(fleet, killed_retries=1)

    for _ in range(2):
        fleet.drain()

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "validating"
        assert _killed_events(store, wo["id"]) == []
        assert _investigations(store) == [], "an outage filed an investigation"
    finally:
        store.close()


# -- 5. the branch sits before the generic outage one ----------------------------------


def test_the_killed_branch_takes_precedence_over_the_generic_outage(fleet):
    """`KilledError` is a `ClaudeCliError` subclass, so a clause after the generic
    `if failure is not None` would never be reached (spec §4)."""
    store = ProjectStore(fleet.project)
    try:
        wo = store.create_work_order("t")
        rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
        Daemon(fleet.daemon.catalog)._validation_killed(
            store, fleet.spec, dict(wo), int(rnd["id"]), 1, Killed(KILLED_MESSAGE, 143),
            fleet.spec.validation)

        assert store.get_validation_round(int(rnd["id"]))["outcome"] == "failed"
        assert [e["cause"] for e in _killed_events(store, wo["id"])] == ["killed"]
    finally:
        store.close()


@pytest.mark.parametrize("rc", [143, 137, -15])
def test_every_signal_spelling_reaches_the_same_branch(fleet, rc):
    fleet.daemon.validator = Validator(killed(rc))
    wo = _submitted(fleet)

    fleet.drain()

    store = fleet.store()
    try:
        assert _killed_events(store, wo["id"])[0]["rc"] == rc
    finally:
        store.close()
