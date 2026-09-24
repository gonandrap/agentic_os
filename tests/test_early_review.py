"""Neo judges an assumption while the worker is still typing, and settles nothing.

docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §5.

Three properties, and the third is the one the whole feature rests on:

* **`decide_early` is a table and is tested as one**, without a daemon and without a
  network — `test_autoreview.py`'s reason for testing `decide` that way.
* **BOTH VERDICTS ARE FORMED HERE AND NEITHER IS SENT.** An acceptance and an objection
  both end as a row and an event; turning `object` into a message is §6's, and a test here
  that asserted a message would be asserting a section this one does not own.
* **NOTHING SETTLES.** Asserted from every side a settle could show up on — the row, the
  pending list, the work order's status, the timeline — and then in the strong form, with
  `ops.accept_assumption` monkeypatched to raise for the drain's duration. That function
  lands the work order behind it, so calling it on a ruling formed with no diff is the
  failure this feature cannot have.
"""

from __future__ import annotations

import json

import pytest

from jarvis import autoreview, ops
from jarvis.catalog import ValidationConfig, load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

#: `test_autoreview.ROUTINE`'s shape: an assumption no reviewer would hold.
ROUTINE = "named the helper `_render_row`, matching the two beside it"

RUNNING = {"id": "wo-1", "status": "running", "title": "t", "description": "d"}


def cfg(**over) -> ValidationConfig:
    return ValidationConfig(**{"enabled": True, "auto_review": True, **over})


def assumption(**over) -> dict:
    return {"id": 3, "n": 2, "status": "pending", "content": ROUTINE,
            "neo_question_id": None, "provisional_verdict": "", **over}


def decide_early(**kw):
    return autoreview.decide_early(kw.pop("assumption", None) or assumption(),
                                   kw.pop("wo", None) or RUNNING,
                                   kw.pop("config", None) or cfg(), **kw)


# -- `decide_early`: the condition table, one test per condition ------------------------


def test_a_routine_assumption_on_a_running_work_order_is_put_to_neo():
    d = decide_early()
    assert d.armed and d.assumption_id == 3 and d.n == 2


@pytest.mark.parametrize("kw", [{"enabled": False}, {"auto_review": False}])
def test_neither_half_of_the_switch_arms_it_alone(kw):
    assert decide_early(config=cfg(**kw)).code == autoreview.HELD_DISABLED


@pytest.mark.parametrize("status", ["needs_review", "pending", "blocked",
                                    "waiting_pr_merge", "completed", "cancelled",
                                    "validating", "budget_exhausted", ""])
def test_only_an_order_with_a_worker_at_a_keyboard_is_reviewed_early(status):
    """The objection this arms is guidance to somebody mid-task. On every status here
    there is nobody to tell, and `needs_review` is `decide`'s pass, not this one's."""
    d = decide_early(wo={**RUNNING, "status": status})
    assert not d.armed and d.code == autoreview.HELD_STATUS


@pytest.mark.parametrize("status", ["accepted", "rejected"])
def test_an_assumption_somebody_already_settled_is_not_reopened(status):
    assert decide_early(assumption=assumption(status=status)).code == \
        autoreview.HELD_SETTLED


@pytest.mark.parametrize("verdict", ["accept", "object"])
def test_an_assumption_this_pass_has_already_judged_is_not_judged_again(verdict):
    """Its own hold code, and not `settled`: the row is still pending and still the
    user's — what exists is an opinion, and a second one would overwrite the first."""
    d = decide_early(assumption=assumption(provisional_verdict=verdict))
    assert d.code == autoreview.HELD_JUDGED and verdict in d.reason


def test_a_panel_that_gave_up_is_not_ruled_on_for_the_user():
    assert decide_early(round_outcome="escalated").code == autoreview.HELD_PANEL_GAVE_UP


@pytest.mark.parametrize("outcome", ["", "passed", "failed", "pending"])
def test_every_other_round_outcome_leaves_the_assumption_reviewable(outcome):
    assert decide_early(round_outcome=outcome).armed


def test_a_refusal_the_worker_has_not_answered_holds_everything_behind_it():
    assert decide_early(refusal_answered=False).code == \
        autoreview.HELD_REFUSAL_UNANSWERED


def test_an_assumption_is_asked_about_once_and_never_again():
    d = decide_early(assumption=assumption(neo_question_id=41))
    assert d.code == autoreview.HELD_ASKED and "41" in d.reason
    # ...and the question being delivered does not block itself, `decide`'s rule.
    assert decide_early(assumption=assumption(neo_question_id=41),
                        asked_question_id=41).armed


def test_the_regex_net_holds_a_high_stakes_assumption_before_a_call_is_made():
    d = decide_early(assumption=assumption(
        content="pointed it at the production database"))
    assert d.code == autoreview.HELD_HIGH_STAKES and "production" in d.reason


def test_the_settle_guard_is_untouched_and_still_refuses_a_running_order():
    """**`decide` IS NOT EDITED BY THIS FEATURE.** It is the only guard on
    `ops.accept_assumption` -> `ops.land_when_cleared`, so the same row that arms
    `decide_early` must still hold it — two functions, two failure directions."""
    a = assumption()
    assert decide_early(assumption=a).armed
    held = autoreview.decide(a, RUNNING, cfg())
    assert not held.armed and held.code == autoreview.HELD_STATUS


# -- the daemon: both verdicts, on a running order --------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    # `test_autoreview.started`'s reason: these tests stand in for the user at the
    # console, and `ops._refuse_worker_write` refuses a worker on purpose.
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def running(daemon, *, auto_review: bool = True,
            assumptions: tuple[str, ...] = (ROUTINE,)) -> tuple[ProjectStore, dict]:
    """A work order MID-TURN, with assumptions its worker recorded as it went.

    `test_autoreview.park`'s opposite number and deliberately much smaller: there is no
    pull request, no validation round and no result summary, because the whole point is
    that none of that exists yet.
    """
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_review = auto_review
    wo = ops.create_work_order("proj_a", "add feature X", description="d")
    store = ProjectStore(spec.path)
    store.set_status(wo["id"], "running")
    for text in assumptions:
        ops.assume(wo["id"], text)
    return store, store.get_work_order(wo["id"])


def ask(daemon, store):
    daemon.auto_review(daemon.catalog.project("proj_a"), store)


def drain(daemon):
    daemon._neo_drain()


def questions(kind: str = "assumption") -> list[dict]:
    neo_store = NeoStore()
    try:
        return [q for q in neo_store.list_questions() if q["kind"] == kind]
    finally:
        neo_store.close()


def events(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


def nothing_settled(store, wo_id: str) -> None:
    """EVERY SIDE A SETTLE WOULD SHOW UP ON. Called by both verdicts' tests.

    A provisional verdict is, to every existing caller, byte-for-byte a pending
    assumption (§4.1) — so this asserts the row, the list every gate reads, the work
    order's own status and the timeline, rather than any one of them.
    """
    (row,) = store.all_assumptions(wo_id)
    assert row["status"] == "pending"
    assert row["decided_by"] == ""
    assert row["decided_reason"] == "" and row["decided_model"] == ""
    assert len(store.pending_assumptions(wo_id)) == 1
    assert store.get_work_order(wo_id)["status"] == "running"
    assert events(store, wo_id, "autoreview_accepted") == []


@pytest.fixture()
def no_settling(monkeypatch):
    """THE STRONG FORM: `ops.accept_assumption` may not be reached at all.

    Asserting the row afterwards proves the state; this proves the CALL was never made,
    which is what stops a future edit settling and then tidying up after itself.
    """
    def boom(*a, **kw):
        raise AssertionError("the early pass must never settle an assumption")

    monkeypatch.setattr(ops, "accept_assumption", boom)


def test_an_agreement_is_recorded_as_provisional_and_settles_nothing(
        started, catalog_file, no_settling):
    ops.set_config("validation.auto_review", True, project="proj_a",
                   reason="the panel has been right for a month",
                   catalog_path=str(catalog_file))
    store, wo = running(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))

    ask(started, store)
    (q,) = questions()
    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["provisional_verdict"] == "accept"
    assert row["provisional_model"]
    assert row["provisional_stakes"] == "routine"
    assert row["provisional_ts"]
    assert row["provisional_config_version"] == ops.current_config_version() is not None
    assert row["neo_question_id"] == q["id"]
    (event,) = events(store, wo["id"], "autoreview_provisional")
    assert event["verdict"] == "accept" and event["neo_question_id"] == q["id"]
    nothing_settled(store, wo["id"])


def test_a_disagreement_is_recorded_as_an_objection_and_nothing_else(
        started, no_settling):
    """§5.3's seam: the verdict is written down and this section STOPS. No message, no
    envelope, no escalation to the user — the objection is §6's to send, off this row."""
    store, wo = running(started, assumptions=(f"FORCE_DENY — {ROUTINE}",))

    ask(started, store)
    (q,) = questions()
    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["provisional_verdict"] == "object"
    assert "the worker chose wrongly" in row["provisional_reason"]
    assert row["provisional_model"]
    (event,) = events(store, wo["id"], "autoreview_provisional")
    assert event["verdict"] == "object" and event["reason"] == row["provisional_reason"]
    # NOT the user's problem: Neo disagreed with a worker that is still typing, which is
    # the whole licence for this feature.
    assert questions()[0]["status"] != "escalated"
    assert events(store, wo["id"], "autoreview_escalated") == []
    # §6 files and sends. Nothing here does.
    assert row["objection_envelope_id"] is None
    assert row["objection_transport"] == ""
    assert store.list_messages(wo["id"]) == []
    nothing_settled(store, wo["id"])
    assert q["id"] == row["neo_question_id"]


@pytest.mark.parametrize("prefix,escalated", [
    ("", True),                     # the fake's default: Neo escalates
    ("FORCE_ACCEPT_HIGH", True),    # accepted, and the stakes net would not take it
    ("FORCE_FAIL", False),          # a model that was never reached made no judgement
])
def test_a_verdict_the_os_cannot_form_records_nothing_and_leaves_it_pending(
        started, no_settling, prefix, escalated):
    """§5.3's third outcome. **AN OVERRIDE IS NOT AN OBJECTION**: `read_ruling` returns
    the same `accept=False` for "Neo declined" and "Neo accepted, stakes high", and a
    high-stakes assumption is never objected to (§2). A failed call is not an answer
    either — the pinned fleet learning — so it forms no verdict of any kind."""
    store, wo = running(started, assumptions=(f"{prefix} — {ROUTINE}".lstrip(" —"),))

    ask(started, store)
    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["provisional_verdict"] == ""
    assert row["provisional_reason"] == "" and row["provisional_model"] == ""
    assert events(store, wo["id"], "autoreview_provisional") == []
    assert bool(events(store, wo["id"], "autoreview_escalated")) is escalated
    nothing_settled(store, wo["id"])


def test_a_worker_that_finishes_while_neo_thinks_does_not_turn_the_ruling_into_a_settle(
        started, no_settling):
    """THE RACE, and it is why the pass that asked is read off the record rather than
    from the status at delivery. Neo was asked about an intention; the work order arriving
    at `needs_review` first does not turn that answer into a judgement of the result, and
    §7 asks again with the diff in hand before anything settles."""
    store, wo = running(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    store.set_status(wo["id"], "needs_review")

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["provisional_verdict"] == "accept"
    assert row["status"] == "pending" and row["decided_by"] == ""
    assert len(store.pending_assumptions(wo["id"])) == 1
    assert events(store, wo["id"], "autoreview_accepted") == []
    assert row["confirm_question_id"] is None


def test_a_high_stakes_assumption_mid_run_is_never_asked_about_at_all(started):
    """Both nets, in this pass too (§2). The first one is about TEXT, so it also holds
    the sentence out of the sibling context of the routine assumption beside it."""
    store, wo = running(started, assumptions=(
        "used the live production credentials in the smoke test",
        f"FORCE_ACCEPT — {ROUTINE}"))

    ask(started, store)

    asked = questions()
    assert len(asked) == 1
    assert "live production credentials" not in asked[0]["question"]
    assert "withheld" in asked[0]["question"]
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_HIGH_STAKES


def test_a_project_that_has_not_opted_in_is_never_judged_early(started):
    store, wo = running(started, auto_review=False)

    ask(started, store)

    assert questions() == []
    assert events(store, wo["id"], "autoreview_held") == []
    assert store.all_assumptions(wo["id"])[0]["provisional_verdict"] == ""


def test_asking_twice_asks_once_and_a_recorded_verdict_stops_a_third(started):
    store, wo = running(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))

    ask(started, store)
    ask(started, store)
    drain(started)
    ask(started, store)

    assert len(questions()) == 1
    # The second pass held on `asked` and the third on `judged_early`, and NEITHER is
    # recorded: both are the pass working, and a tick's worth of events for the rest of a
    # worker's run would bury the ones that mean something.
    assert events(store, wo["id"], "autoreview_held") == []
