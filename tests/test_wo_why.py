"""`jarvis wo why` — the diagnosis report, §6 of the order-observability spec
(docs/specs/2026-09-24-order-observability.md).

The question it answers is the one §1 measured as the most-asked: "this order has not
moved in three hours. What is it waiting for, and what do I type?" Everything it prints
already existed, scattered over three surfaces that each showed a slice, so the tests here
are about the AGGREGATION and about the four honesty rules the spec puts on it:

* two sources that disagree are BOTH reported and neither wins — a silent pick is how
  #197 and #711 reached the user as a confidently wrong sentence;
* absent is never zero (issue #227) — no turn, no transcript and no timeline each get
  `None` plus a sentence, because an unmeasurable clock and an idle one are different
  answers;
* a call that never reached the model has made no judgement (pinned `kn-40db1828`), so it
  is reported as UNREACHABLE and never as something the model decided;
* only commands the OS would accept right now are offered — offering one that will be
  refused teaches the user to distrust the surface.
"""

from __future__ import annotations

import json

import pytest

from jarvis import agent_usage, cli, db, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def validating_catalog(tmp_path, project):
    """The one fleet shape where a round can be forced: the panel is ON."""
    data = {
        "os": {"defaults": {"model": "sonnet", "max_in_flight": 50},
               "validation": {"enabled": True},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "validating-catalog.json"
    path.write_text(json.dumps(data))
    return path


def an_order(daemon, title: str = "build the exporter") -> dict:
    """A dispatched work order — one tick, one worker."""
    wo = ops.create_work_order("proj_a", title)
    daemon.tick()
    return wo


def at(monkeypatch, ts: float):
    """Write the next store rows as if the clock read `ts`.

    `add_event` stamps with `db.now()` and nothing lets a caller pass a moment, so a hold
    of a stated length can only be built by moving the clock. Patched on `db` because
    that is the module `project_store` calls through.
    """
    monkeypatch.setattr(db, "now", lambda: ts)


# -- 1. the blocker and the status label agree -------------------------------------------


def test_assumptions_blocker_agrees_with_the_user_blockers(started, project):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default")

    d = ops.diagnose(wo["id"])

    assert d["wo_id"] == wo["id"]
    assert d["project"] == "proj_a"
    assert d["blocker"]["what"] == "assumptions"
    assert d["needs_you"] and "assumption" in d["needs_you"][0].lower()
    # The point of the report: on an order where both sources can see the same fact they
    # say the same thing, and the user is told nothing about a conflict that is not there.
    assert d["disagreements"] == []
    assert d["status_label"]


# -- 2. where two disagree, both are reported --------------------------------------------


def test_disagreement_reported_when_the_blocker_does_not_need_the_user(started,
                                                                      monkeypatch):
    """`true_blockers` wants the user; `waiting_on` says a turn is in flight."""
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default")
    monkeypatch.setattr(ops, "waiting_on", lambda store, w: {
        "what": "turn_running", "stalled": False,
        "detail": "a turn is in flight — the worker is working"})

    d = ops.diagnose(wo["id"])

    assert len(d["disagreements"]) == 1
    sentence = d["disagreements"][0]
    assert "waiting_on" in sentence and "true_blockers" in sentence
    assert "turn_running" in sentence
    # Neither answer is dropped and neither is crowned.
    assert d["blocker"]["what"] == "turn_running"
    assert d["needs_you"]


def test_disagreement_reported_when_nothing_needs_the_user(started, monkeypatch):
    """The other direction: `waiting_on` names a user wait `true_blockers` does not."""
    wo = an_order(started)
    monkeypatch.setattr(ops, "waiting_on", lambda store, w: {
        "what": "assumptions", "stalled": False,
        "detail": f"1 assumption(s) await your review — `jarvis wo review {wo['id']}`"})
    monkeypatch.setattr(invariants, "true_blockers", lambda store, w, now=None: [])

    d = ops.diagnose(wo["id"])

    assert len(d["disagreements"]) == 1
    sentence = d["disagreements"][0]
    assert "waiting_on" in sentence and "true_blockers" in sentence
    assert "assumptions" in sentence
    assert d["needs_you"] == []


# -- 3. absent is never zero -------------------------------------------------------------


def test_an_order_with_no_turn_reports_none_and_says_so(started, project):
    wo = ops.create_work_order("proj_a", "not dispatched yet")  # no tick: no worker

    d = ops.diagnose(wo["id"])

    assert d["clock"]["last_turn"] is None
    assert d["clock"]["last_turn"] != 0
    assert d["clock"]["turn_open"] is False
    assert "note" in d["clock"] and d["clock"]["note"]
    # And the report says out loud that it read the record rather than the process.
    assert "jarvis watch" in d["clock"]["live_note"]


# -- 4. no transcript means the residual is unmeasurable, not nil ------------------------


def test_missing_transcript_leaves_the_residual_none(started):
    wo = ops.create_work_order("proj_a", "never dispatched, never transcribed")

    d = ops.diagnose(wo["id"])

    residual = d["holds"]["unexplained"]
    assert residual["seconds"] is None
    assert residual["seconds"] != 0
    assert residual["residual"] is True
    assert "transcript" in residual["note"]


# -- 5. a call that never reached the model decided nothing ------------------------------


#: Words that would turn an unreachable call into a judgement it never made. Asserted
#: against the block rather than the payload: the rest of the report legitimately talks
#: about reviews and escalations.
JUDGEMENT_WORDS = ("decision", "decided", "escalat", "refus", "verdict", "answered it")


def test_a_failed_os_call_is_reported_as_unreachable(started):
    wo = an_order(started)
    agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                       label="should this export CSV", model="sonnet", ok=False)
    agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                       label="should this export CSV", model="sonnet", ok=True)

    calls = ops.diagnose(wo["id"])["os_calls"]

    assert calls["calls"] == 2 and calls["failed"] == 1
    outcomes = sorted(r["outcome"] for r in calls["rows"])
    assert outcomes == ["answered", "unreachable"]
    assert "unreachable" in calls["note"]
    unreachable = [r for r in calls["rows"] if r["outcome"] == "unreachable"]
    blob = json.dumps({"rows": unreachable, "note": calls["note"]}).lower()
    for word in JUDGEMENT_WORDS:
        assert word not in blob, word


# -- 6. only commands the OS would accept right now --------------------------------------


def commands(d: dict) -> list[str]:
    return [c["command"] for c in d["commands"]]


def test_validation_force_is_withheld_with_its_refusal(started):
    wo = an_order(started)

    d = ops.diagnose(wo["id"])

    assert not any(c.startswith("jarvis validation force") for c in commands(d))
    assert d["refusals"] and any("force" in r for r in d["refusals"])


def test_validation_force_is_offered_when_it_would_be_accepted(jarvis_home, fake_claude,
                                                               validating_catalog,
                                                               project):
    ops.start_os(str(validating_catalog), foreground=True)
    wo = ops.create_work_order("proj_a", "delivered behind a pull request")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "waiting_pr_merge",
                         pr_url="https://example.invalid/pr/1")
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert any(c.startswith(f"jarvis validation force {wo['id']}")
               for c in commands(d))
    assert not any("force" in r for r in d["refusals"])


def test_unblock_is_offered_bare_when_a_dependency_is_dead(started, project):
    """A stranded order: the default form cuts the dead edge, so it is the one offered."""
    first = ops.create_work_order("proj_a", "the thing it builds on")
    second = ops.create_work_order("proj_a", "the dependent", depends_on=[first["id"]])
    store = ProjectStore(project)
    try:
        store.set_status(first["id"], "cancelled")
    finally:
        store.close()

    d = ops.diagnose(second["id"])

    assert f"jarvis wo unblock {second['id']}" in commands(d)
    assert f"jarvis wo unblock {second['id']} --all" not in commands(d)


def test_unblock_needs_all_when_the_dependency_is_still_live(started, project):
    """A live edge: `unblock_work_order` refuses the bare form, so only --all shows."""
    first = ops.create_work_order("proj_a", "the thing it builds on")
    second = ops.create_work_order("proj_a", "the dependent", depends_on=[first["id"]])

    d = ops.diagnose(second["id"])

    assert f"jarvis wo unblock {second['id']} --all" in commands(d)
    assert f"jarvis wo unblock {second['id']}" not in commands(d)
    [why] = [c["why"] for c in d["commands"]
             if c["command"].endswith("--all")]
    assert "LIVE" in why


# -- 6b. a capped list is never presented as the whole of it ---------------------------


def test_os_calls_report_their_cap_and_the_counts_as_a_floor(started):
    wo = an_order(started)
    for _ in range(3):
        agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                           label="a question", model="sonnet", ok=True)

    calls = ops._diagnose_os_calls(wo["id"], limit=2, shown=1)

    assert calls["calls"] == 2 and calls["limit"] == 2 and calls["capped"] is True
    assert "capped at the newest 2 calls" in calls["note"]
    assert "floor" in calls["note"]
    # The human listing's arithmetic, done here so the renderer only slices.
    assert calls["shown_limit"] == 1 and calls["more"] == 1


def test_an_uncapped_list_says_nothing_about_a_cap(started):
    wo = an_order(started)
    agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                       label="a question", model="sonnet", ok=True)

    calls = ops.diagnose(wo["id"])["os_calls"]

    assert calls["capped"] is False and calls["more"] == 0
    assert calls["limit"] == ops.OS_CALLS_LIMIT
    assert "capped" not in calls["note"]


def test_ack_is_withheld_while_assumptions_are_pending(started):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default")

    d = ops.diagnose(wo["id"])

    assert not any(c.startswith("jarvis wo ack") for c in commands(d))
    # The command that IS the way through is the one `waiting_on` already names.
    assert f"jarvis wo review {wo['id']}" in d["blocker"]["detail"]


# -- 7. the holds, with the open one marked ----------------------------------------------


def test_holds_are_listed_and_by_cause_sums_to_them(started, project, monkeypatch):
    wo = ops.create_work_order("proj_a", "held twice")
    now = db.now()
    store = ProjectStore(project)
    try:
        at(monkeypatch, now - 600)
        store.add_event(wo["id"], "question_asked", {"neo_question_id": 7})
        at(monkeypatch, now - 300)
        store.add_event(wo["id"], "neo_answered", {"neo_question_id": 7})
        at(monkeypatch, now - 100)
        store.add_event(wo["id"], "gate_requested", {"approval_id": 3})
    finally:
        store.close()
    monkeypatch.setattr(db, "now", lambda: now)

    held = ops.diagnose(wo["id"])["holds"]

    assert len(held["episodes"]) == 2
    assert [e["cause"] for e in held["episodes"]] == ["neo_question", "gate"]
    assert held["open"] is not None and held["open"]["cause"] == "gate"
    assert [e for e in held["episodes"] if e["open"]] == [held["open"]]
    total = sum(e["seconds"] for e in held["episodes"])
    assert sum(held["by_cause"].values()) == pytest.approx(total, abs=2.0)
    # Biggest first, so a reader's eye lands on the hold that cost the afternoon.
    assert list(held["by_cause"]) == ["neo_question", "gate"]


# -- 8. a settled order diagnoses cleanly ------------------------------------------------


def test_a_completed_order_says_how_it_settled(started, project, settle_turns):
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        # The dispatch turn is a real process; a work order with one still in flight is
        # waiting on the turn, whatever its status column says.
        assert settle_turns(store)
        store.set_status(wo["id"], "completed")
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert d["status"] == "completed"
    assert d["blocker"]["what"] == "completed"
    assert "nothing is running to nudge" in d["blocker"]["detail"]
    assert d["needs_you"] == []
    assert d["clock"]["last_status"]["status"] == "completed"
    assert not any(c.startswith("jarvis wo resume-auto") for c in commands(d))


# -- 9. the CLI hands the dict over unchanged --------------------------------------------


def test_cli_json_round_trips_the_ops_dict(started, capsys):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default")

    assert cli.main(["wo", "why", wo["id"], "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    fresh = ops.diagnose(wo["id"])

    assert set(printed) == set(fresh)
    for key in ("wo_id", "project", "title", "status", "blocker", "needs_you",
                "notes", "disagreements", "commands", "refusals"):
        assert printed[key] == fresh[key], key


def test_cli_human_rendering_names_the_blocker(started, capsys):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default")

    for _ in range(ops.OS_CALLS_SHOWN + 2):
        agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                           label="a question", model="sonnet", ok=True)

    assert cli.main(["wo", "why", wo["id"]]) == 0
    out = capsys.readouterr().out

    assert wo["id"] in out
    assert f"jarvis wo review {wo['id']}" in out
    # The listing is cut at the payload's depth and the remainder comes from the payload
    # too — 200 lines of call table in a terminal is not a diagnosis.
    assert out.count("  answered  ") == ops.OS_CALLS_SHOWN
    assert "… and 2 older call(s)" in out
