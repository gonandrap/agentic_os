"""The confirmation pass: a provisional verdict only settles once a diff exists.

docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §7.

Three properties carry the section, and each gets its own block:

* **`decide` RUNS UNCHANGED UNDERNEATH.** A provisional verdict is not a ticket past any
  of its seven conditions, so every one of them is exercised again on a row that carries
  one. A confirmation pass that skipped the table would settle assumptions the ask pass
  itself would refuse to put to Neo.
* **CONFIRMING IS THE NARROW PATH**, `test_autoreview.py`'s rule one section along:
  anything that is not a routine-stakes approval of the CONFIRMATION question leaves the
  assumption pending, with both readings on the record.
* **THE OBJECTION GATE IS A RETRY, NOT A REFUSAL.** §6.6 withdraws on the transition into
  `needs_review`; an outstanding objection means "not yet" and costs the assumption
  nothing.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from jarvis import autoreview, ops
from jarvis.project_store import ProjectStore

# The fixtures are `test_autoreview.py`'s, imported rather than re-written: the two
# passes must be driven through the same parked work order or they are not comparable.
from tests.test_autoreview import (  # noqa: F401 — pytest fixtures
    PR,
    ROUTINE,
    SECRET,
    ask,
    assumption,
    cfg,
    drain,
    events,
    park,
    questions,
    started,
)

WO = {"id": "wo-1", "status": "needs_review", "title": "t", "description": "d"}

PROVISIONAL_REASON = "a naming convention, judged while the worker typed"


def judged(**over) -> dict:
    """An assumption carrying an early ACCEPT — the only row §7 has anything to do."""
    return assumption(**{"provisional_verdict": "accept",
                         "provisional_reason": PROVISIONAL_REASON,
                         "provisional_model": "sonnet",
                         "provisional_stakes": "routine",
                         "confirm_question_id": None, **over})


def confirm(**kw):
    return autoreview.decide_confirm(kw.pop("assumption", None) or judged(),
                                     kw.pop("wo", None) or WO,
                                     kw.pop("config", None) or cfg(), **kw)


# -- `decide_confirm`: the four gates of its own ---------------------------------------


def test_an_early_accept_on_a_parked_order_is_put_back_to_neo():
    d = confirm()
    assert d.armed and d.assumption_id == 3 and d.n == 2


def test_an_assumption_no_early_pass_judged_has_nothing_to_confirm():
    """The historical row, and the ordinary one on a fleet that never enabled §5."""
    assert confirm(assumption=assumption()).code == autoreview.HELD_UNJUDGED


def test_an_early_objection_is_never_confirmed_because_nothing_was_approved():
    """§7's last paragraph: an `object` approved nothing, so there is nothing to confirm
    and no second call to spend. The user decides it, three ways."""
    d = confirm(assumption=judged(provisional_verdict="object"))
    assert d.code == autoreview.HELD_OBJECTED


def test_a_confirmation_already_filed_is_not_filed_twice():
    """One question per assumption per pass — the invariant `confirm_question_id` exists
    to keep, given `neo_question_id` is deliberately excluded from condition 6 here."""
    d = confirm(assumption=judged(confirm_question_id=77))
    assert d.code == autoreview.HELD_CONFIRMING and "77" in d.reason


def test_an_objection_still_in_flight_holds_the_whole_pass():
    """§7 runs after §6.6, never beside it: settling an assumption while a message about
    it is still on a wire to the worker is the contradiction the ordering removes."""
    d = confirm(objections_outstanding=True)
    assert d.code == autoreview.HELD_OBJECTION_IN_FLIGHT


# -- `decide`'s seven conditions, on a row that carries a provisional verdict -----------


@pytest.mark.parametrize("kw, code", [
    ({"config": cfg(auto_review=False)}, autoreview.HELD_DISABLED),
    ({"config": cfg(enabled=False)}, autoreview.HELD_DISABLED),
    ({"wo": {**WO, "status": "running"}}, autoreview.HELD_STATUS),
    ({"assumption": judged(status="accepted")}, autoreview.HELD_SETTLED),
    ({"round_outcome": "escalated"}, autoreview.HELD_PANEL_GAVE_UP),
    ({"refusal_answered": False}, autoreview.HELD_REFUSAL_UNANSWERED),
    ({"assumption": judged(content=SECRET)}, autoreview.HELD_HIGH_STAKES),
])
def test_a_provisional_verdict_is_not_a_ticket_past_any_condition(kw, code):
    """§7.1: `autoreview.decide` runs first, unchanged, and every one of its seven
    conditions must hold. An early reading is an opinion about an intention — it cannot
    buy the order past a panel that gave up or a permission nobody granted."""
    assert confirm(**kw).code == code


def test_the_early_question_does_not_hold_its_own_confirmation():
    """Condition 6 through the documented escape hatch. `neo_question_id` points at the
    EARLY question and always will, so without `asked_question_id` every confirmation
    would hold as `asked` and the pass would never run once."""
    d = confirm(assumption=judged(neo_question_id=41))
    assert d.armed


def test_a_different_question_still_holds_it():
    """The pair: the hatch excludes the assumption's OWN early question and nothing
    else."""
    d = autoreview.decide(judged(neo_question_id=41), WO, cfg(), asked_question_id=9)
    assert d.code == autoreview.HELD_ASKED


# -- the daemon: asking the second question --------------------------------------------


EARLY_QUESTION = 99


def provisional(store, wo, *, verdict: str = "accept",
                reason: str = PROVISIONAL_REASON) -> dict:
    """Stamp §5's verdict by hand. Section 5 has not landed; its COLUMNS have (§4).

    The early QUESTION is linked too, at an id no test ever files, because that link is
    what condition 6 trips on: a fixture without it would leave the escape hatch
    untested at the daemon and every confirmation would still pass.
    """
    (row,) = [a for a in store.all_assumptions(wo["id"])]
    store.record_provisional(row["id"], verdict=verdict, reason=reason,
                             model="sonnet", stakes="routine")
    store.link_assumption_question(row["id"], EARLY_QUESTION)
    return store.all_assumptions(wo["id"])[0]


def _git(cwd, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def with_diff(started, **kw):
    """A parked order whose worker worktree really has a commit in it.

    `park` alone leaves the repository empty, and `evidence.collect_work_order` answers
    an empty packet for one — which this section ASKS on anyway. So the diff has to be
    real here or the test would pass with the evidence never collected at all.
    """
    path = started.catalog.project("proj_a").path
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    worktree = path / ".claude" / "worktrees" / "wt"
    _git(path, "worktree", "add", "-q", "-b", "wo-branch", str(worktree))
    (worktree / "render.py").write_text("def _render_row():\n    return 1\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "the change under review")
    store, wo = park(started, auto_review=True, **kw)
    store.update_work_order(wo["id"], worktree="wt")
    return store, store.get_work_order(wo["id"])


def test_the_confirmation_carries_the_diff_the_early_pass_never_had(started):
    """THE SECOND CALL IS THE FEATURE (Neo, question 549). The cheap design — confirm in
    code, re-ask only if something changed — was refused: a mid-turn verdict never had a
    result summary, so "something changed" is always true."""
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert confirmation["kind"] == autoreview.QUESTION_KIND
    assert PROVISIONAL_REASON in confirmation["question"]
    assert "opened a PR" in confirmation["question"]       # the result summary
    assert "render.py" in confirmation["question"]         # the diff
    row = store.all_assumptions(wo["id"])[0]
    assert row["confirm_question_id"] == confirmation["id"]
    assert row["neo_question_id"] == EARLY_QUESTION   # the early link is not overwritten


def test_running_the_confirmation_pass_twice_asks_once(started):
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)
    ask(started, store)

    assert len(questions()) == 1


def test_a_confirmation_in_flight_is_not_a_note_on_the_timeline(started):
    """`confirming` is suppressed for `asked`'s reason: the question is filed, which is
    the pass WORKING. Unsuppressed it would put "held — already with Neo to confirm" on
    every assumption the OS is in the middle of confirming, every reconcile tick."""
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)
    ask(started, store)

    assert events(store, wo["id"], "autoreview_held") == []


def test_an_early_objection_costs_no_second_call_at_the_daemon_either(started):
    """The pure hold, asserted where the money is spent."""
    store, wo = with_diff(started)
    provisional(store, wo, verdict="object")

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_OBJECTED


def test_a_historical_row_behaves_exactly_as_it_did_before_this_section(started):
    """Every new column empty: the ordinary `decide`/`propose` path, one question, and
    nothing linked to a confirmation."""
    store, wo = park(started, auto_review=True)

    ask(started, store)

    (q,) = questions()
    row = store.all_assumptions(wo["id"])[0]
    assert row["neo_question_id"] == q["id"] and row["confirm_question_id"] is None
    assert "CONFIRM" not in q["question"]


# -- the daemon: the outstanding-objection gate ----------------------------------------


def test_an_objection_in_flight_holds_the_pass_and_says_so(started):
    """kn-22ba6087: a guard that returns early must still record why. `objection_in_
    flight` is therefore NOT on `_note_autoreview_held`'s suppression list — unlike the
    four holds that mean "never a candidate", this one is a state the user can see."""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_OBJECTION_IN_FLIGHT


def test_an_objection_the_worker_actually_received_does_not_hold_anything(started):
    """DELIVERED IS NOT OUTSTANDING. The worker was told, the record can explain how and
    when, and the assumption the OS was going to confirm is confirmed."""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)
    store.mark_objection_delivered(row["id"])

    ask(started, store)

    assert len(questions()) == 1
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is not None


def test_the_gate_is_a_retry_and_not_a_refusal(started):
    """"Not yet" costs the assumption nothing: the withdrawal runs, the next tick asks."""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)
    ask(started, store)

    store.withdraw_objection(row["id"])
    ask(started, store)

    assert len(questions()) == 1
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is not None


# -- the daemon: what comes back -------------------------------------------------------


def test_a_confirmed_assumption_settles_exactly_as_an_ordinary_acceptance_does(started):
    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    (confirmation,) = questions()

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "accepted"
    assert row["decided_by"] == "neo" and row["decided_by"] != "user"
    assert store.pending_assumptions(wo["id"]) == []
    (event,) = events(store, wo["id"], "autoreview_confirmed")
    assert event["neo_question_id"] == confirmation["id"]
    assert event["provisional_reason"] == PROVISIONAL_REASON
    assert event["provisional_verdict"] == "accept"


def test_a_verdict_that_does_not_confirm_leaves_the_assumption_with_the_user(started):
    """BOTH READINGS, side by side: what Neo thought while the work ran, and what it
    thought once it saw the result. That is strictly more than the user gets today."""
    store, wo = with_diff(started, assumptions=(f"FORCE_DENY — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    (confirmation,) = questions()

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_confirmed") == []
    (event,) = events(store, wo["id"], "autoreview_unconfirmed")
    assert event["provisional_verdict"] == "accept"
    assert event["provisional_reason"] == PROVISIONAL_REASON
    assert event["provisional_model"] == "sonnet"
    assert "test-forced" in event["reason"]
    assert event["neo_question_id"] == confirmation["id"]
    assert next(q for q in questions()
                if q["id"] == confirmation["id"])["status"] == "escalated"


@pytest.mark.parametrize("marker", ["FORCE_FAIL", "FORCE_GARBAGE"])
def test_a_failure_is_never_a_confirmation(started, marker):
    """The transport failing and the model answering something else are the same fact
    here: nothing was confirmed, so nothing settles."""
    store, wo = with_diff(started, assumptions=(f"{marker} — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_confirmed") == []
