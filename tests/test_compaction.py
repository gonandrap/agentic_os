"""Compacting a conversation whose prompt cache has expired, before the next prompt.

Three layers, and each has a different way of being wrong:

* THE DECISION (`worker_session.compaction_due`) — every exclusion is a way this could
  cost more than it saves, so each one is a test and each sits beside the case that
  SHOULD fire. "Did not compact" is indistinguishable from "cannot compact" otherwise.
* THE DELIVERY (`Daemon._compacted_first`) — the compaction goes out as a turn of its
  own and the message stays queued behind it. What matters is that the message is not
  lost and not double-sent.
* THE ACCOUNTING (`usage`, `bill`, `inspection`, `agent_usage`) — a compaction is a
  full-priced API call the transcript does not record, and the write it leaves behind
  looks exactly like the prefix defect the fleet already watches for. Both of those
  would turn a real saving into a reported one.

Spec: docs/superpowers/specs/2026-09-18-compact-past-the-ttl.md
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import bill, claude_cli, inspection, ops, usage, worker_session
from jarvis.catalog import COMPACT_MIN_CONTEXT_MIN, CatalogError, load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import COMPACT_TURN, ProjectStore

FLOOR = 5_000
BIG = 2_000          # the fake CLI reports a 3,003-token context on every turn…
HUGE = 500_000       # …so this is "bigger than anything the fake will report"


@pytest.fixture()
def fleet(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    daemon = Daemon(catalog)
    # The fake CLI's turns report a 3,003-token context, and the real floor is 100,000
    # (`catalog.DEFAULT_COMPACT_MIN_CONTEXT`) — a value no fixture can reach and no
    # catalog may go below. Lowered on the loaded object rather than in the file, so
    # the parser's own rule stays under test in `test_the_floor_has_a_floor`.
    daemon.catalog.os.compact_min_context = BIG
    return {"daemon": daemon, "project": catalog.projects[0],
            "store": ProjectStore(project), "catalog": catalog}


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    """A fake `~/.claude/projects` tree — `tests/test_usage.py`'s fixture, which lives
    there because that is where the format itself is under test."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict], *, slug: str = "-proj"):
        project_dir = root / slug
        project_dir.mkdir(exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    return write


def _running_wo(fleet, settle_turns) -> dict:
    """A work order one turn in, with a session and a settled boundary behind it."""
    wo = ops.create_work_order("proj_a", "task")
    store, project = fleet["store"], fleet["project"]
    wo = store.get_work_order(wo["id"])
    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    return store.get_work_order(wo["id"])


def _age_last_turn(store: ProjectStore, wo_id: str, seconds: float) -> None:
    """Move the last turn's end back, which is the only input the clock test has."""
    turn = store.latest_turn(wo_id)
    store.conn.execute("UPDATE wo_turns SET ended_at=? WHERE id=?",
                       (turn["ended_at"] - seconds, turn["id"]))


def _big_context(store: ProjectStore, wo_id: str, tokens: int = BIG * 10) -> None:
    """Put a context on the last turn, the only input the floor test has."""
    turn = store.latest_turn(wo_id)
    store.conn.execute("UPDATE wo_turns SET usage_json=? WHERE id=?",
                       (json.dumps({"context_peak": tokens}), turn["id"]))


def _deliver(fleet, wo_id: str) -> None:
    fleet["daemon"].deliver_messages(fleet["project"], fleet["store"])


def _turns(store: ProjectStore, wo_id: str) -> list[tuple[str, str]]:
    return [(t["kind"], t["prompt"]) for t in store.list_turns(wo_id)]


# -- the trigger -----------------------------------------------------------------------


def test_a_cold_boundary_with_a_large_conversation_compacts_before_the_prompt(
        fleet, settle_turns, fake_claude):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    ops.send_message(wo["id"], "please also do the other thing")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)

    _deliver(fleet, wo["id"])

    kinds = _turns(store, wo["id"])
    assert kinds[-1] == (COMPACT_TURN, claude_cli.COMPACT_PROMPT)
    # THE MESSAGE IS NOT SPENT. Nothing about it has happened, so it must still be
    # queued — marking it delivered here would lose it entirely.
    assert [m["content"] for m in store.queued_messages(wo["id"])] == [
        "please also do the other thing"]
    assert settle_turns(store)

    # …and the tick after the compaction settles delivers it, into the warm session.
    _deliver(fleet, wo["id"])
    assert _turns(store, wo["id"])[-1] == ("message", "please also do the other thing")
    assert not store.queued_messages(wo["id"])


def test_a_boundary_inside_the_ttl_does_not_compact(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    ops.send_message(wo["id"], "carry on")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS - 30)

    _deliver(fleet, wo["id"])

    assert _turns(store, wo["id"])[-1] == ("message", "carry on")


def test_a_small_conversation_past_the_ttl_does_not_compact(fleet, settle_turns):
    """The break-even floor: below it the summary costs more than the re-write."""
    store = fleet["store"]
    fleet["daemon"].catalog.os.compact_min_context = HUGE
    wo = _running_wo(fleet, settle_turns)
    ops.send_message(wo["id"], "carry on")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 600)

    _deliver(fleet, wo["id"])

    assert _turns(store, wo["id"])[-1] == ("message", "carry on")


# -- the exclusions --------------------------------------------------------------------


def test_the_off_switch_stops_it_and_nothing_else(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    fresh = store.get_work_order(wo["id"])

    assert worker_session.compaction_due(store, fresh, BIG) is not None
    assert worker_session.compaction_due(store, fresh, None) is None


def test_an_opening_turn_is_never_compacted_before(fleet):
    """Nothing to summarise, and the first prompt writes the cache rather than
    re-writing it."""
    store = fleet["store"]
    wo = store.get_work_order(ops.create_work_order("proj_a", "task")["id"])

    assert worker_session.compaction_due(store, wo, BIG) is None


def test_a_turn_in_flight_is_never_compacted_under(fleet, fake_claude, settle_turns):
    """Never mid-turn: what a compaction discards is what that turn is holding."""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    gate = fake_claude.hold_turns()
    worker_session.send(store, fleet["project"], store.get_work_order(wo["id"]), "more")

    assert worker_session.compaction_due(store, store.get_work_order(wo["id"]),
                                         BIG) is None
    gate.unlink()
    assert settle_turns(store)


def test_a_compaction_is_never_followed_by_another(fleet, settle_turns):
    """Nothing was added in between, so the second one would summarise a summary — and
    if the first FAILED, repeating it is the loop rather than the fix."""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    ops.send_message(wo["id"], "go on")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    _deliver(fleet, wo["id"])
    assert settle_turns(store)
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)  # cold again

    assert worker_session.compaction_due(store, store.get_work_order(wo["id"]),
                                         BIG) is None
    _deliver(fleet, wo["id"])
    assert _turns(store, wo["id"])[-1] == ("message", "go on")


def test_a_relaunched_turn_is_never_compacted_before(fleet, fake_claude, settle_turns):
    """Two reasons, and the second is why this is not even asked at the decision.

    `_nudge` tells the worker the conversation above is intact, which a compaction
    makes false; and the pause is re-derived from the LATEST turn, so a compact turn
    behind a paused one would erase the pause and strand the relaunch.
    """
    store, daemon = fleet["store"], fleet["daemon"]
    wo = _running_wo(fleet, settle_turns)
    fake_claude.turns_rate_limited()
    worker_session.send(store, fleet["project"], store.get_work_order(wo["id"]), "more")
    assert settle_turns(store)
    fake_claude.turns_recover()
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None
    store.conn.execute("UPDATE wo_turns SET ended_at=? WHERE id=?",
                       (pause.turn["ended_at"] - 3 * usage.WRITE_TTL_SECONDS,
                        pause.turn["id"]))

    daemon.retry_paused_turns(fleet["project"], store)
    assert settle_turns(store)

    assert COMPACT_TURN not in [k for k, _ in _turns(store, wo["id"])]
    # …and the pause survived to be retried at all, which is the half a compaction
    # would have broken.
    assert _turns(store, wo["id"])[-1][0] == "message"


def test_a_message_queued_behind_a_paused_turn_never_compacts(fleet, fake_claude,
                                                              settle_turns):
    """The delivery path, not the retry path — and this is the stall that is permanent.

    `turn_pause` is re-derived from the LATEST turn, so a compact turn inserted behind
    a paused one erases the pause: nothing relaunches the lost turn and the message
    waits for ever. Asserted through `Daemon.deliver_messages` because that is the
    caller that can get here, and then asked of the decision directly, because
    `delivery_hold` doing the right thing is not the same as the decision being safe.
    """
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    fake_claude.turns_rate_limited()
    worker_session.send(store, fleet["project"], store.get_work_order(wo["id"]), "more")
    assert settle_turns(store)
    fake_claude.turns_recover()
    ops.send_message(wo["id"], "and this as well")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    _big_context(store, wo["id"])
    before = worker_session.turn_pause(store, wo["id"])
    assert before is not None

    _deliver(fleet, wo["id"])

    assert COMPACT_TURN not in [k for k, _ in _turns(store, wo["id"])]
    after = worker_session.turn_pause(store, wo["id"])
    assert after is not None and after.turn["id"] == before.turn["id"]
    assert [m["content"] for m in store.queued_messages(wo["id"])] == [
        "and this as well"]
    assert worker_session.compaction_due(store, store.get_work_order(wo["id"]),
                                         BIG) is None


def test_a_pause_nothing_will_relaunch_reaches_the_decision_and_is_refused(
        fleet, settle_turns):
    """The case where nothing upstream rules it out. `delivery_hold` deliberately does
    NOT hold behind a non-resumable pause — the message is the only thing left that can
    start the conversation again — so the delivery pass arrives here with a pause on
    record, past the TTL, over the floor, and only `compaction_due` can refuse it."""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    for _ in range(len(worker_session.TRANSIENT_BACKOFF) + 1):
        turn = store.create_turn(wo["id"], kind="message", prompt="x")
        store.finish_turn(turn["id"], "failed",
                          error="API Error: 500 Internal server error.")
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.exhausted and not pause.resumable
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    _big_context(store, wo["id"])
    fresh = store.get_work_order(wo["id"])
    assert worker_session.delivery_hold(store, fresh) is None, "the premise"
    assert worker_session.compaction_due(store, fresh, BIG) is None
    ops.send_message(wo["id"], "go on")

    _deliver(fleet, wo["id"])

    # The message goes, as it did before this change; what must not be in front of it
    # is a compaction.
    assert _turns(store, wo["id"])[-1] == ("message", "go on")
    assert COMPACT_TURN not in [k for k, _ in _turns(store, wo["id"])]
    assert settle_turns(store)


def test_a_compaction_the_transport_lost_is_re_sent_verbatim(fleet, fake_claude,
                                                             settle_turns):
    """Never the nudge: `/compact` is a command, and prose addressed to a worker
    mid-task would land in the transcript as a user message asking nobody to continue
    nothing."""
    store, daemon = fleet["store"], fleet["daemon"]
    wo = _running_wo(fleet, settle_turns)
    ops.send_message(wo["id"], "go on")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    fake_claude.turns_rate_limited()
    _deliver(fleet, wo["id"])
    assert settle_turns(store)
    fake_claude.turns_recover()
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.turn["kind"] == COMPACT_TURN
    store.conn.execute("UPDATE wo_turns SET ended_at=? WHERE id=?",
                       (pause.turn["ended_at"] - 3600, pause.turn["id"]))

    daemon.retry_paused_turns(fleet["project"], store)

    assert _turns(store, wo["id"])[-1] == (COMPACT_TURN, claude_cli.COMPACT_PROMPT)


def test_a_compaction_that_cannot_be_launched_never_costs_the_message(
        fleet, settle_turns, monkeypatch):
    """The boundary was going to be paid for anyway; a work order that stops moving is
    strictly worse than paying it."""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    ops.send_message(wo["id"], "go on")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    monkeypatch.setattr(worker_session, "compact",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))

    _deliver(fleet, wo["id"])

    assert _turns(store, wo["id"])[-1] == ("message", "go on")


# -- what the record and the bill say --------------------------------------------------


def test_the_summary_is_not_recorded_as_the_workers_reply(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    before = len(store.agent_replies(wo["id"]))
    ops.send_message(wo["id"], "go on")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    _deliver(fleet, wo["id"])
    assert settle_turns(store)

    assert len(store.agent_replies(wo["id"])) == before
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "compacting" in kinds and "compacted" in kinds


def test_the_compactions_own_cost_lands_on_the_bill(fleet, settle_turns):
    """It writes no assistant message, so `usage.read_session` cannot see it. If it is
    not recorded here the saving is reported GROSS — the one thing this must not do."""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    ops.send_message(wo["id"], "go on")
    _age_last_turn(store, wo["id"], usage.WRITE_TTL_SECONDS + 60)
    _deliver(fleet, wo["id"])
    assert settle_turns(store)

    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        kinds = [c["kind"] for c in central.agent_calls(wo_id=wo["id"])]
    finally:
        central.close()
    assert "compaction" in kinds


# -- the accounting, which is where a relabelling would hide ---------------------------


def _rows(*, write: int, read: int, at: str, mid: str) -> dict:
    return {"type": "assistant", "timestamp": at,
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 2,
                                  "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": read,
                                  "output_tokens": 10}}}


def _compact_row(at: str, pre: int = 293_382, post: int = 4_697) -> dict:
    return {"type": "system", "subtype": "compact_boundary", "timestamp": at,
            "compactMetadata": {"trigger": "manual", "preTokens": pre,
                                "postTokens": post}}


COMPACTED = [
    _rows(write=200_000, read=0, at="2026-09-18T10:00:00.000Z", mid="a"),
    _rows(write=3_000, read=200_000, at="2026-09-18T10:01:00.000Z", mid="b"),
    _compact_row("2026-09-18T10:30:00.000Z"),
    # The call after a compaction: seconds later, static head served, summary written.
    _rows(write=15_380, read=12_776, at="2026-09-18T10:30:20.000Z", mid="c"),
]


def test_the_turn_after_a_compaction_is_not_a_prefix_miss(transcripts):
    """By gap and read alone it is one — which is exactly why it is tested first.

    Counting it as prefix invalidation would report the remedy as the defect, and
    `invariants.check_prefix_stable` would read a fleet whose prefix had not moved as
    drifting.
    """
    transcripts("s-compact", COMPACTED)
    total = usage.read_session("s-compact", FLOOR).total

    assert total.rewrite_compact_write == 15_380 and total.boundaries_compact == 1
    assert total.rewrite_prefix_write == 0
    assert total.rewrite_ttl_write == 0

    # …and the same boundary WITHOUT the compaction row is a prefix miss, so the test
    # above is about the marker and not about the numbers.
    transcripts("s-plain", [r for r in COMPACTED if r.get("type") == "assistant"])
    plain = usage.read_session("s-plain", FLOOR).total
    assert plain.rewrite_prefix_write == 15_380 and plain.boundaries_compact == 0


def test_inspect_labels_the_write_a_compaction_and_raises_no_alarm_for_it(transcripts):
    transcripts("s-compact", COMPACTED)
    calls = usage.session_calls("s-compact")
    stamps = usage.compaction_stamps(
        next(iter(usage.index_sessions()["s-compact"])))

    causes = [w.cause for w in inspection.classify_writes(calls, 10_000, stamps)]

    assert causes == [inspection.COLD_START, inspection.COMPACTION]
    # Without the marker the same write is misread — as `ttl-expiry` here and as
    # prefix invalidation in `usage`, because the compaction call writes no assistant
    # message and so the gap the two measure spans the whole idle stretch. Two
    # classifiers, two wrong answers, one marker: that is why it is read from the
    # transcript rather than inferred from the numbers.
    assert inspection.classify_writes(calls, 10_000)[1].cause == inspection.TTL_EXPIRY


def test_a_compacted_boundary_is_not_counted_as_a_prefix_one_on_the_seal():
    """`prefix_boundaries` is a subtraction, so a third cause has to be taken out of it
    or every compaction reads as prefix drift."""
    writes = bill.CacheWrites(boundaries=10, ttl_boundaries=3, compact_boundaries=4)

    assert writes.prefix_boundaries == 3


def test_the_floor_has_a_floor_and_null_is_the_only_off_switch(tmp_path):
    def catalog(value) -> dict:
        return {"os": {"compact_min_context": value},
                "projects": [{"name": "p", "path": str(tmp_path)}]}

    def load(value):
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps(catalog(value)))
        return load_catalog(path)

    assert load(None).os.compact_min_context is None
    assert load(120_000).os.compact_min_context == 120_000
    with pytest.raises(CatalogError, match="compact_min_context"):
        load(COMPACT_MIN_CONTEXT_MIN - 1)


def test_the_cohort_script_classifies_boundaries_exactly_as_the_bill_does(transcripts):
    """Two spellings of one classification is how two surfaces come to disagree about
    the same boundary — the rule `scripts/cache_ttl_cohort.py` already follows by
    importing `usage.read_session` outright. This one cannot: it needs the boundaries
    kept apart rather than totalled, and re-reading every transcript a second time to
    get the aggregate costs five minutes (kn-4b2ef07f (3)). So it restates them, and
    this is what holds the restatement to the original.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "compaction_cohort", Path(__file__).resolve().parents[1]
        / "scripts" / "compaction_cohort.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    path = transcripts("s-mixed", COMPACTED + [
        # …and one more turn, cold and uncompacted, so the two other causes are here too
        _rows(write=120_000, read=0, at="2026-09-18T12:00:00.000Z", mid="d"),
        _rows(write=4_000, read=120_000, at="2026-09-18T12:00:30.000Z", mid="e"),
    ])
    stamps = usage.compaction_stamps(path)
    theirs = module._classify(usage.session_calls("s-mixed"), stamps, FLOOR)
    ours = usage.read_session("s-mixed", FLOOR).total

    assert (theirs.cache_write, theirs.rewrite_ttl_write, theirs.rewrite_prefix_write,
            theirs.rewrite_compact_write) == (
        ours.cache_write, ours.rewrite_ttl_write, ours.rewrite_prefix_write,
        ours.rewrite_compact_write)
    assert ours.rewrite_ttl_write == 120_000  # the uncompacted cold boundary
