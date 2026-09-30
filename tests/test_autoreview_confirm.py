"""The confirmation pass: a provisional verdict only settles once a diff exists.

docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §7.

Four properties carry the section, and each gets its own block:

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
* **THE EVIDENCE IS GATED BEFORE IT IS STORED.** The confirmation question is the first
  one carrying a diff and a result summary, and `neo.ask` PERSISTS it. Both texts go
  through `decide_evidence`, and a hit files no question at all.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from jarvis import autoreview, daemon as daemon_mod, evidence, ops
from jarvis.daemon import Daemon
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


def test_a_confirmation_nobody_will_ever_answer_is_asked_again():
    """The confirm pass is guarded by its own column, so without this it holds for ever on
    a dead confirmation (2026-09-26-an-unreachable-neo-question… §4)."""
    a = judged(confirm_question_id=77)

    assert confirm(assumption=a, unreachable_question_ids=(77,)).armed
    assert confirm(assumption=a).code == autoreview.HELD_CONFIRMING
    assert confirm(assumption=a,
                   unreachable_question_ids=(9,)).code == autoreview.HELD_CONFIRMING


def test_the_set_reaches_the_conditions_decide_owns_too():
    """It is forwarded, not consumed: a dead EARLY link on a row being confirmed must not
    hold in `decide`'s condition 6 either."""
    a = judged(confirm_question_id=77, neo_question_id=41)

    assert confirm(assumption=a, unreachable_question_ids=(77, 41)).armed


# -- a dropped confirmation must not hold an assumption for ever -----------------------
#
# docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-an-assumption-
# for-ever.md. §4 clears the link at the settle site on a TRANSIENT drop, §5 holds the
# pass while a round is open, §6 splits `confirming` from `confirm_spent`.


def test_the_transient_drop_list_is_an_allowlist_of_exactly_three_codes():
    """§4.1, asserted WHOLE so a fourth code is a visible edit rather than an accident.

    The two rejected candidates are named too: `settled` has no reader (the drop site
    returns earlier) and `evidence_secret` says the assumption is the user's."""
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
    """§6: the link is set and the question is closed, so nothing is in flight and the
    hold has to say so — with the question id IN THE TEXT (§8 row 9)."""
    a = judged(confirm_question_id=920)

    spent = confirm(assumption=a, confirmation_open=False)

    assert spent.code == autoreview.HELD_CONFIRM_SPENT
    assert "920" in spent.reason and "is yours" in spent.reason


def test_a_confirmation_genuinely_in_flight_still_reads_as_the_pass_working():
    """The default keeps every existing caller on today's suppressed code."""
    a = judged(confirm_question_id=920)

    assert confirm(assumption=a).code == autoreview.HELD_CONFIRMING
    assert confirm(assumption=a, confirmation_open=True).code == autoreview.HELD_CONFIRMING


def test_a_dead_question_is_re_asked_rather_than_reported_as_spent():
    """§6: `unreachable_question_ids` is checked FIRST — a `failed` question is an outage
    (2026-09-26 spec §4), not a decision handed back."""
    a = judged(confirm_question_id=920)

    assert confirm(assumption=a, confirmation_open=False,
                   unreachable_question_ids=(920,)).armed


def test_a_validation_round_still_open_holds_the_confirmation():
    """§5.1: the result this confirms against may be about to be sent back. The round
    travels on the decision, because it is part of the dedupe key (§5.3)."""
    held = confirm(round_n=2, round_outcome="pending")

    assert held.code == autoreview.HELD_ROUND_OPEN and held.round == 2
    assert "round 2" in held.reason
    assert confirm(round_n=2, round_outcome="").code == autoreview.HELD_ROUND_OPEN


def test_a_resolved_round_and_no_round_at_all_both_arm():
    """The pair: a project with validation off has no round, and must behave exactly as
    it does today."""
    assert confirm(round_n=2, round_outcome="passed").armed
    assert confirm(round_n=0, round_outcome="").armed


# -- the daemon: asking the second question --------------------------------------------


EARLY_QUESTION = 99


def provisional(store, wo, *, verdict: str = "accept",
                reason: str = PROVISIONAL_REASON) -> dict:
    """Stamp §5's verdict by hand, rather than running §5's pass to produce one.

    §5 has landed, and driving it here would make every test below depend on what the
    early pass happens to decide — this section is about what the CONFIRMATION does with
    a verdict that already exists.

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


DEFAULT_FILES = {"render.py": "def _render_row():\n    return 1\n"}


def with_diff(started, **kw):
    """A parked order whose worker worktree really has a commit in it.

    `park` alone leaves the repository empty, and `evidence.collect_work_order` answers
    an empty packet for one — which this section ASKS on anyway. So the diff has to be
    real here or the end-to-end test would pass with the evidence never collected at all.

    The diff it collects is not this fixture's to promise: tests about question TEXT
    state theirs through `stub_evidence` instead.
    """
    path = started.catalog.project("proj_a").path
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    worktree = path / ".claude" / "worktrees" / "wt"
    _git(path, "worktree", "add", "-q", "-b", "wo-branch", str(worktree))
    for name, body in DEFAULT_FILES.items():
        (worktree / name).write_text(body)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "the change under review")
    store, wo = park(started, auto_review=True, **kw)
    store.update_work_order(wo["id"], worktree="wt")
    return store, store.get_work_order(wo["id"])


def packet_of(stat: str, diff: str, *, pr_url: str = PR,
              source: str = "pull_request", head: str = "wo-branch",
              diff_truncated: bool = False,
              dropped_files: tuple[str, ...] = ()) -> evidence.EvidencePacket:
    """A real packet over a stated diff — `files` read off the `diff --git` headers."""
    files = tuple(new or old for new, old, _ in evidence._sections(diff) if new or old)
    return evidence.EvidencePacket(
        unit="work_order", subject_id="wo-1", title="t", description="d", summary="s",
        declared="", pr_url=pr_url, base="main", head=head, stat=stat, files=files,
        diff=diff, diff_truncated=diff_truncated, dropped_files=dropped_files,
        diff_sha="sha", source=source)


def stub_evidence(monkeypatch, stat: str, diff: str, **packet) -> None:
    """Drive the pass through a chosen `(stat, diff)`, the seam that method exists for.

    A test about what the question CONTAINS should state the diff it means. Deriving it
    from a fixture repository put the environment in the assertion once already: before
    `make_git_project` pinned `-b main`, a machine defaulting to `master` matched no rung
    of `evidence.base_ref`'s ladder and collected an empty diff, and four tests here
    failed on CI only. The pin fixed that; stating the diff keeps these tests out of the
    question.
    """
    monkeypatch.setattr(Daemon, "_confirmation_evidence",
                        lambda self, project, wo: packet_of(stat, diff, **packet))


def added(path: str, body: str) -> tuple[str, str]:
    """`(stat, diff)` for one file added whole — the shape git prints, headers included.

    The headers are not decoration: `secret_marker` reads paths off the stat AND off
    `diff --git`, and must never read `+++ b/…` as an added line.
    """
    lines = body.splitlines()
    n = len(lines)
    stat = f" {path} | {n} +\n {n} file changed, {n} insertions(+)\n"
    diff = (f"diff --git a/{path} b/{path}\n"
            f"new file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{n} @@\n" + "".join(f"+{line}\n" for line in lines))
    return stat, diff


def test_the_confirmation_carries_the_diff_the_early_pass_never_had(started, monkeypatch):
    """THE SECOND CALL IS THE FEATURE (Neo, question 549). The cheap design — confirm in
    code, re-ask only if something changed — was refused: a mid-turn verdict never had a
    result summary, so "something changed" is always true."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("render.py", DEFAULT_FILES["render.py"]))
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


def test_the_real_collector_still_reaches_the_question(started):
    """THE END-TO-END ONE, through `evidence.collect_work_order` and a real worktree —
    without it every claim above holds over a stub only.

    It asserts what is true in EVERY git environment: the collected diff itself is not,
    because the base the collector resolves depends on the machine's default branch.
    """
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert confirmation["kind"] == autoreview.QUESTION_KIND
    assert PROVISIONAL_REASON in confirmation["question"]
    assert "opened a PR" in confirmation["question"]       # the result summary
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] == confirmation["id"]


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


def test_a_confirmation_dropped_because_the_status_flipped_is_asked_again(started):
    """THE #833 SHAPE — wo-9b70ddec, wo-3312682f, wo-672bd388. The panel rejected the
    round 19 seconds after the confirmation was filed, the settle site dropped the ruling
    on `status`, and the link it left behind held the assumption for ever (§1.1).

    §4: a transient drop clears the link and leaves the spent question CLOSED — never
    `escalated`, which is what keeps it off `/neo` once nothing can resolve it back to an
    assumption (§4.3) — and the next parked tick confirms against the FINAL delivered
    diff. `neo.drain_queue` has already recorded Neo's real answer by the time the settle
    site re-checks, so `supersede`'s open-status guard leaves that verdict alone: the
    claim here is the STATUS, not who is credited with the answer.
    """
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
    """§4.2: the drop says the assumption is the USER'S, so re-asking would lobby them
    once per reconcile tick. The link stays, the question stays `escalated`, and §6's
    visible hold is what stops the record showing the stale settle-site sentence."""
    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    row = provisional(store, wo)
    ask(started, store)
    (first,) = questions()
    # Condition 7 is re-run against the row AS IT STANDS when the ruling lands.
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
    # §8 rows 2 and 3: both surfaces show the new sentence, and the question id is in the
    # prose because a hold payload carries no `neo_question_id` for the template to link.
    enriched = ops.assumptions_with_rulings(store, wo["id"])[0]
    line = ops.assumption_ruling_line(enriched)
    assert str(first["id"]) in line and "not waiting on a review" not in line
    state = ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]
    assert str(first["id"]) in state and "not waiting on a review" not in state

    before = len(held)
    ask(started, store)
    assert len(events(store, wo["id"], "autoreview_held")) == before


def test_a_validation_round_still_open_files_no_confirmation_at_the_daemon_either(started):
    """§5: the confirmation reads the DIFF, and an open round means that diff may be about
    to be sent back. Recorded rather than suppressed (§5.3), and asked once it resolves."""
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
    """THE wo-7c7347e1 CONTRAST (§1.4): the path that always worked pays nothing for any
    of this — one confirmation question, no hold of either new code."""
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
    here: nothing was confirmed, so nothing settles.

    The FORCE_FAIL half has two claims of its own, one test each below: the question
    stays RETRYABLE, and no `autoreview_unconfirmed` event is written.
    """
    store, wo = with_diff(started, assumptions=(f"{marker} — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_confirmed") == []


def test_a_failed_call_leaves_the_confirmation_queued_for_retry(started):
    """A MODEL THAT WAS NEVER REACHED HAS MADE NO JUDGEMENT (pinned fleet learning).

    `neo.drain_queue` hands a crashed call back through `NeoStore.release_claim`, which
    requeues it with `attempts` incremented and only writes `failed` once the retries are
    spent. Pinned here because the failure mode it replaces — `failed` at `attempts=0`
    with an escalation synthesised from the crash — reached the user as a ruling Neo
    never made (question 388), and this pass files the questions it would happen to.
    """
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
    """A DIFFERENT CLAIM from "nothing settled": `autoreview_unconfirmed` records that
    Neo looked at the delivered result and would not confirm its own earlier reading. A
    call that never happened produced no such reading, and writing the event would put
    that judgement on the record in Neo's name."""
    store, wo = with_diff(started, assumptions=(f"FORCE_FAIL — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    assert events(store, wo["id"], "autoreview_unconfirmed") == []


# -- the second gate: evidence the OS will not copy into a stored question -------------
#
# `decide_confirm` is pure over a ROW and cannot see the evidence. This is the gate over
# it, and it exists because `neo.ask` PERSISTS the question text: a diff that adds a
# credential — or a result summary that quotes one — would land in the question store and
# on `/neo` and `jarvis neo list`, surfaces the ask pass never put delivered content on
# (kn-deef42ea — a redaction decision is also a filing decision). Neo, question 593,
# chose this narrow net over running `high_stakes_marker` on the diff.

SECRET_VALUE = "sk-live-9f2b7c41d8e3a6"
SECRET_BODY = f'api_key = "{SECRET_VALUE}"\n'

#: The negative control's body, and it is the test that stops this becoming the wide
#: net: every word here is this repo's daily vocabulary.
PROSE_BODY = (
    "# the credential check runs before the production deploy path\n"
    "def _render_row(row):\n"
    '    """Delete the row from the render list, never from the database."""\n'
    "    return row\n"
)

#: A RESULT SUMMARY carrying a credential — plain text, no `+` prefix, because nothing
#: strips a diff marker off the worker's own prose.
SECRET_SUMMARY = ("wired the client to the vendor and committed the settings:\n"
                  f'api_key = "{SECRET_VALUE}"')

#: The summary net's negative control: the prose a worker on this repo actually files.
PROSE_SUMMARY = ("deleted the old credential check and moved the production deploy path "
                 "behind a flag; no key or token value changed")


def held_codes(store, wo_id: str) -> list[str]:
    return [e["code"] for e in events(store, wo_id, "autoreview_held")]


def test_a_secret_shaped_added_line_is_never_copied_into_a_stored_question(
        started, monkeypatch):
    """THE BLOCKING FINDING. No question is filed at all — the assumption stays pending
    and is the user's, exactly as it is without this feature. Filing the question with
    the diff withheld is the cheap design Neo refused in question 549."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("settings.py", SECRET_BODY))
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    assert all(SECRET_VALUE not in q["question"] for q in questions())
    assert store.all_assumptions(wo["id"])[0]["status"] == "pending"
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_EVIDENCE_SECRET
    assert SECRET_VALUE not in held["reason"]


def test_a_secret_shaped_path_holds_it_whatever_the_body_says(started, monkeypatch):
    """The path is evidence on its own: a work order that adds a `.env` is one whose
    diff the OS will not store, and reading the file to find out would be the same
    mistake one layer down."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added(".env", "GREETING=hello\n"))
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_EVIDENCE_SECRET
    assert ".env" in held["reason"]


def test_this_repos_own_everyday_diff_still_arms_and_still_asks(started, monkeypatch):
    """THE NEGATIVE CONTROL, and without it a net that holds everything passes. Running
    `high_stakes_marker` over a diff was REJECTED (Neo, question 593) for exactly this:
    "credential", "production" and "delete" appear in nearly every change this repo
    makes, so that net would hold almost every confirmation and switch the pass off
    silently."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("render.py", PROSE_BODY))
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert "render.py" in confirmation["question"]
    assert autoreview.HELD_EVIDENCE_SECRET not in held_codes(store, wo["id"])


def test_a_secret_shaped_result_summary_is_never_copied_into_a_stored_question(
        started, monkeypatch):
    """THE SECOND BLOCKING FINDING: the diff is not the only text the question carries.

    `_confirm_question` interpolates `result_summary` raw, and the worker wrote that
    text — a summary quoting the credential it wired up reaches the question store by
    exactly the route the diff was gated on.
    """
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("render.py", PROSE_BODY))
    store.update_work_order(wo["id"], result_summary=SECRET_SUMMARY)
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    assert all(SECRET_VALUE not in q["question"] for q in questions())
    assert store.all_assumptions(wo["id"])[0]["status"] == "pending"
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_EVIDENCE_SECRET
    # WHICH TEXT to go and read: the summary, not the diff.
    assert "result summary" in held["reason"] and "diff" not in held["reason"]
    assert SECRET_VALUE not in held["reason"]


def test_an_ordinary_result_summary_still_arms_and_still_asks(started, monkeypatch):
    """THE NEGATIVE CONTROL for the summary net, and without it this is the wide net by
    another route: "credential", "production" and "delete" are what a worker on this
    repo writes in every summary it files."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("render.py", PROSE_BODY))
    store.update_work_order(wo["id"], result_summary=PROSE_SUMMARY)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert "production deploy path" in confirmation["question"]
    assert autoreview.HELD_EVIDENCE_SECRET not in held_codes(store, wo["id"])


def test_the_evidence_hold_is_written_once_however_many_ticks_run(started, monkeypatch):
    """`decide_evidence` carries `assumption_id` and `n` so `_note_autoreview_held`
    dedupes per assumption. The reconciler runs this pass every tick, so without the
    dedupe one held work order writes a timeline line a minute."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("settings.py", SECRET_BODY))
    provisional(store, wo)

    ask(started, store)
    first = [c for c in held_codes(store, wo["id"])
             if c == autoreview.HELD_EVIDENCE_SECRET]
    ask(started, store)
    second = [c for c in held_codes(store, wo["id"])
              if c == autoreview.HELD_EVIDENCE_SECRET]

    assert len(first) == 1 and len(second) == 1


def test_the_objection_hold_is_written_once_however_many_ticks_run(started):
    """The same claim for the other hold that is NOT suppressed. An objection sits in
    flight until the worker reads it, which is many ticks."""
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


# -- `secret_marker`: pure, and it never repeats the secret ----------------------------


@pytest.mark.parametrize("line", [
    '+api_key = "sk-live-9f2b7c41d8e3a6"',
    # THE COMMONEST ADDED CREDENTIAL: a JSON or Python-dict line, where the name is
    # quoted and the separator is a colon.
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
    # The quoted name is widened for; the VALUE test is not.
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
    # This module's own code, which names secrets for a living.
    '+HELD_EVIDENCE_SECRET = "evidence_secret"',
    '+    r"credential|secret|password|\\bapi[ -]?key\\b"',
    "+    marker = high_stakes_marker(text)",
    "+# the api key is read from the environment, never committed",
])
def test_placeholders_and_prose_do_not_fire(line):
    assert autoreview.secret_marker("", line) == ""


def test_a_removed_secret_line_does_not_fire():
    """A removed secret is the change doing the right thing, and holding the pass on it
    would punish the only diff that fixes one."""
    assert autoreview.secret_marker("", f'-api_key = "{SECRET_VALUE}"') == ""


def test_a_diff_header_is_not_read_as_an_added_line():
    """`+++ b/path` starts with `+` and is not a line the diff adds — so the assignment
    net must not read one, whatever the rest of the header says."""
    assert autoreview.secret_marker("", f'+++ b/api_token = "{SECRET_VALUE}"') == ""


def test_the_marker_never_carries_the_secret_itself():
    """IT IS RENDERED on the timeline and on `jarvis wo show`. A marker quoting the
    value would move the secret from the question store to the event store."""
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


# -- `decide_evidence`: the hold it returns --------------------------------------------


def test_decide_evidence_arms_on_an_ordinary_diff():
    d = autoreview.decide_evidence(judged(), " render.py | 2 +-", "+    return 1\n")
    assert d.armed and d.assumption_id == 3 and d.n == 2


def test_decide_evidence_reads_the_summary_as_plain_text():
    """No `+` prefix and no diff: the summary is prose the worker typed, and the same
    line scanner reads it."""
    d = autoreview.decide_evidence(judged(), "", "+    return 1\n",
                                   summary=SECRET_SUMMARY)
    assert d.code == autoreview.HELD_EVIDENCE_SECRET
    assert "result summary" in d.reason and SECRET_VALUE not in d.reason


def test_decide_evidence_arms_on_an_ordinary_summary():
    d = autoreview.decide_evidence(judged(), " render.py | 2 +-", "+    return 1\n",
                                   summary=PROSE_SUMMARY)
    assert d.armed


def test_decide_evidence_holds_and_carries_the_row_it_is_about():
    """`assumption_id` and `n` so `_note_autoreview_held` dedupes per assumption, as
    every other hold does — otherwise one work order records this every tick."""
    d = autoreview.decide_evidence(judged(), "", f'+api_key = "{SECRET_VALUE}"')
    assert d.code == autoreview.HELD_EVIDENCE_SECRET
    assert d.assumption_id == 3 and d.n == 2
    assert SECRET_VALUE not in d.reason


# -- the confirmation question's own diff budget (spec § 2) -----------------------------

#: One file's section, and every file below is this size: equal-length names make the
#: budget arithmetic in these tests exact rather than approximate.
MOD_BODY = "def f():\n    return 1\n"
MOD_SECTION = len(added("mod00.py", MOD_BODY)[1])


def mods(n: int) -> tuple[str, str]:
    """`(stat, diff)` for `n` equal-sized added files, `mod00.py` upward."""
    pairs = [added(f"mod{i:02d}.py", MOD_BODY) for i in range(n)]
    return "".join(p[0] for p in pairs), "".join(p[1] for p in pairs)


def budget(started, chars: int) -> None:
    """The project's `validation.confirm_diff_chars`, which is what the pass trims to."""
    started.catalog.project("proj_a").validation.confirm_diff_chars = chars


def build(stat: str, diff: str, limit: int, *, content: str = ROUTINE,
          **packet) -> autoreview.ConfirmEvidence:
    return autoreview.confirm_evidence(
        packet_of(stat, diff, **packet), assumption(content=content), limit,
        collect_limit=daemon_mod.CONFIRM_COLLECT_CHARS)


def test_a_diff_inside_the_budget_is_carried_whole_and_claims_no_truncation(
        started, monkeypatch):
    """The pairing that stops an unconditionally appended marker passing."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("render.py", DEFAULT_FILES["render.py"]))
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert "def _render_row():" in confirmation["question"]
    assert "diff truncated" not in confirmation["question"]


def test_a_diff_over_the_budget_is_cut_at_a_file_boundary_and_says_what_is_missing(
        started, monkeypatch):
    """The marker names the kept size, the exact total, the exact count and the names."""
    stat, diff = mods(5)
    ce = build(stat, diff, 2 * MOD_SECTION)
    assert ce.diff_truncated and len(ce.diff) == 2 * MOD_SECTION
    assert ce.full_chars == 5 * MOD_SECTION
    assert ce.dropped_files == ("mod02.py", "mod03.py", "mod04.py")
    assert ce.diff.endswith("+    return 1\n") and "mod02.py" not in ce.diff

    store, wo = park(started, auto_review=True)
    budget(started, 2 * MOD_SECTION)
    stub_evidence(monkeypatch, stat, diff)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert (f"[diff truncated — {2 * MOD_SECTION:,} of {5 * MOD_SECTION:,} chars; "
            f"3 file(s) not shown: mod02.py, mod03.py, mod04.py; "
            f"full diff: {PR}]") in confirmation["question"]
    assert confirmation["question"].count("diff --git") == 2
    assert ("Escalate rather than judge on what you cannot see."
            in confirmation["question"])


def test_the_stat_and_the_file_list_survive_a_budget_that_keeps_no_hunk(
        started, monkeypatch):
    """evidence.py's rule 3: `files` is never truncated at any limit."""
    stat, diff = mods(5)
    store, wo = park(started, auto_review=True)
    budget(started, 1)
    stub_evidence(monkeypatch, stat, diff)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert confirmation["question"].count("diff --git") == 0
    for i in range(5):
        assert f"mod{i:02d}.py" in confirmation["question"]
    assert (f"[diff truncated — 0 of {5 * MOD_SECTION:,} chars; "
            f"5 file(s) not shown: ") in confirmation["question"]


def test_the_reference_rides_on_an_untruncated_question_too(started, monkeypatch):
    """Neo, question 753: truncated or not, the question carries the reference."""
    store, wo = park(started, auto_review=True)
    stub_evidence(monkeypatch, *added("render.py", DEFAULT_FILES["render.py"]))
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert "diff truncated" not in confirmation["question"]
    assert f"pull request: {PR}" in confirmation["question"]
    assert "head branch: wo-branch" in confirmation["question"]


def test_a_sha_is_labelled_a_sha_and_a_branch_a_branch(started):
    """The false-reference trap: `head` means different things per `source`."""
    stat, diff = added("render.py", DEFAULT_FILES["render.py"])
    tree = build(stat, diff, MOD_SECTION * 9, source="worktree", head="a" * 40)
    pr = build(stat, diff, MOD_SECTION * 9, source="pull_request", head="wo-branch")

    from_tree = autoreview._confirm_question("proj_a", WO, judged(), [], tree)
    from_pr = autoreview._confirm_question("proj_a", WO, judged(), [], pr)

    assert f"head sha: {'a' * 40}" in from_tree and "head branch" not in from_tree
    assert "head branch: wo-branch" in from_pr and "head sha" not in from_pr


def test_a_file_the_assumption_names_is_kept_even_when_diff_order_would_drop_it():
    """Prefer the hunks that matter: the named file moves to the front of the budget."""
    stat, diff = mods(5)
    ce = build(stat, diff, MOD_SECTION, content="renamed the helper in mod04.py")

    assert "diff --git a/mod04.py" in ce.diff
    assert len(ce.diff) == MOD_SECTION
    assert set(ce.dropped_files) == {f"mod{i:02d}.py" for i in range(4)}
    assert build(stat, diff, MOD_SECTION).dropped_files[0] == "mod01.py"


def test_a_collection_that_was_itself_truncated_never_states_a_false_total():
    """Neo, question 945: the total is unknown, so the marker says `more than`."""
    stat, diff = mods(5)
    ce = build(stat, diff, 2 * MOD_SECTION, diff_truncated=True,
               dropped_files=("later.py",))
    text = autoreview._confirm_question("proj_a", WO, judged(), [], ce)

    assert ce.collect_truncated and "later.py" in ce.dropped_files
    assert (f"{2 * MOD_SECTION:,} of more than "
            f"{daemon_mod.CONFIRM_COLLECT_CHARS:,} chars") in text
    assert f"of {5 * MOD_SECTION:,} chars" not in text


def test_the_secret_net_reads_the_trimmed_text_and_not_the_collection(
        started, monkeypatch):
    """`decide_evidence` scans what is persisted and sent — both directions asserted."""
    prose = added("render.py", PROSE_BODY)
    secret = added("settings.py", SECRET_BODY)
    stat, diff = prose[0] + secret[0], prose[1] + secret[1]

    store, wo = park(started, auto_review=True)
    budget(started, len(prose[1]))
    stub_evidence(monkeypatch, stat, diff)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert SECRET_VALUE not in confirmation["question"]
    assert autoreview.HELD_EVIDENCE_SECRET not in held_codes(store, wo["id"])

    # The other direction: the same secret in a KEPT hunk files nothing at all.
    store_b, wo_b = park(started, auto_review=True)
    budget(started, len(diff))
    provisional(store_b, wo_b)

    ask(started, store_b)

    assert len(questions()) == 1
    assert held_codes(store_b, wo_b["id"]) == [autoreview.HELD_EVIDENCE_SECRET]


def test_a_failed_collection_still_asks_with_an_empty_evidence_block(started, monkeypatch):
    """`Daemon._confirmation_evidence` returning `None` — collection failed — must still
    ask, with no diff fabricated and no truncation claimed."""
    store, wo = park(started, auto_review=True)
    monkeypatch.setattr(Daemon, "_confirmation_evidence", lambda self, project, wo: None)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert "diff --git" not in confirmation["question"]
    assert "diff truncated" not in confirmation["question"]
    assert "pull request: (none)\nhead: (unknown)" in confirmation["question"]


def what_changed_section(question: str) -> str:
    """The question's `# What changed` block, heading to the next `# ` heading."""
    head = "# What changed"
    start = question.index(head)
    nxt = re.compile(r"^# ", re.M).search(question, start + len(head))
    return question[start:nxt.start() if nxt else len(question)].rstrip("\n")


def test_every_byte_of_the_evidence_block_went_through_the_net(started, monkeypatch):
    """The trim goes BEFORE the net, so no byte reaches `neo_store.ask` unscanned."""
    stat, diff = mods(5)
    seen: list[str] = []
    real = autoreview.decide_evidence

    def recorder(a, stat_text, diff_text, summary=""):
        seen.append("\n".join([stat_text, diff_text, summary]))
        return real(a, stat_text, diff_text, summary=summary)

    monkeypatch.setattr(autoreview, "decide_evidence", recorder)
    store, wo = park(started, auto_review=True)
    budget(started, 2 * MOD_SECTION)
    stub_evidence(monkeypatch, stat, diff)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert len(seen) == 1
    assert what_changed_section(confirmation["question"]) in seen[0]
