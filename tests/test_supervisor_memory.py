"""The supervisor's memory — §6 of
docs/superpowers/specs/2026-08-31-the-supervisor.md.

EVERY OBVIOUS TEST HERE IS GREEN BEFORE THE CHANGE, which is why each one is written as a
pair. `NeoStore.learnings` already scoped by seat, `SEATS` already lacked `"supervisor"`,
and `add_learning(seat="supervisor")` already stayed out of Neo's prompt — so "assert Neo's
prompt is unchanged" and "assert `supervisor` is not in `SEATS`" both grade nothing about
this work order. What ships here is that `ops.review_alarm` writes the row at all, that it
writes it under THAT seat, and that `ops.validate_seat` accepts the scope without the
catalog accepting it as a roster seat. Each assertion below names the positive case in the
same test as its negative control.

No model call is made: the supervisor's verdict is stated with `update_alarm`, exactly as
`tests/test_alarm_review.py` states it.
"""

from __future__ import annotations

import pytest

from jarvis import neo as neo_mod
from jarvis import ops, supervisor
from jarvis.neo_store import LEARNING_SCOPES, SEATS, SUPERVISOR_SEAT, NeoStore
from jarvis.project_store import ProjectStore

@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """The registered fleet `ops.review_alarm` reads projects out of."""
    ops.start_os(str(catalog_file), foreground=True)


FIRED = "turn 1 has been running 2h and is still being billed"
WHY = "eleven minutes of a design review is the shape of the work, not a stall"
NOTE = "the design doc is long on purpose"
RULING = "a two-hour turn with no file written is never explicable — escalate it"


def _decided_alarm(*, verdict="ack", status="acked", qid=None):
    """One work order with one alarm the supervisor has already judged."""
    wo = ops.create_work_order("proj_a", "a very slow design")
    path = ops.find_work_order(wo["id"])[1]
    store = ProjectStore(path)
    try:
        alarm = store.add_alarm(wo["id"], "long-turn", 1, FIRED)
        store.update_alarm(alarm["id"], status=status, verdict=verdict,
                           verdict_reason=WHY, note=NOTE, decided_at=alarm["ts"],
                           neo_question_id=qid)
    finally:
        store.close()
    return wo["id"], alarm["id"]


def _learnings(**kwargs):
    store = NeoStore()
    try:
        return store.learnings("proj_a", **kwargs)
    finally:
        store.close()


def _prompts():
    """Neo's system prompt and the supervisor's, built off the same live ledger."""
    store = NeoStore()
    try:
        return (neo_mod.build_system_prompt(store, "proj_a"),
                supervisor.build_system_prompt(store, "proj_a"))
    finally:
        store.close()


# -- the loop the feature exists to close ----------------------------------------------


def test_a_correction_teaches_the_supervisor_without_touching_neos_prompt(started):
    """THE ONE TEST THAT GRADES THIS WORK ORDER, and it has to be one test.

    Driven through `ops.review_alarm` rather than `add_learning` directly: the store
    already scoped by seat before this branch existed, so seeding the row by hand proves
    only that SQLite works. This fails if `review_alarm` writes the learning with no seat
    (Neo's prompt grows) or under a panel seat's name (the supervisor's does not).
    """
    _, alarm_id = _decided_alarm()
    neo_before, supervisor_before = _prompts()

    ops.review_alarm(alarm_id, approved=False, feedback=RULING)

    neo_after, supervisor_after = _prompts()
    assert neo_after == neo_before
    assert RULING not in neo_after
    assert RULING in supervisor_after
    assert len(supervisor_after) > len(supervisor_before)

    rows = _learnings(seat=SUPERVISOR_SEAT)
    assert len(rows) == 1
    assert rows[0]["seat"] == SUPERVISOR_SEAT
    assert rows[0]["source"] == "review"
    assert rows[0]["project"] == "proj_a"
    # The sentence carries the verdict it corrects, not the ruling alone: a lesson with
    # no case attached teaches the next review nothing about when it applies.
    assert "long-turn" in rows[0]["content"] and WHY in rows[0]["content"]


def test_an_approved_verdict_teaches_nothing(started):
    """The negative control the test above needs. `review_alarm` is also the surface for
    "the supervisor was right", and recording a learning there would fill the prompt with
    the cases that needed no correction."""
    _, alarm_id = _decided_alarm()
    ops.review_alarm(alarm_id, approved=True)

    assert _learnings(seat=SUPERVISOR_SEAT) == []
    _, supervisor_prompt = _prompts()
    assert "(none yet — escalate when unsure)" in supervisor_prompt


def test_the_correction_survives_the_alarm_it_came_from(started):
    """The split the feature runs on: the alarm is project state and the lesson is not.

    Deleting the work order takes its whole project record with it — the alarm row
    included — and the supervisor still knows what it was told.
    """
    wo_id, alarm_id = _decided_alarm()
    ops.review_alarm(alarm_id, approved=False, feedback=RULING)
    ops.delete_work_order(wo_id)

    _, supervisor_prompt = _prompts()
    assert RULING in supervisor_prompt


# -- the vocabulary, and why it is two constants ---------------------------------------


def test_the_supervisor_is_a_learning_scope_and_never_a_panel_seat(tmp_path):
    """Both halves, because either alone is already true.

    `"supervisor" not in SEATS` passes on `main`; so does refusing it from
    `validate_seat`. What `LEARNING_SCOPES` buys is the pair — a catalog still cannot
    seat the supervisor on Neo's panel, where it has no definition and no mandate, and a
    learning may still be scoped to it.
    """
    import json

    from jarvis.catalog import CatalogError, parse_catalog

    assert SUPERVISOR_SEAT in LEARNING_SCOPES and SUPERVISOR_SEAT not in SEATS
    ops.validate_seat(SUPERVISOR_SEAT)  # does not raise

    data = {
        "os": {"neo": {"panel": {"roster": ["premise", SUPERVISOR_SEAT, "chair"]}}},
        "projects": [{"name": "proj_a", "path": str(tmp_path)}],
    }
    with pytest.raises(CatalogError, match=SUPERVISOR_SEAT):
        parse_catalog(json.loads(json.dumps(data)))

    with pytest.raises(ops.OpsError, match="unknown seat"):
        ops.validate_seat("supervsior")


def test_a_neo_answer_still_cannot_be_corrected_at_the_supervisor(jarvis_home):
    """Widening `validate_seat` must not open a door in `neo_review`.

    The refusal moved rather than disappearing: it is now the "did this seat opine on
    this question" check, which the supervisor can never pass because it opines on
    alarms and not on Neo's questions. That message is also the better one — it names
    the seats that did.
    """
    store = NeoStore()
    try:
        q = store.ask("proj_a", "wo-nothing", "which delimiter?")
        store.record_answer(q["id"], "CSV")
        store.record_opinion(q["id"], "premise", verdict="answer", route="fast")
    finally:
        store.close()

    with pytest.raises(ops.OpsError, match="premise"):
        ops.neo_review(q["id"], approved=False, feedback=RULING,
                       seat=SUPERVISOR_SEAT)

    assert _learnings(seat=SUPERVISOR_SEAT) == []


# -- what the shared renderer already guarantees, inherited rather than rebuilt ---------


def test_the_supervisors_block_is_append_only(jarvis_home):
    """A second learning EXTENDS the prompt instead of rewriting it, which is the whole
    reason `NeoStore.learnings` returns rows oldest-first. Asserted as a prefix, because
    that is the exact property the Anthropic prompt cache keys on."""
    store = NeoStore()
    try:
        store.add_learning("the first ruling", project="proj_a", seat=SUPERVISOR_SEAT)
        first = supervisor.build_system_prompt(store, "proj_a")
        store.add_learning("the second ruling", project="proj_a", seat=SUPERVISOR_SEAT)
        second = supervisor.build_system_prompt(store, "proj_a")
    finally:
        store.close()

    assert second.startswith(first)
    assert "the second ruling" in second


def test_the_supervisors_block_inherits_the_same_character_bound(jarvis_home):
    """Mirrors `test_a_panel_seat_inherits_the_same_bound`: a bound that lived only in
    `neo.build_system_prompt` would leave every other reader of `render_learnings`
    unbounded, and this one is rebuilt on every alarm."""
    store = NeoStore()
    try:
        for i in range(60):
            store.add_learning(f"ruling {i}: " + "w" * 900, project="proj_a",
                               seat=SUPERVISOR_SEAT)
        prompt = supervisor.build_system_prompt(store, "proj_a")
    finally:
        store.close()

    assert "older learnings not shown" in prompt
    assert len(prompt) < 30000


def test_a_retracted_ruling_leaves_the_next_review(started, capsys):
    """The retraction discipline arrives with the table, and this is what "nothing new is
    built" is worth: `jarvis neo learnings --seat supervisor` lists the row, and
    `jarvis neo retract` takes it out of the next prompt while keeping it on the ledger.
    """
    from jarvis import cli

    _, alarm_id = _decided_alarm()
    ops.review_alarm(alarm_id, approved=False, feedback=RULING)
    learning_id = _learnings(seat=SUPERVISOR_SEAT)[0]["id"]

    # `--project` for the same reason every project-scoped learning needs it: the
    # command's default scope is global, and the ruling is about proj_a's alarm.
    cli.main(["neo", "learnings", "--project", "proj_a", "--seat", SUPERVISOR_SEAT])
    assert RULING in capsys.readouterr().out

    # Without --seat it is a Neo surface and must not show a supervisor row.
    cli.main(["neo", "learnings", "--project", "proj_a"])
    assert RULING not in capsys.readouterr().out

    cli.main(["neo", "retract", str(learning_id), "--reason", "the turn was explicable"])
    _, supervisor_prompt = _prompts()
    assert RULING not in supervisor_prompt

    # Retired, not deleted: the audit surface still carries it, marked.
    cli.main(["neo", "learnings", "--project", "proj_a", "--seat", SUPERVISOR_SEAT])
    assert "the turn was explicable" in capsys.readouterr().out
