"""The confirmation pass: a provisional verdict only settles once a diff exists (docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §7.)"""

from __future__ import annotations

import inspect
import os
import re
import subprocess

import pytest

from jarvis import autoreview, daemon as daemon_mod, evidence, navigation, ops
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

# The fixtures are `test_autoreview.py`'s
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
    """An assumption carrying an early ACCEPT — the only row §7 has anything to do"""
    return assumption(**{"provisional_verdict": "accept",
                         "provisional_reason": PROVISIONAL_REASON,
                         "provisional_model": "sonnet",
                         "provisional_stakes": "routine",
                         "confirm_question_id": None, **over})


def confirm(**kw):
    return autoreview.decide_confirm(kw.pop("assumption", None) or judged(),
                                     kw.pop("wo", None) or WO,
                                     kw.pop("config", None) or cfg(), **kw)


# -- `decide_confirm`: the four gates of its own


def test_an_early_accept_on_a_parked_order_is_put_back_to_neo():
    d = confirm()
    assert d.armed and d.assumption_id == 3 and d.n == 2


def test_an_assumption_no_early_pass_judged_has_nothing_to_confirm():
    """The historical row, and the ordinary one on a fleet that never enabled §5"""
    assert confirm(assumption=assumption()).code == autoreview.HELD_UNJUDGED


def test_an_early_objection_is_never_confirmed_because_nothing_was_approved():
    """§7's last paragraph: an `object` approved nothing"""
    d = confirm(assumption=judged(provisional_verdict="object"))
    assert d.code == autoreview.HELD_OBJECTED


def test_a_confirmation_already_filed_is_not_filed_twice():
    """One question per assumption per pass"""
    d = confirm(assumption=judged(confirm_question_id=77))
    assert d.code == autoreview.HELD_CONFIRMING and "77" in d.reason


def test_an_objection_still_in_flight_holds_the_whole_pass():
    """§7 runs after §6.6, never beside it: settling an assumption while a message about it is still on a..."""
    d = confirm(objections_outstanding=True)
    assert d.code == autoreview.HELD_OBJECTION_IN_FLIGHT


# -- `decide`'s seven conditions, on a row that carries a provisional verdict


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
    """§7.1: `autoreview.decide` runs first, unchanged, and every one of its seven conditions must hold"""
    assert confirm(**kw).code == code


def test_the_early_question_does_not_hold_its_own_confirmation():
    """Condition 6 through the documented escape hatch"""
    d = confirm(assumption=judged(neo_question_id=41))
    assert d.armed


def test_a_different_question_still_holds_it():
    """The pair: the hatch excludes the assumption's OWN early question and nothing else"""
    d = autoreview.decide(judged(neo_question_id=41), WO, cfg(), asked_question_id=9)
    assert d.code == autoreview.HELD_ASKED


def test_a_confirmation_nobody_will_ever_answer_is_asked_again():
    """The confirm pass is guarded by its own column"""
    a = judged(confirm_question_id=77)

    assert confirm(assumption=a, unreachable_question_ids=(77,)).armed
    assert confirm(assumption=a).code == autoreview.HELD_CONFIRMING
    assert confirm(assumption=a,
                   unreachable_question_ids=(9,)).code == autoreview.HELD_CONFIRMING


def test_the_set_reaches_the_conditions_decide_owns_too():
    """It is forwarded, not consumed: a dead EARLY link on a row being confirmed must not hold in..."""
    a = judged(confirm_question_id=77, neo_question_id=41)

    assert confirm(assumption=a, unreachable_question_ids=(77, 41)).armed


# -- a dropped confirmation must not hold an assumption for ever


def test_the_transient_drop_list_is_an_allowlist_of_exactly_three_codes():
    """§4.1, asserted WHOLE so a fourth code is a visible edit rather than an accident"""
    assert autoreview.TRANSIENT_DROPS == frozenset({
        autoreview.HELD_STATUS,
        autoreview.HELD_REFUSAL_UNANSWERED,
        autoreview.HELD_OBJECTION_IN_FLIGHT,
    })
    for code in (autoreview.HELD_HIGH_STAKES, autoreview.HELD_PANEL_GAVE_UP,
                 autoreview.HELD_DISABLED, autoreview.HELD_EVIDENCE_SECRET,
                 autoreview.HELD_SETTLED):
        assert code not in autoreview.TRANSIENT_DROPS


def test_a_confirmation_question_nobody_is_answering_any_more_is_the_users():
    """§6: the link is set and the question is closed"""
    a = judged(confirm_question_id=920)

    spent = confirm(assumption=a, confirmation_open=False)

    assert spent.code == autoreview.HELD_CONFIRM_SPENT
    assert "920" in spent.reason and "is yours" in spent.reason


def test_a_confirmation_genuinely_in_flight_still_reads_as_the_pass_working():
    """The default keeps every existing caller on today's suppressed code"""
    a = judged(confirm_question_id=920)

    assert confirm(assumption=a).code == autoreview.HELD_CONFIRMING
    assert confirm(assumption=a, confirmation_open=True).code == autoreview.HELD_CONFIRMING


def test_a_row_neo_already_settled_is_never_told_its_confirmation_is_spent():
    """§3.2 of docs/superpowers/specs/2026-10-01-a-confirmation-is-not-re-run-on-a- settled-assumption.md:..."""
    settled = judged(status="accepted", confirm_question_id=920)
    pending = judged(confirm_question_id=920)

    assert confirm(assumption=settled,
                   confirmation_open=False).code == autoreview.HELD_SETTLED
    assert confirm(assumption=pending,
                   confirmation_open=False).code == autoreview.HELD_CONFIRM_SPENT


def test_a_dead_question_is_re_asked_rather_than_reported_as_spent():
    """§6: `unreachable_question_ids` is checked FIRST"""
    a = judged(confirm_question_id=920)

    assert confirm(assumption=a, confirmation_open=False,
                   unreachable_question_ids=(920,)).armed


def test_a_validation_round_still_open_holds_the_confirmation():
    """§5.1: the result this confirms against may be about to be sent back"""
    held = confirm(round_n=2, round_outcome="pending")

    assert held.code == autoreview.HELD_ROUND_OPEN and held.round == 2
    assert "round 2" in held.reason
    assert confirm(round_n=2, round_outcome="").code == autoreview.HELD_ROUND_OPEN


def test_a_resolved_round_and_no_round_at_all_both_arm():
    """The pair: a project with validation off has no round, and must behave exactly as it does today"""
    assert confirm(round_n=2, round_outcome="passed").armed
    assert confirm(round_n=0, round_outcome="").armed


# -- the daemon: asking the second question


EARLY_QUESTION = 99


def provisional(store, wo, *, verdict: str = "accept",
                reason: str = PROVISIONAL_REASON) -> dict:
    """Stamp §5's verdict by hand, rather than running §5's pass to produce one"""
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


DEFAULT_FILES = {"render.py": "def _render_row():\n    return 1\n"}


def with_diff(started, *, files=None, **kw):
    """A parked order whose worker worktree really has a commit in it"""
    path = started.catalog.project("proj_a").path
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    worktree = path / ".claude" / "worktrees" / "wt"
    _git(path, "worktree", "add", "-q", "-b", "wo-branch", str(worktree))
    if files is None:
        for name, body in DEFAULT_FILES.items():
            (worktree / name).write_text(body)
    else:
        files(worktree)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "the change under review")
    store, wo = park(started, auto_review=True, **kw)
    store.update_work_order(wo["id"], worktree="wt")
    return store, store.get_work_order(wo["id"])


def test_the_confirmation_carries_what_the_early_pass_never_had(started):
    """THE SECOND CALL IS STILL THE FEATURE, for the RESULT SUMMARY and the decision record (#102)"""
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert confirmation["kind"] == autoreview.QUESTION_KIND
    assert PROVISIONAL_REASON in confirmation["question"]
    assert "opened a PR" in confirmation["question"]       # the result summary
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
    """`confirming` is suppressed for `asked`'s reason: the question is filed, which is the pass WORKING"""
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)
    ask(started, store)

    assert events(store, wo["id"], "autoreview_held") == []


def test_an_early_objection_costs_no_second_call_at_the_daemon_either(started):
    """The pure hold, asserted where the money is spent"""
    store, wo = with_diff(started)
    provisional(store, wo, verdict="object")

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_OBJECTED


def test_a_historical_row_behaves_exactly_as_it_did_before_this_section(started):
    """Every new column empty: the ordinary `decide`/`propose` path"""
    store, wo = park(started, auto_review=True)

    ask(started, store)

    (q,) = questions()
    row = store.all_assumptions(wo["id"])[0]
    assert row["neo_question_id"] == q["id"] and row["confirm_question_id"] is None
    assert "CONFIRM" not in q["question"]


# -- the daemon: the outstanding-objection gate


def test_an_objection_in_flight_holds_the_pass_and_says_so(started):
    """kn-22ba6087: a guard that returns early must still record why"""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_OBJECTION_IN_FLIGHT


def test_an_objection_the_worker_actually_received_does_not_hold_anything(started):
    """DELIVERED IS NOT OUTSTANDING"""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)
    store.mark_objection_delivered(row["id"])

    ask(started, store)

    assert len(questions()) == 1
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is not None


def test_the_gate_is_a_retry_and_not_a_refusal(started):
    """"Not yet" costs the assumption nothing: the withdrawal runs, the next tick asks"""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)
    ask(started, store)

    store.withdraw_objection(row["id"])
    ask(started, store)

    assert len(questions()) == 1
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is not None


# -- the daemon: what comes back


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


def test_a_confirmation_dropped_because_the_status_flipped_is_asked_again(started):
    """THE #833 SHAPE — wo-9b70ddec, wo-3312682f, wo-672bd388"""
    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    (first,) = questions()
    store.set_status(wo["id"], "running", trigger="test")

    drain(started)

    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is None
    spent = next(q for q in questions() if q["id"] == first["id"])
    assert spent["status"] == "answered"
    assert spent["status"] not in ("escalated", "queued", "answering")
    (escalated,) = events(store, wo["id"], "autoreview_escalated")
    assert escalated["dropped"] == autoreview.HELD_STATUS

    store.set_status(wo["id"], "needs_review", trigger="test")
    ask(started, store)

    (second,) = [q for q in questions() if q["id"] != first["id"]]
    assert "opened a PR" in second["question"]          # the delivered result, again
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] == second["id"]

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "accepted" and row["decided_by"] == "neo"
    (confirmed,) = events(store, wo["id"], "autoreview_confirmed")
    assert confirmed["neo_question_id"] == second["id"]


def test_a_high_stakes_drop_keeps_its_link_and_reads_as_the_users(started):
    """§4.2: the drop says the assumption is the USER'S"""
    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    row = provisional(store, wo)
    ask(started, store)
    (first,) = questions()
    # Condition 7 is re-run against the row AS IT STANDS when the ruling lands
    store.conn.execute("UPDATE assumptions SET content=? WHERE id=?",
                       (SECRET, row["id"]))

    drain(started)

    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] == first["id"]
    assert next(q for q in questions() if q["id"] == first["id"])["status"] == "escalated"

    ask(started, store)
    ask(started, store)

    assert [q["id"] for q in questions()] == [first["id"]]
    held = events(store, wo["id"], "autoreview_held")
    assert held[-1]["code"] == autoreview.HELD_CONFIRM_SPENT
    assert str(first["id"]) in held[-1]["reason"]
    assert "not waiting on a review" not in held[-1]["reason"]
    # §8 rows 2 and 3: both surfaces show the new sentence
    enriched = ops.assumptions_with_rulings(store, wo["id"])[0]
    line = ops.assumption_ruling_line(enriched)
    assert str(first["id"]) in line and "not waiting on a review" not in line
    state = ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]
    assert str(first["id"]) in state and "not waiting on a review" not in state

    before = len(held)
    ask(started, store)
    assert len(events(store, wo["id"], "autoreview_held")) == before


def test_a_validation_round_still_open_files_no_confirmation_at_the_daemon_either(started):
    """§5: the confirmation reads the DIFF, and an open round means that diff may be about to be sent back"""
    store, wo = with_diff(started, outcome="",
                          assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_ROUND_OPEN and held["round"] == 1

    store.close_validation_round(
        store.latest_validation_round(wo_id=wo["id"])["id"], "passed", "")
    ask(started, store)

    assert len(questions()) == 1


def test_a_confirmation_nothing_flips_under_settles_on_one_ask(started):
    """THE wo-7c7347e1 CONTRAST (§1.4): the path that always worked pays nothing for any of this"""
    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    asked = events(store, wo["id"], "autoreview_asked")
    assert [e.get("confirm") for e in asked] == [True]
    codes = held_codes(store, wo["id"])
    assert autoreview.HELD_CONFIRM_SPENT not in codes
    assert autoreview.HELD_ROUND_OPEN not in codes
    assert store.all_assumptions(wo["id"])[0]["status"] == "accepted"


def test_the_parked_pass_writes_no_hold_about_the_row_it_already_accepted(started):
    """§3.1 of docs/superpowers/specs/2026-10-01-a-confirmation-is-not-re-run-on-a- settled-assumption.md:..."""
    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    # The sibling arrives after the confirmation is filed
    still_pending = store.add_assumption(wo["id"], "put the helper at the bottom")

    drain(started)

    accepted = store.all_assumptions(wo["id"])[0]
    assert accepted["status"] == "accepted" and accepted["confirm_question_id"]

    ask(started, store)

    held = events(store, wo["id"], "autoreview_held")
    assert [h for h in held if h.get("assumption_id") == accepted["id"]] == []
    asked = events(store, wo["id"], "autoreview_asked")
    assert [a["assumption_id"] for a in asked if a["assumption_id"] == still_pending]


def test_a_verdict_that_does_not_confirm_leaves_the_assumption_with_the_user(started):
    """BOTH READINGS, side by side: what Neo thought while the work ran"""
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
    """The transport failing and the model answering something else are the same fact here: nothing was..."""
    store, wo = with_diff(started, assumptions=(f"{marker} — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_confirmed") == []


def test_a_failed_call_leaves_the_confirmation_queued_for_retry(started):
    """A MODEL THAT WAS NEVER REACHED HAS MADE NO JUDGEMENT (pinned fleet learning)"""
    store, wo = with_diff(started, assumptions=(f"FORCE_FAIL — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    (confirmation,) = questions()

    drain(started)

    row = next(q for q in questions() if q["id"] == confirmation["id"])
    assert row["status"] == "queued"
    assert row["status"] not in ("failed", "answered", "escalated")
    assert not row["answer"]


def test_a_failed_call_writes_no_unconfirmed_event(started):
    """A DIFFERENT CLAIM from "nothing settled": `autoreview_unconfirmed` records that Neo looked at the..."""
    store, wo = with_diff(started, assumptions=(f"FORCE_FAIL — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    assert events(store, wo["id"], "autoreview_unconfirmed") == []


# -- the second gate: evidence the OS will not copy into a stored question

SECRET_VALUE = "sk-live-9f2b7c41d8e3a6"
SECRET_BODY = f'api_key = "{SECRET_VALUE}"\n'

#: The negative control's body, and it is the test that stops this becoming the wide net: every word...
PROSE_BODY = (
    "# the credential check runs before the production deploy path\n"
    "def _render_row(row):\n"
    '    """Delete the row from the render list, never from the database."""\n'
    "    return row\n"
)

#: A RESULT SUMMARY carrying a credential
SECRET_SUMMARY = ("wired the client to the vendor and committed the settings:\n"
                  f'api_key = "{SECRET_VALUE}"')

#: The summary net's negative control: the prose a worker on this repo actually files
PROSE_SUMMARY = ("deleted the old credential check and moved the production deploy path "
                 "behind a flag; no key or token value changed")


def held_codes(store, wo_id: str) -> list[str]:
    return [e["code"] for e in events(store, wo_id, "autoreview_held")]


def test_a_secret_shaped_result_summary_is_never_copied_into_a_stored_question(
        started):
    """THE BLOCKING FINDING"""
    store, wo = park(started, auto_review=True)
    store.update_work_order(wo["id"], result_summary=SECRET_SUMMARY)
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    assert store.all_assumptions(wo["id"])[0]["status"] == "pending"
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_EVIDENCE_SECRET
    # WHICH TEXT to go and read
    assert "result summary" in held["reason"]
    assert SECRET_VALUE not in held["reason"]


def test_a_secret_shaped_assumption_is_never_copied_into_a_stored_question(started):
    """THE OTHER TEXT THE QUESTION CARRIES (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f'wired the client:\napi_key = "{SECRET_VALUE}"',))
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    assert store.all_assumptions(wo["id"])[0]["status"] == "pending"
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_EVIDENCE_SECRET
    # WHICH TEXT: the assumption, not the summary
    assert "assumption" in held["reason"] and "result summary" not in held["reason"]
    assert SECRET_VALUE not in held["reason"]


def test_an_ordinary_result_summary_still_arms_and_still_asks(started):
    """THE NEGATIVE CONTROL, and without it a net that holds everything passes"""
    store, wo = park(started, auto_review=True)
    store.update_work_order(wo["id"], result_summary=PROSE_SUMMARY)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert "production deploy path" in confirmation["question"]
    assert autoreview.HELD_EVIDENCE_SECRET not in held_codes(store, wo["id"])


def test_the_evidence_hold_is_written_once_however_many_ticks_run(started):
    """`decide_evidence` carries `assumption_id` and `n` so `_note_autoreview_held` dedupes per assumption"""
    store, wo = park(started, auto_review=True)
    store.update_work_order(wo["id"], result_summary=SECRET_SUMMARY)
    provisional(store, wo)

    ask(started, store)
    first = [c for c in held_codes(store, wo["id"])
             if c == autoreview.HELD_EVIDENCE_SECRET]
    ask(started, store)
    second = [c for c in held_codes(store, wo["id"])
              if c == autoreview.HELD_EVIDENCE_SECRET]

    assert len(first) == 1 and len(second) == 1


def test_the_objection_hold_is_written_once_however_many_ticks_run(started):
    """The same claim for the other hold that is NOT suppressed"""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)

    ask(started, store)
    first = [c for c in held_codes(store, wo["id"])
             if c == autoreview.HELD_OBJECTION_IN_FLIGHT]
    ask(started, store)
    second = [c for c in held_codes(store, wo["id"])
              if c == autoreview.HELD_OBJECTION_IN_FLIGHT]

    assert len(first) == 1 and len(second) == 1


# -- `secret_marker`: pure, and it never repeats the secret


@pytest.mark.parametrize("line", [
    '+api_key = "sk-live-9f2b7c41d8e3a6"',
    # THE COMMONEST ADDED CREDENTIAL: a JSON or Python-dict line
    '+  "api_key": "sk-live-9f2b7c41d8e3a6",',
    '+  "Authorization": "Bearer sk-live-9f2b7c41d8e3a6"',
    "+AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI7K9bPxRfiCY1EXAMPLEKEY2",
    '+    self.client_secret = "7f3a9b2c5d8e1f4a6b0c"',
    "+export GITHUB_TOKEN=ghp_9aF3kLm2Qr7Xz1Bv6Nt0",
    "+-----BEGIN RSA PRIVATE KEY-----",
    "+ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC9x2 user@host",
    "+Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.aGk",
    "+Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
])
def test_secret_shaped_added_lines_fire(line):
    assert autoreview.secret_marker("", line)


@pytest.mark.parametrize("line", [
    '+api_key = ""',
    # The quoted name is widened for; the VALUE test is not
    '+  "api_key": "",',
    '+  "api_key": "changeme",',
    "+api_key = ''",
    "+api_key = None",
    "+api_key: str",
    '+password = "changeme"',
    '+client_secret = "<your-secret-here>"',
    '+api_key = "${API_KEY}"',
    '+api_key = os.environ["API_KEY"]',
    '+token = os.getenv("TOKEN")',
    '+api_key = "REDACTED"',
    '+api_key = "example"',
    # This module's own code, which names secrets for a living
    '+HELD_EVIDENCE_SECRET = "evidence_secret"',
    '+    r"credential|secret|password|\\bapi[ -]?key\\b"',
    "+    marker = high_stakes_marker(text)",
    "+# the api key is read from the environment, never committed",
])
def test_placeholders_and_prose_do_not_fire(line):
    assert autoreview.secret_marker("", line) == ""


def test_a_removed_secret_line_does_not_fire():
    """A removed secret is the change doing the right thing"""
    assert autoreview.secret_marker("", f'-api_key = "{SECRET_VALUE}"') == ""


def test_a_diff_header_is_not_read_as_an_added_line():
    """`+++ b/path` starts with `+` and is not a line the diff adds"""
    assert autoreview.secret_marker("", f'+++ b/api_token = "{SECRET_VALUE}"') == ""


def test_the_marker_never_carries_the_secret_itself():
    """IT IS RENDERED on the timeline and on `jarvis wo show`"""
    marker = autoreview.secret_marker("", f'+api_key = "{SECRET_VALUE}"')
    assert marker and SECRET_VALUE not in marker
    assert not any(c in marker for c in ("9f2b", "sk-live"))


@pytest.mark.parametrize("path", [
    " .env | 2 +-",
    " config/.env.production | 2 +-",
    " certs/server.pem | 2 +-",
    " certs/server.key | 2 +-",
    " keystore.p12 | 2 +-",
    " keystore.pfx | 2 +-",
    " deploy/id_rsa | 2 +-",
    " deploy/id_ed25519 | 2 +-",
    " infra/.aws/credentials | 2 +-",
    " home/.netrc | 2 +-",
    " home/.npmrc | 2 +-",
    " app/secrets.yaml | 2 +-",
    " gcp-service-account-prod.json | 2 +-",
])
def test_secret_shaped_paths_fire_from_the_stat(path):
    assert autoreview.secret_marker(path, "")


@pytest.mark.parametrize("path", [
    " src/jarvis/autoreview.py | 40 ++++",
    " tests/test_autoreview_confirm.py | 12 +-",
    " docs/keyboard-shortcuts.md | 3 +-",
    " src/jarvis/ui/templates/_question.html | 2 +-",
])
def test_this_repos_own_paths_do_not_fire(path):
    assert autoreview.secret_marker(path, "") == ""


def test_a_secret_shaped_path_in_a_diff_header_fires_too():
    assert ".env" in autoreview.secret_marker("", "diff --git a/.env b/.env\n")


# -- `decide_evidence`: the hold it returns


def test_decide_evidence_arms_on_ordinary_prose():
    d = autoreview.decide_evidence(judged(), summary=PROSE_SUMMARY)
    assert d.armed and d.assumption_id == 3 and d.n == 2


def test_decide_evidence_reads_the_summary_as_plain_text():
    """No `+` prefix and no diff: the summary is prose the worker typed, and the same line scanner reads it"""
    d = autoreview.decide_evidence(judged(), summary=SECRET_SUMMARY)
    assert d.code == autoreview.HELD_EVIDENCE_SECRET
    assert "result summary" in d.reason and SECRET_VALUE not in d.reason


def test_decide_evidence_reads_the_assumptions_own_sentence_too():
    """The second text the question carries, and the one newly scanned (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)"""
    d = autoreview.decide_evidence(
        judged(content=f'wired it:\napi_key = "{SECRET_VALUE}"'),
        summary=PROSE_SUMMARY)
    assert d.code == autoreview.HELD_EVIDENCE_SECRET
    assert "assumption" in d.reason and SECRET_VALUE not in d.reason


def test_the_summary_outranks_the_assumption_so_the_wording_stays_tested():
    """Precedence is unchanged: the summary branch is first"""
    d = autoreview.decide_evidence(
        judged(content=f'wired it:\napi_key = "{SECRET_VALUE}"'),
        summary=SECRET_SUMMARY)
    assert "result summary" in d.reason


def test_the_confirmation_packet_carries_the_decision_record_too():
    """Test 7's mirror (#832)"""
    from tests.test_autoreview import RECORD_HEADER, FakeNeo, FakeStore, qrow

    neo_ = FakeNeo(qrow(answered_by="user", answer="build NO kill remedy"))

    autoreview.propose_confirmation(
        FakeStore(), neo_, "p", WO, judged(), [judged()])

    (packet,) = neo_.asked
    assert RECORD_HEADER in packet
    assert "Q887" in packet and "answered by user" in packet
    assert packet.index("do not rule on these") < packet.index(RECORD_HEADER)
    assert packet.index(RECORD_HEADER) < packet.index("You are CONFIRMING")


def test_the_confirmation_packet_says_when_there_is_no_record():
    """The empty case on this pass too: `record=""` renders the section, never omits it"""
    packet = autoreview._confirm_question("p", WO, judged(), [])   # noqa: SLF001

    assert "(no prior decisions recorded)" in packet


def test_decide_evidence_holds_and_carries_the_row_it_is_about():
    """`assumption_id` and `n` so `_note_autoreview_held` dedupes per assumption"""
    d = autoreview.decide_evidence(judged(), summary=SECRET_SUMMARY)
    assert d.code == autoreview.HELD_EVIDENCE_SECRET
    assert d.assumption_id == 3 and d.n == 2
    assert SECRET_VALUE not in d.reason


# -- the question the store refuses to hold

#: Planted in the text that makes the question oversized (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
DIFF_MARKER = "PASTED-DIFF-MARKER-7b2d"


def _huge_assumption() -> str:
    """An assumption sentence alone past `claude_cli.MAX_OS_PROMPT_CHARS` (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md §2)"""
    from jarvis import claude_cli

    line = f"renamed the render helper, noted as {DIFF_MARKER}, for consistency\n"
    return line * (claude_cli.MAX_OS_PROMPT_CHARS // len(line) + 500)


def _refusals(store, wo_id: str) -> list[dict]:
    from jarvis.project_store import OS_PROMPT_REFUSED_EVENT

    return events(store, wo_id, OS_PROMPT_REFUSED_EVENT)


def _refusal_inbox(wo_id: str) -> list[dict]:
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        return [r for r in central.unacked_inbox()
                if r["wo_id"] == wo_id and "too large" in r["title"]]
    finally:
        central.close()


def _oversized_confirmation(started, monkeypatch):
    """A parked order whose confirmation question is past the ceiling"""
    monkeypatch.setattr(ops, "_concision_cap", lambda *_: 0)  # stored before the cap existed
    store, wo = park(started, auto_review=True,
                     assumptions=(_huge_assumption(),))
    provisional(store, wo)

    ask(started, store)
    ask(started, store)
    return store, wo


def test_a_confirmation_too_large_to_store_does_not_take_the_tick_down(
        started, monkeypatch):
    """`neo_store.ask` refuses before it persists"""
    store, wo = _oversized_confirmation(started, monkeypatch)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending"
    assert row["confirm_question_id"] is None
    assert questions() == [], "an oversized question was persisted after all"


def test_the_confirmation_refusal_is_written_down_exactly_once(started, monkeypatch):
    """THE ONCE-GUARD"""
    store, wo = _oversized_confirmation(started, monkeypatch)

    assert len(_refusals(store, wo["id"])) == 1
    rows = _refusal_inbox(wo["id"])
    assert len(rows) == 1 and rows[0]["level"] == "critical"


def test_the_confirmation_refusal_repeats_no_byte_of_the_question(started, monkeypatch):
    """NUMBERS AND IDENTIFIERS ONLY, on every surface the user reads"""
    store, wo = _oversized_confirmation(started, monkeypatch)

    (said,) = _refusals(store, wo["id"])
    assert said["total"] > said["ceiling"], (
        "nothing was over the ceiling, so the rule below is vacuous")
    (row,) = _refusal_inbox(wo["id"])
    assert "os.max_os_prompt_chars" in row["body"]
    assert DIFF_MARKER not in row["title"]
    assert DIFF_MARKER not in row["body"]
    fresh = store.get_work_order(wo["id"])
    assert DIFF_MARKER not in str(fresh["attention_reason"])
    assert "os.max_os_prompt_chars" in str(fresh["attention_reason"])


# -- NO DIFF TRAVELS, and the live case that proves the hold is gone


#: Planted in the body of the change, so "no added line travels" is not vacuous
ADDED_MARKER = "ADDED-LINE-MARKER-4c91"


def big_change(worktree, files: int = 40, lines: int = 200) -> None:
    """A delivery far past any budget the confirmation ever had, really committed"""
    for i in range(files):
        (worktree / f"mod{i:02d}.py").write_text(
            "".join(f"def f{i}_{j}():  # {ADDED_MARKER}\n    return {j}\n"
                    for j in range(lines)))


def test_a_confirmation_question_carries_no_diff_whatever_the_change_size(started):
    """THE WHOLE CHANGE, asserted on the RENDERED question and not on a flag"""
    store, wo = with_diff(started, files=big_change)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    text = confirmation["question"]
    assert "diff --git" not in text
    assert "@@" not in text
    assert ADDED_MARKER not in text
    assert "# What changed" not in text
    assert "file changed" not in text and "files changed" not in text
    assert "diff truncated" not in text
    assert "not shown]" not in text
    # ...and the texts that DO still travel are there, so this is not an empty question
    assert PROVISIONAL_REASON in text
    assert "opened a PR" in text                      # the result summary
    assert "the validation panel" in text


def test_a_change_that_is_only_a_huge_document_is_confirmed_rather_than_held(started):
    """THE LIVE CASE, wo-09c60d79: the only changed file was a ~20,000-char spec document (#102)"""
    spec_body = "spec line that says something about the change\n" * 420
    assert len(spec_body) > 19_000, "not the live case: the document is too small"

    def only_a_doc(worktree):
        (worktree / "docs").mkdir(parents=True, exist_ok=True)
        (worktree / "docs" / "spec.md").write_text(spec_body)

    store, wo = with_diff(
        started, files=only_a_doc,
        assumptions=("wrote the wording of the new spec document myself",))
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert confirmation["kind"] == autoreview.QUESTION_KIND
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] == confirmation["id"]
    assert held_codes(store, wo["id"]) == []
    assert "spec line that says something" not in confirmation["question"]
