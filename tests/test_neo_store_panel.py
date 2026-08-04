"""What `neo.db` has to know before a Neo panel can exist.

Three storage facts (the `panel_opinions` table, the learnings' seat scope, the `SEATS`
vocabulary) and one prerequisite bug fix (a question stranded in `answering` for ever,
bl-3f5f1464). No panel runs here — this is the layer underneath one.
"""

from __future__ import annotations

import pytest

from jarvis import catalog, neo_store
from jarvis.neo_store import NeoStore


@pytest.fixture()
def store(jarvis_home):
    s = NeoStore()
    try:
        yield s
    finally:
        s.close()


def strand(store: NeoStore, question_id: int, age_seconds: float,
           attempts: int = 0) -> None:
    """Park a question in `answering` as a dead drain would leave it.

    THE REAL CAUSE CANNOT BE STAGED IN-PROCESS: it is the daemon dying (or throwing)
    between `claim_next` and `record_answer`, and a test that kills its own interpreter
    proves nothing about the row it leaves behind. So the row is written directly, in
    exactly the state that crash produces, and the correctness argument that the cutoff
    cannot fire underneath a LIVE call is carried by
    `test_the_cutoff_outlives_the_longest_possible_neo_call` instead.
    """
    from jarvis import db

    store.conn.execute(
        "UPDATE questions SET status='answering', claimed_at=?, attempts=? WHERE id=?",
        (db.now() - age_seconds, attempts, question_id),
    )


def statuses(store: NeoStore) -> dict[str, int]:
    return {r["status"]: r["c"] for r in store.conn.execute(
        "SELECT status, COUNT(*) c FROM questions GROUP BY status")}


# -- the seat vocabulary ---------------------------------------------------------------


def test_seats_are_the_rostered_five():
    """Two other work orders validate against this tuple (a catalog roster and a CLI
    `--seat` argument), so its contents are the contract, not an implementation detail."""
    assert neo_store.SEATS == ("premise", "record", "blast", "taste", "chair")


# -- panel_opinions --------------------------------------------------------------------


def test_an_opinion_is_declared_a_child_of_its_question(store):
    """The constraint, read off the database rather than off the CREATE statement.

    `db.connect` sets `foreign_keys=ON`, so this cascade is live — and it has to be:
    `purge_work_order` hard-DELETEs questions, and `questions.id` is an AUTOINCREMENT
    rowid that SQLite reuses, so an orphan opinion would eventually reattach itself to
    an unrelated question.
    """
    fks = [dict(r) for r in store.conn.execute(
        "PRAGMA foreign_key_list(panel_opinions)")]
    assert len(fks) == 1
    assert fks[0]["table"] == "questions"
    assert fks[0]["from"] == "question_id" and fks[0]["to"] == "id"
    assert fks[0]["on_delete"] == "CASCADE"


def test_deleting_a_work_order_takes_its_deliberation_but_not_its_learnings(store):
    """The cascade end to end, with the control that makes it mean something.

    `purge_work_order` deliberately KEEPS learnings and only cuts the back-link: what
    Neo learned is durable knowledge about the user, not about the work order it came
    from. So a test that only asserted "the opinions are gone" could not tell a correct
    cascade from one that had quietly swallowed the learnings too.
    """
    q = store.ask("proj_a", "wo-doomed", "may I merge this?", kind="approval")
    store.record_opinion(q["id"], "premise", reply="it only names shipit",
                         verdict="dismiss", route="fast", model="sonnet",
                         latency_ms=1200)
    store.record_opinion(q["id"], "blast", reply="nothing ships", status="ok")
    kept = store.add_learning("a grep naming shipit ships nothing", project="proj_a",
                              source="review", question_id=q["id"])
    assert len(store.opinions(q["id"])) == 2

    assert store.purge_work_order("wo-doomed") == 1

    assert store.opinions(q["id"]) == []
    survivor = store.conn.execute(
        "SELECT * FROM learnings WHERE id=?", (kept["id"],)).fetchone()
    assert survivor is not None, "the learning was deleted — the cascade went too far"
    assert survivor["question_id"] is None
    assert survivor["content"] == "a grep naming shipit ships nothing"


def test_an_unknown_opinion_status_is_refused(store):
    """The same refusal `ask()` gives an unknown kind: an assertion, not a silent row."""
    q = store.ask("proj_a", "wo-1", "which delimiter?")
    with pytest.raises(AssertionError):
        store.record_opinion(q["id"], "blast", status="maybe")
    with pytest.raises(AssertionError):
        store.ask("proj_a", "wo-1", "which delimiter?", kind="vibes")
    assert store.opinions(q["id"]) == []


def test_a_re_run_replaces_a_seats_opinion_rather_than_doubling_it(store):
    """A question CAN be answered twice — `reclaim_stale` re-queues a stranded one — so
    the panel re-records the same seats. One row per seat, last run wins, stable id."""
    q = store.ask("proj_a", "wo-1", "which delimiter?")
    first = store.record_opinion(q["id"], "premise", reply="stranded attempt",
                                 route="panel")
    second = store.record_opinion(q["id"], "premise", reply="the answer that shipped",
                                  route="fast", verdict="dismiss")

    rows = store.opinions(q["id"])
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"] == second["id"]   # a stable handle for the UI
    assert rows[0]["reply"] == "the answer that shipped"
    assert rows[0]["route"] == "fast" and rows[0]["verdict"] == "dismiss"


def test_opinions_read_back_what_the_seats_recorded(store):
    q = store.ask("proj_a", "wo-1", "which delimiter?")
    store.record_opinion(q["id"], "premise", reply="a real merge", verdict="",
                         route="panel", model="sonnet", latency_ms=900)
    store.record_opinion(q["id"], "taste", status="abstained")

    rows = store.opinions(q["id"])
    assert [r["seat"] for r in rows] == ["premise", "taste"]
    assert rows[0]["route"] == "panel" and rows[0]["latency_ms"] == 900
    assert rows[0]["model"] == "sonnet"
    # an abstention is a recorded row, not a missing one: it is the evidence the seat ran
    assert rows[1]["status"] == "abstained" and rows[1]["reply"] == ""
    assert rows[1]["route"] == "", "only the premise seat routes"


# -- learnings: the seat scope ---------------------------------------------------------


def test_seat_scope_is_additive_and_the_default_is_global_only(store):
    """The one thing about this feature that is easy to get backwards.

    Asserting only that `seat="blast"` returns something would not distinguish a working
    filter from a broken one: `[]` reads identically to "no blast learnings exist". So
    every assertion here names the global row as well, in the same call.
    """
    store.add_learning("always default to CSV", project="proj_a")
    store.add_learning("a grep that merely names shipit ships nothing",
                       project="proj_a", seat="blast")
    store.add_learning("one line when you agree", project="proj_a", seat="taste")

    def contents(**kw):
        return [r["content"] for r in store.learnings("proj_a", **kw)]

    # the default is what `neo.build_system_prompt` uses — it must NOT widen
    assert contents() == ["always default to CSV"]
    assert contents(seat="") == ["always default to CSV"]
    # additive, and oldest-first survives the extra WHERE clause (append-only prefix)
    assert contents(seat="blast") == [
        "always default to CSV", "a grep that merely names shipit ships nothing"]
    assert contents(seat="taste") == [
        "always default to CSV", "one line when you agree"]


# -- the stranded question -------------------------------------------------------------


def test_the_cutoff_outlives_the_longest_possible_neo_call():
    """A cutoff below the call timeout re-queues a question underneath a live call, and
    the worker gets two answers to one question. This is the assertion that makes
    `reclaim_stale` safe to run on a live queue."""
    assert neo_store.STALE_ANSWERING_SECONDS > catalog.NeoConfig().timeout


def test_a_stale_question_comes_back_with_an_attempt_spent(store):
    q = store.ask("proj_a", "wo-1", "which delimiter?")
    strand(store, q["id"], age_seconds=neo_store.STALE_ANSWERING_SECONDS + 60)

    assert store.reclaim_stale() == {"requeued": [q["id"]], "failed": []}
    back = store.get(q["id"])
    assert back["status"] == "queued"
    assert back["attempts"] == 1
    assert back["claimed_at"] is None


def test_a_question_claimed_a_second_ago_is_left_alone(store):
    q = store.ask("proj_a", "wo-1", "which delimiter?")
    strand(store, q["id"], age_seconds=1)

    assert store.reclaim_stale() == {"requeued": [], "failed": []}
    assert store.get(q["id"])["status"] == "answering"
    assert store.get(q["id"])["attempts"] == 0


def test_no_other_status_is_touched(store):
    """The whole histogram, not just the row under test: a WHERE clause that dropped
    `status='answering'` would re-queue answered and escalated questions too, and
    re-delivering a decided answer is worse than the bug being fixed."""
    for kind, status in (("queued", "queued"), ("answered", "answered"),
                         ("escalated", "escalated"), ("failed", "failed")):
        q = store.ask("proj_a", "wo-1", f"a {kind} question")
        if status != "queued":
            store.conn.execute(
                "UPDATE questions SET status=?, claimed_at=? WHERE id=?",
                (status, 0.0, q["id"]))  # 0.0 = the epoch, stale by any cutoff
    before = statuses(store)

    assert store.reclaim_stale() == {"requeued": [], "failed": []}
    assert statuses(store) == before


def test_a_question_at_the_attempt_ceiling_fails_instead_of_looping(store):
    q = store.ask("proj_a", "wo-1", "which delimiter?")
    strand(store, q["id"], age_seconds=neo_store.STALE_ANSWERING_SECONDS + 60,
           attempts=neo_store.MAX_ANSWER_ATTEMPTS)

    assert store.reclaim_stale() == {"requeued": [], "failed": [q["id"]]}
    dead = store.get(q["id"])
    assert dead["status"] == "failed"          # the only status surfaced as attention
    assert str(neo_store.MAX_ANSWER_ATTEMPTS) in dead["answer_reason"]
    assert dead["attempts"] == neo_store.MAX_ANSWER_ATTEMPTS  # not spent on the way out


def test_a_question_stranded_before_this_shipped_is_not_exempt(store):
    """`claimed_at` is NULL on every row written by the release in production — which is
    where the questions this fix exists for actually are."""
    q = store.ask("proj_a", "wo-1", "which delimiter?")
    store.conn.execute(
        "UPDATE questions SET status='answering', claimed_at=NULL, ts=? WHERE id=?",
        (0.0, q["id"]))

    assert store.reclaim_stale()["requeued"] == [q["id"]]


def test_the_daemon_rescues_a_stranded_question_and_answers_it(
        jarvis_home, fake_claude, catalog_file, project):
    """End to end through the real path: a row left in `answering` by a dead drain is
    reclaimed by `neo_tick`, drained on the neo thread, and answered — in ONE tick.

    Reclaiming after the queued count is read would have rescued it and then gone back to
    sleep, so the assertion is on the answer, not on the status being `queued` again.
    """
    from jarvis import ops
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")

    seed = NeoStore()
    try:
        strand(seed, 1, age_seconds=neo_store.STALE_ANSWERING_SECONDS + 60)
    finally:
        seed.close()

    daemon.neo_tick()
    daemon.neo_pool.shutdown(wait=True)  # join the drain thread

    check = NeoStore()
    try:
        q = check.get(1)
        assert q["status"] == "answered", q
        assert q["attempts"] == 1
    finally:
        check.close()
