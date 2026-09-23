"""A resubmission that answers nothing the last round asked about never reaches the panel.

Spec docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md. Two measured
failures, one predicate — wo-2005a89b round 3 (only tests changed, behaviour re-judged and
flipped) and wo-0a9ba9b3 round 2 (none of the five asks touched, a full round spent).

THE SEATS HERE REPLY IN JSON. `validation.findings` parses a seat's reply, and the prose
replies `tests/test_validation_loop.py` uses parse to no findings at all — which is the
FAIL-OPEN case, so a test built on those would pass with the whole feature deleted.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from jarvis import evidence, ops, validation
from jarvis.invariants import VALIDATION_STUCK_BLOCKER, true_blockers
from jarvis.project_store import ProjectStore
from tests.test_validation_loop import _git
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)


def edit(fleet, wo_id: str, text: str, name: str) -> None:
    """A committed change at a NESTED path.

    `Fleet.change` writes to the worktree root, and a root-level `app.py` is exactly what
    `validation.cited_paths` refuses to read as a citation — one directory separator is
    the whole difference between a path and a sentence's subject.
    """
    tree = fleet.worktree(wo_id)
    path = tree / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", f"work {len(text)}")


def rejecting(*paths: str) -> dict:
    """A rejection whose blockers CITE files — what a real seat's JSON looks like."""
    reply = json.dumps({
        "verdict": "reject",
        "reason": "the change is not covered",
        "findings": [{"severity": "blocker", "title": f"{p} is wrong",
                      "detail": f"look at {p} and fix it"} for p in paths],
    })
    return {"outcome": "rejected", "reason": f"fix {', '.join(paths)}",
            "seats": [{"seat": "tester", "status": "ok", "verdict": "reject",
                       "model": "sonnet", "latency_ms": 5, "reply": reply}]}


def rounds(fleet, wo_id: str) -> list[dict]:
    store = fleet.store()
    try:
        return [dict(r) for r in store.validation_rounds(wo_id=wo_id)]
    finally:
        store.close()


def events(fleet, wo_id: str, kind: str) -> list[dict]:
    store = fleet.store()
    try:
        return [{**e, "payload": json.loads(e["payload"] or "{}")}
                for e in store.events_of_kind(wo_id, kind)]
    finally:
        store.close()


def rejected_once(fleet, wo_id: str, *cited: str) -> Validator:
    """Round 1, rejected, citing `cited`. Leaves the work order back with its worker."""
    v = fleet.daemon.validator = Validator(rejecting(*cited))
    edit(fleet, wo_id, "print('one')\n", "src/app.py")
    finish(fleet, wo_id)
    fleet.drain()
    fleet.tick()          # deliver the rejection envelope
    assert [r["outcome"] for r in rounds(fleet, wo_id)] == ["rejected"]
    return v


# -- the rule itself, in isolation ---------------------------------------------------


def test_cited_paths_reads_title_and_detail_and_drops_a_line_number():
    found = [{"title": "src/jarvis/budget.py:41 is wrong",
              "detail": "compare against tests/test_budget.py"},
             {"title": "no path here at all", "detail": "really none"}]
    assert validation.cited_paths(found) == ("src/jarvis/budget.py",
                                             "tests/test_budget.py")


def test_a_bare_filename_is_not_a_citation():
    """`budget.py` is as often a sentence's subject as a path, and one false citation
    bounces real work."""
    assert validation.cited_paths([{"title": "budget.py is wrong", "detail": ""}]) == ()


def test_changed_since_counts_a_deleted_path():
    before, now = {"a.py": "1", "b.py": "2"}, {"a.py": "9"}
    assert evidence.changed_since(before, now) == frozenset({"a.py", "b.py"})


@pytest.mark.parametrize("previous, before, now", [
    (None, {"a/b.py": "1"}, {"a/b.py": "1"}),                       # no previous round
    ({"outcome": "passed"}, {"a/b.py": "1"}, {"a/b.py": "1"}),      # not a rejection
    ({"outcome": "rejected"}, {}, {"a/b.py": "1"}),                 # nothing recorded
    ({"outcome": "rejected"}, {"a/b.py": "1"}, {}),                 # no diff now
])
def test_every_uncertainty_lets_the_panel_judge(previous, before, now):
    found = [{"title": "a/b.py is wrong", "detail": ""}]
    assert validation.unanswered_submission(previous, found, before, now) is None


def test_blockers_that_cite_nothing_let_the_panel_judge():
    found = [{"title": "this is just bad", "detail": "no path anywhere"}]
    assert validation.unanswered_submission(
        {"outcome": "rejected"}, found, {"a/b.py": "1"}, {"a/b.py": "1"}) is None


@pytest.mark.parametrize("cited,moved,touched", [
    ("src/jarvis/budget.py", "src/jarvis/budget.py", True),       # the usual case
    ("jarvis/budget.py", "src/jarvis/budget.py", True),           # a seat quoting short
    ("src/jarvis/budget.py", "jarvis/budget.py", True),           # and the other way
    ("src/jarvis/project_store.py", "store.py", False),           # the cut is at a
    ("src/store.py", "src/project_store.py", False),              # separator, or this
    ("src/jarvis/budget.py", "src/jarvis/budgets.py", False),     # pair would match
])
def test_a_citation_matches_a_moved_path_only_at_a_directory_boundary(cited, moved,
                                                                      touched):
    """`_touched` decides whether real work is bounced. A suffix that did not have to
    start at a separator would match `store.py` against `project_store.py` and let a
    round that answered nothing through; an exact-only rule would bounce the seat that
    quoted the path without its `src/` prefix."""
    assert validation._touched(cited, frozenset({moved, "unrelated/x.py"})) is touched


def test_the_short_citation_reaches_the_rule_and_not_just_the_helper():
    """The suffix branch is only worth anything if `unanswered_submission` uses it."""
    found = [{"title": "jarvis/budget.py is wrong", "detail": ""}]
    assert validation.unanswered_submission(
        {"outcome": "rejected"}, found, {"a.py": "1"},
        {"src/jarvis/budget.py": "2"}) is None
    assert validation.unanswered_submission(
        {"outcome": "rejected"}, found, {"a.py": "1"},
        {"src/jarvis/budgets.py": "2"}) == ("jarvis/budget.py",)


def test_one_cited_path_touched_is_enough():
    """SOME of the list goes to the panel — only NONE short-circuits (spec §5)."""
    found = [{"title": "a/b.py and a/c.py are wrong", "detail": ""}]
    assert validation.unanswered_submission(
        {"outcome": "rejected"}, found,
        {"a/b.py": "1", "a/c.py": "1"}, {"a/b.py": "2", "a/c.py": "1"}) is None


def test_the_paths_are_returned_when_none_of_them_moved():
    found = [{"title": "a/b.py is wrong", "detail": ""}]
    assert validation.unanswered_submission(
        {"outcome": "rejected"}, found,
        {"a/b.py": "1"}, {"a/b.py": "1", "a/z.py": "9"}) == ("a/b.py",)


# -- the round records what it judged -------------------------------------------------


def test_a_judged_round_records_a_digest_per_file(fleet):
    wo = fleet.dispatch()
    fleet.daemon.validator = Validator(passed())
    edit(fleet, wo["id"], "print('one')\n", "src/app.py")
    finish(fleet, wo["id"])
    fleet.drain()

    shas = ProjectStore.validation_file_shas(rounds(fleet, wo["id"])[0])
    assert "src/app.py" in shas and shas["src/app.py"]


def test_a_round_that_predates_the_column_lets_the_panel_judge(tmp_path):
    """The fail-open the spec promises costs a live submitter nothing.

    Every round judged before this work order landed recorded no map, and `''` reads as
    "not recorded" and never as "the submitter touched nothing" — otherwise the first
    resubmission on every upgraded database would be bounced for work it did do. The
    column is DROPPED and the store reopened, because a fresh database gets it from the
    `CREATE TABLE` and would pass with the migration forgotten.
    """
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    store = ProjectStore(proj)                          # today's schema...
    wo = store.create_work_order(title="aged", description="")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="a")
    store.close_validation_round(int(rnd["id"]), "rejected", "fix src/app.py")
    store.close()
    conn = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    conn.execute("ALTER TABLE validation_rounds DROP COLUMN file_shas")   # ...aged
    conn.commit()
    conn.close()

    store = ProjectStore(proj)                          # the upgrade
    try:
        row = store.last_judged_round(wo_id=wo["id"])
        assert row is not None and row["file_shas"] == ""
        assert ProjectStore.validation_file_shas(row) == {}
        # and the rule reads that as "I cannot tell", not as "nothing moved"
        assert validation.unanswered_submission(
            row, [{"title": "src/app.py is wrong", "detail": ""}],
            ProjectStore.validation_file_shas(row), {"notes/notes.py": "9"}) is None
    finally:
        store.close()


# -- failure 2: a resubmission that answers none of the list ---------------------------


def test_a_submission_touching_none_of_the_cited_paths_never_reaches_the_panel(fleet):
    wo = fleet.dispatch()
    v = rejected_once(fleet, wo["id"], "src/app.py")

    edit(fleet, wo["id"], "# unrelated\n", "notes/notes.py")
    assert finish(fleet, wo["id"])["status"] == "validating"
    fleet.drain()

    assert len(v.calls) == 1, "the panel was convened over a submission answering nothing"
    assert [r["round"] for r in rounds(fleet, wo["id"])] == [1]


def test_a_bounce_parks_even_when_the_LATEST_round_is_not_the_one_it_read(fleet):
    """The bounce reads `last_judged_round`, which walks BACK past `failed` and `void`
    rows — so the latest row can be one the bounce never looked at, and letting the join
    re-derive the status from it lands unreviewed work in the merge queue.

    The `void` here is the row `Daemon._void` writes (a redelivery with nothing in it):
    settled, and NOT counted, so `last_judged_round` still answers round 1.
    """
    wo = fleet.dispatch()
    rejected_once(fleet, wo["id"], "src/app.py")
    store = fleet.store()
    try:
        voided = store.open_validation_round(wo_id=wo["id"], fingerprint="empty",
                                             round=2)
        store.close_validation_round(int(voided["id"]), "void", "nothing to judge")
    finally:
        store.close()

    edit(fleet, wo["id"], "# unrelated\n", "notes/notes.py")
    assert finish(fleet, wo["id"])["status"] == "validating"

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "validating"
        assert str(store.latest_validation_round(wo_id=wo["id"])["outcome"]) == "void"
    finally:
        store.close()
    assert events(fleet, wo["id"], "validation_bounced"), "this was a bounce"


def test_the_bounce_spends_no_round(fleet):
    """The next real submission is still round 2 — spec §5."""
    wo = fleet.dispatch()
    v = rejected_once(fleet, wo["id"], "src/app.py")

    edit(fleet, wo["id"], "# unrelated\n", "notes/notes.py")
    finish(fleet, wo["id"])
    fleet.tick()                                 # deliver the bounce
    edit(fleet, wo["id"], "print('two')\n", "src/app.py")     # now answer it
    finish(fleet, wo["id"])
    fleet.drain()

    assert [r["round"] for r in rounds(fleet, wo["id"])] == [1, 2]
    assert len(v.calls) == 2


def test_the_bounce_names_the_paths_it_checked(fleet):
    """The user's ruling on Neo question 518: a bounce that leaves no trace is silence."""
    wo = fleet.dispatch()
    rejected_once(fleet, wo["id"], "src/app.py")
    edit(fleet, wo["id"], "# unrelated\n", "notes/notes.py")
    finish(fleet, wo["id"])

    [bounce] = events(fleet, wo["id"], "validation_bounced")
    assert bounce["payload"]["after_round"] == 1
    assert bounce["payload"]["cited"] == ["src/app.py"]


def test_the_bounce_goes_back_to_the_worker_as_feedback(fleet):
    wo = fleet.dispatch()
    rejected_once(fleet, wo["id"], "src/app.py")
    edit(fleet, wo["id"], "# unrelated\n", "notes/notes.py")
    finish(fleet, wo["id"])

    # THE ENVELOPE, not a message: `bus.post` never resolves, and a test that ticked
    # first would be asserting the router's work rather than the bounce's.
    store = fleet.store()
    try:
        [env] = [e for e in store.queued_envelopes()
                 if e["subject_wo_id"] == wo["id"]]
        payload = json.loads(env["payload"])
    finally:
        store.close()
    assert env["to_role"] == "implementor"
    assert "src/app.py" in payload["reason"]
    assert "no round was spent" in payload["reason"]


def test_touching_a_cited_path_reaches_the_panel(fleet):
    wo = fleet.dispatch()
    v = rejected_once(fleet, wo["id"], "src/app.py")
    v.outcomes = [passed()]

    edit(fleet, wo["id"], "print('answered')\n", "src/app.py")   # the cited file
    finish(fleet, wo["id"])
    fleet.drain()

    assert len(v.calls) == 2
    assert [r["outcome"] for r in rounds(fleet, wo["id"])] == ["rejected", "passed"]


# -- failure 1: the behaviour was rejected and is unchanged ----------------------------


def test_a_test_only_round_is_bounced_when_the_behaviour_was_rejected(fleet):
    """wo-2005a89b round 3, exactly: only a test moved, and round 1 rejected src/app.py."""
    wo = fleet.dispatch()
    v = rejected_once(fleet, wo["id"], "src/app.py")

    edit(fleet, wo["id"], "def test_it(): pass\n", "tests/test_app.py")
    finish(fleet, wo["id"])
    fleet.drain()

    assert len(v.calls) == 1
    assert [r["round"] for r in rounds(fleet, wo["id"])] == [1]


def test_a_test_only_round_reaches_the_panel_when_the_tests_were_what_was_rejected(fleet):
    """The case the work order says must NOT be refused: round 1 asked for coverage and
    the resubmission is the coverage."""
    wo = fleet.dispatch()
    v = rejected_once(fleet, wo["id"], "tests/test_app.py")
    v.outcomes = [passed()]

    edit(fleet, wo["id"], "def test_it(): pass\n", "tests/test_app.py")
    finish(fleet, wo["id"])
    fleet.drain()

    assert len(v.calls) == 2
    assert [r["outcome"] for r in rounds(fleet, wo["id"])] == ["rejected", "passed"]


# -- the ceiling -----------------------------------------------------------------------


def test_the_third_bounce_asks_the_user_and_the_flag_survives_a_reconcile(fleet):
    wo = fleet.dispatch()
    v = rejected_once(fleet, wo["id"], "src/app.py")

    for i in range(ops.BOUNCE_LIMIT + 1):
        edit(fleet, wo["id"], f"# unrelated {i}\n", "notes/notes.py")
        finish(fleet, wo["id"])
        fleet.tick()

    assert len(v.calls) == 1, "the panel was convened after all"
    assert len(events(fleet, wo["id"], "validation_bounced")) == ops.BOUNCE_LIMIT
    assert [r["outcome"] for r in rounds(fleet, wo["id"])] == ["rejected", "escalated"]

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
        # The flag is RE-DERIVED every tick; a give-up with no round behind it loses it.
        assert VALIDATION_STUCK_BLOCKER in true_blockers(
            store, store.get_work_order(wo["id"]))
        # The escalated round is a ROUND, so it carries the map like any other: the next
        # submission reads it through `last_judged_round` and would fail open on a blank.
        given_up = store.last_judged_round(wo_id=wo["id"])
        assert int(given_up["round"]) == 2
        shas = ProjectStore.validation_file_shas(given_up)
        assert "notes/notes.py" in shas and "src/app.py" in shas
    finally:
        store.close()


def test_a_panel_round_in_between_restarts_the_count(fleet):
    """`consecutive` is true by construction — the key is the round bounced after."""
    wo = fleet.dispatch()
    v = rejected_once(fleet, wo["id"], "src/app.py")

    edit(fleet, wo["id"], "# unrelated\n", "notes/notes.py")
    finish(fleet, wo["id"])
    fleet.tick()
    v.outcomes = [rejecting("src/app.py")]
    edit(fleet, wo["id"], "print('two')\n", "src/app.py")           # answers it — round 2 runs
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()

    assert [r["round"] for r in rounds(fleet, wo["id"])] == [1, 2]
    store = fleet.store()
    try:
        assert ops.consecutive_bounces(store, wo["id"], 2) == 0
    finally:
        store.close()
