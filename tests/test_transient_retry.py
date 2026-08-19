"""Self-healing after the Claude API fails — the OTHER way a turn dies innocently.

`wo-4f460495` took `API Error: 500 Internal server error` five hours into a work order.
Nothing in the OS recognised it, so the order settled to `failed` and sat there for
TWENTY-SEVEN HOURS until the user came back and typed "retry" by hand. This is what
stops that.

It is the sibling of `test_rate_limit_retry.py`, and the two differ in one way that
drives every design decision here:

    usage limit    the turn was refused BEFORE it ran. Nothing sent, nothing billed,
                   and the refusal states when the window reopens.
    API error      the turn RAN. The prompt is in the conversation, work may be done and
                   paid for, and nothing says when the API will be well again.

So this half needs a backoff rather than a stated moment, and a retry that tells the
worker to carry on rather than re-sending a prompt it has already acted on.

Four layers, tested separately because they fail separately:
  * `claude_cli.transient_failure` — reading the failure, and refusing to read anything
    else (a 429 above all, which is a spend cap the user has to clear)
  * `worker_session.turn_pause` — the backoff, re-derived from the last turn
  * `worker_session.retry` — nudge or verbatim, decided from what the turn actually did
  * the daemon — pausing instead of failing, and relaunching on the tick
"""

from __future__ import annotations

import json

import pytest

from jarvis import claude_cli, invariants, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

#: The failure exactly as it came back on wo-4f460495 turn 2 — the work order that
#: prompted this feature. Em dash and all.
LIVE_500 = ("API Error: 500 Internal server error. This is a server-side issue, "
            "usually temporary — try again in a moment. If it persists, check "
            "https://status.claude.com.")

#: What the CLI assembles when it has retried a 529 to exhaustion. No HTTP status in the
#: envelope for this one, so only the text branch can catch it.
LIVE_529 = ("API Error: Repeated 529 Overloaded errors. The API is at capacity — this "
            "is usually temporary. Try again in a moment. If it persists, check "
            "https://status.claude.com.")

#: And when the socket went away. Also status-less.
LIVE_DROPPED = ("API Error: Connection to the API was lost (ECONNRESET). This is "
                "usually temporary — try again.")


# -- reading the failure ---------------------------------------------------------------


def test_reads_the_live_500_from_the_structured_fields():
    """The whole point of keying on `api_error_status`: no prose is consulted."""
    fail = claude_cli.transient_failure(LIVE_500, terminal_reason="api_error",
                                        api_error_status=500)
    assert fail is not None
    assert fail.status == 500
    assert "Internal server error" in fail.message


@pytest.mark.parametrize("status", [500, 502, 503, 529, 599])
def test_every_server_side_status_is_retriable(status):
    fail = claude_cli.transient_failure(f"API Error: {status} something",
                                        terminal_reason="api_error",
                                        api_error_status=status)
    assert fail is not None and fail.status == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 429])
def test_nothing_below_500_is_retriable(status):
    """429 IS ON THIS LIST ON PURPOSE. It is the usage limit's own code, and the form
    `usage_limit` declines to touch is a SPEND cap, which never reopens by itself.
    Retrying one here would burn five attempts on something only the user can clear."""
    assert claude_cli.transient_failure(f"API Error: {status} nope",
                                        terminal_reason="api_error",
                                        api_error_status=status) is None


def test_a_usage_limit_refusal_is_never_read_as_transient():
    """Belt and braces on the ordering: the limit refusal arrives as an api_error with
    a 429, so both guards have to hold, and this checks the one that does not depend on
    the caller asking in the right order."""
    refusal = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"
    assert claude_cli.transient_failure(refusal, terminal_reason="api_error",
                                        api_error_status=429) is None
    # Even with the status stripped, which is how an old row reaches us.
    assert claude_cli.transient_failure(refusal) is None


def test_an_aborted_turn_is_not_retried():
    """Neo's ruling on question 126: an abort lands mid-tool-use, so replaying it can
    re-run a side effect the work order already had. Three of the fleet's ten recorded
    failures are this shape, so it is the exclusion that matters most."""
    for reason in ("aborted_streaming", "aborted_tools"):
        assert claude_cli.transient_failure(
            "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use",
            terminal_reason=reason) is None


def test_a_reason_the_cli_named_is_believed_over_the_prose():
    """`prompt_too_long` does not get better by being repeated, however the message
    happens to be worded."""
    assert claude_cli.transient_failure("API Error: 500 whatever",
                                        terminal_reason="prompt_too_long") is None


@pytest.mark.parametrize("text", [LIVE_529, LIVE_DROPPED])
def test_the_status_less_shapes_are_caught_by_their_wording(text):
    """These two carry no HTTP status anywhere, so the text branch is the only thing
    that can see them — and the CLI assembles both from a fixed table, so they are
    matched as the literals they are."""
    fail = claude_cli.transient_failure(text)
    assert fail is not None and fail.status is None


def test_the_status_in_the_message_is_read_when_the_envelope_has_none():
    """An old row: reaped before the columns existed, so all that survives is the text
    the CLI printed — which names the status itself."""
    fail = claude_cli.transient_failure(LIVE_500)
    assert fail is not None and fail.status == 500


def test_prose_about_an_api_error_does_not_park_a_work_order():
    """The error text can be a tail of the WORKER'S OWN stderr, and a worker that logs
    about a 500 must not thereby put itself on a retry loop. The `API Error: ` prefix is
    what tells the CLI's own voice from a quotation of it."""
    for text in (
        "the handler returns 500 Internal server error when the upstream is down",
        "TODO: connection to the API was lost — add a retry here",
        "assert resp.status == 503",
        "",
    ):
        assert claude_cli.transient_failure(text) is None


def test_an_ordinary_failure_is_still_an_ordinary_failure():
    assert claude_cli.transient_failure("turn reported is_error") is None
    assert claude_cli.transient_failure(
        "the turn's process ended without writing a result") is None


def test_the_result_envelope_carries_the_fields_through(tmp_path):
    """End of the transport: the two fields have to survive `read_turn_result`, or
    nothing downstream can see them. Shape taken from the live wo-4f460495 file."""
    out = tmp_path / "2.json"
    out.write_text(json.dumps({
        "type": "result", "subtype": "success", "is_error": True,
        "terminal_reason": "api_error", "api_error_status": 500,
        "result": LIVE_500, "session_id": "s", "num_turns": 14,
        "duration_api_ms": 177098, "total_cost_usd": 1.99,
    }))
    result = claude_cli.read_turn_result(out)
    assert result is not None
    assert result.terminal_reason == "api_error"
    assert result.api_error_status == 500


def test_a_missing_or_null_status_reads_as_absent(tmp_path):
    """`api_error_status` is declared nullable AND optional, so both have to land as
    None rather than as a status of some kind."""
    for envelope in ({}, {"api_error_status": None}, {"api_error_status": "500"}):
        out = tmp_path / "t.json"
        out.write_text(json.dumps({"type": "result", "is_error": False,
                                   "result": "fine", **envelope}))
        result = claude_cli.read_turn_result(out)
        assert result is not None and result.api_error_status is None


# -- the backoff, re-derived from the conversation --------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path)
    s.create_work_order("a work order", origin="manual", wo_id="wo-test")
    yield s
    s.close()


def _broken(store, wo_id="wo-test", status=500, error=LIVE_500, kind="message",
            api_ms=177098):
    turn = store.create_turn(wo_id, kind=kind, prompt="do the thing")
    return store.finish_turn(
        turn["id"], "failed", error=error, terminal_reason="api_error",
        api_error_status=status,
        usage_json=json.dumps({"duration_api_ms": api_ms}))


def test_the_backoff_is_the_schedule_that_was_asked_for(store):
    """1, 2, 6, 10, 20 minutes, measured from the moment the turn died — and the fifth
    failure is the last one that schedules anything."""
    assert worker_session.TRANSIENT_BACKOFF == (60, 120, 360, 600, 1200)
    for expected in worker_session.TRANSIENT_BACKOFF:
        turn = _broken(store)
        pause = worker_session.turn_pause(store, "wo-test")
        assert pause is not None and pause.reason == worker_session.PAUSE_TRANSIENT
        assert pause.retry_at == pytest.approx(turn["ended_at"] + expected, abs=1)
        assert not pause.exhausted


def test_it_gives_up_after_five(store):
    """A fault still there after thirty-nine minutes is not going to fix itself, and a
    work order that looks busy for ever is worse than one that asks."""
    for _ in range(len(worker_session.TRANSIENT_BACKOFF)):
        _broken(store)
    last = worker_session.turn_pause(store, "wo-test")
    assert last is not None and not last.exhausted
    _broken(store)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert pause.exhausted and pause.attempts == 6 and pause.max_attempts == 5


def test_the_streak_resets_when_a_turn_gets_through(store):
    _broken(store)
    _broken(store)
    twice = worker_session.turn_pause(store, "wo-test")
    assert twice is not None and twice.attempts == 2
    done = store.create_turn("wo-test", kind="message", prompt="ok")
    store.finish_turn(done["id"], "done", result="through")
    assert worker_session.turn_pause(store, "wo-test") is None
    _broken(store)
    again = worker_session.turn_pause(store, "wo-test")
    assert again is not None and again.attempts == 1


def test_the_two_reasons_do_not_share_a_streak(store):
    """A conversation that hit the usage limit and then hit a 500 is on its FIRST 500.
    Charging it the limit's attempts would cut the backoff short and could even start it
    exhausted, which would fail a work order that had not been retried once."""
    for _ in range(4):
        turn = store.create_turn("wo-test", kind="message", prompt="x")
        store.finish_turn(turn["id"], "failed",
                          error="You've hit your session limit · resets 3am (UTC)")
    _broken(store)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None and pause.reason == worker_session.PAUSE_TRANSIENT
    assert pause.attempts == 1, "the limit's streak must not count against the API's"
    assert not pause.exhausted


def test_an_old_row_with_no_columns_is_still_diagnosed(store):
    """Rows written before the migration have NULL in both columns, so the text branch
    is the only evidence left — and those are exactly the work orders sitting failed
    when this ships."""
    turn = store.create_turn("wo-test", kind="message", prompt="x")
    store.finish_turn(turn["id"], "failed", error=LIVE_500)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None and pause.reason == worker_session.PAUSE_TRANSIENT
    assert pause.status == 500


# -- what a retry actually sends --------------------------------------------------------


def test_reached_model_reads_the_recorded_api_time(store):
    """The signal that decides nudge vs verbatim. A usage-limit refusal records 0 here
    (verified across every one the fleet has), and a turn that got any distance records
    the time it spent."""
    assert worker_session._reached_model(_broken(store, api_ms=177098))
    assert not worker_session._reached_model(_broken(store, api_ms=0))
    # No envelope at all — an old row, or a crashed turn. Falls back to the verbatim
    # re-send, which is the behaviour that was already there.
    bare = store.create_turn("wo-test", kind="message", prompt="x")
    assert not worker_session._reached_model(store.finish_turn(bare["id"], "failed"))


def test_the_nudge_tells_the_worker_to_continue_not_to_restart(store):
    pause = worker_session.TurnPause(
        reason=worker_session.PAUSE_TRANSIENT, turn=_broken(store), retry_at=0,
        attempts=1, message="API Error: 500 Internal server error", status=500)
    nudge = worker_session._nudge(pause)
    assert "Carry on" in nudge and "Do not start again" in nudge
    assert "500" in nudge, "the worker is owed what actually happened"


# -- what the user is told --------------------------------------------------------------


def test_the_label_names_the_error_the_clock_and_the_attempt(store):
    """Unlike a usage window, which reopens once at a stated time, a backoff can be on
    its fourth of five — so "retrying at 14:07" alone would read as a promise the OS
    might not keep."""
    store.set_status("wo-test", "running")
    _broken(store)
    wo = store.get_work_order("wo-test")
    note = invariants.pause_note(store, wo)
    assert "Claude API error 500" in note
    assert "retrying by itself at" in note
    assert "attempt 1 of 5" in note
    assert invariants.status_label(store, wo).startswith("running — ")


def test_an_exhausted_pause_says_nothing(store):
    """Past the cap the work order is genuinely failed, and the reassuring note would
    contradict the attention flag beside it."""
    store.set_status("wo-test", "running")
    for _ in range(len(worker_session.TRANSIENT_BACKOFF) + 1):
        _broken(store)
    assert invariants.pause_note(store, store.get_work_order("wo-test")) == ""


# -- end to end -------------------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _tick(daemon):
    """One tick with the retry pass ON (the daemon runs it every RETRY_EVERY_TICKS)."""
    daemon.tick_count = 0
    daemon.tick()


def _due_now(store, wo_id):
    """Move the clock the only way a test can from out here: age the dead turn until
    its backoff has elapsed.

    The backoff is deliberately NOT flattened to zero instead. At zero the retry becomes
    due in the very tick that settles the failure, so the settle-then-retry sequence a
    real pause goes through collapses into one step and the test can no longer see the
    paused state it exists to check. `turn_pause` recomputes `retry_at` from the row on
    every pass, so ageing the row is the same code path a real wait takes, minus the
    wait.
    """
    turn = store.latest_turn(wo_id)
    store.conn.execute("UPDATE wo_turns SET ended_at=? WHERE id=?",
                       (turn["ended_at"] - max(worker_session.TRANSIENT_BACKOFF) - 1,
                        turn["id"]))
    return turn


def test_a_broken_work_order_resumes_itself(started, fake_claude, project,
                                            settle_turns):
    """THE POINT OF THE WHOLE FEATURE, and the exact shape of wo-4f460495: a turn dies
    on a 500, and the work order is never failed, never flagged, and working again on
    the next pass — with nobody typing "retry"."""
    daemon = started
    fake_claude.turns_api_error(500)
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)

    _tick(daemon)                       # dispatches turn 1, which takes the 500
    assert settle_turns(store), "the broken turn never settled"
    _tick(daemon)                       # settles it

    row = store.get_work_order(wo["id"])
    assert row["status"] != "failed", "a transport failure must not fail the work order"
    assert not row["needs_attention"], "the OS fixes this one itself; do not ask the user"
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "turn_paused" in kinds and "turn_failed" not in kinds
    # The diagnosis was persisted, not just acted on — the result file it came from is
    # pruned by Claude Code on its own schedule.
    failed = store.latest_turn(wo["id"])
    assert failed["terminal_reason"] == "api_error"
    assert failed["api_error_status"] == 500

    fake_claude.turns_recover()
    _due_now(store, wo["id"])
    _tick(daemon)                       # the retry pass relaunches it
    assert settle_turns(store), "the retried turn never ran"
    _tick(daemon)

    events = [e["kind"] for e in store.list_events(wo["id"])]
    assert "turn_resumed" in events
    assert store.latest_turn(wo["id"])["state"] == "done"
    assert worker_session.turn_pause(store, wo["id"]) is None
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    store.close()


def test_a_turn_that_reached_the_model_is_nudged_rather_than_re_sent(
        started, fake_claude, project, settle_turns, monkeypatch, tmp_path):
    """Neo's ruling on question 126, and the reason this is not just the usage-limit
    path with a different clock.

    Verified on the real wo-4f460495: its session transcript holds the prompt followed
    by ~55 messages and $1.99 of work. Re-sending the prompt there would put the same
    user message into the conversation a second time and invite the worker to redo all
    of it.

    The transcript is planted by hand because the fake CLI keeps its own log somewhere
    `claude_cli.session_transcript_path` does not look — so without this the resume
    branch would never be reached and the test would prove the opposite of what it says.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    daemon = started
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)

    fake_claude.turns_api_error(500)
    ops.send_message(wo["id"], "please also update the changelog")
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    broken = store.latest_turn(wo["id"])
    assert broken["state"] == "failed"
    assert broken["prompt"] == "please also update the changelog"

    # The conversation exists on disk, which is what makes this a resume rather than a
    # re-open — and therefore what makes re-sending the prompt a duplicate.
    spec = daemon.catalog.projects[0]
    row = store.get_work_order(wo["id"])
    cwd = worker_session.worktree_path(spec, row) or spec.path
    transcript = claude_cli.session_transcript_path(cwd, row["session_id"])
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("{}\n")

    fake_claude.turns_recover()
    _due_now(store, wo["id"])
    _tick(daemon)
    sent = store.latest_turn(wo["id"])
    assert sent["seq"] == broken["seq"] + 1
    assert sent["prompt"] != "please also update the changelog", \
        "the worker already read that message and acted on it; re-sending duplicates it"
    assert "cut short" in sent["prompt"] and "Do not start again" in sent["prompt"]
    store.close()


def test_a_turn_that_never_ran_still_gets_its_prompt_verbatim(started, fake_claude,
                                                              project, settle_turns):
    """The other half of the same ruling. A message is marked `delivered` the instant
    its turn starts, so the turn row is the only surviving copy of what the user said —
    and when the turn never reached the model, the worker has still never seen it."""
    daemon = started
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)

    fake_claude.turns_rate_limited()   # refuses BEFORE the turn runs: 0ms of API time
    ops.send_message(wo["id"], "please also update the changelog")
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    refused = store.latest_turn(wo["id"])

    fake_claude.turns_recover()
    store.conn.execute("UPDATE wo_turns SET error=? WHERE id=?",
                       ("Claude AI usage limit reached|1000000000", refused["id"]))
    _tick(daemon)
    sent = store.latest_turn(wo["id"])
    assert sent["prompt"] == "please also update the changelog", \
        "a turn that never ran must re-send what the worker never got to read"
    store.close()


def test_a_newer_message_waits_for_the_broken_one(started, fake_claude, project,
                                                  settle_turns):
    """Delivery is held while paused, so the lost turn goes out first and the
    conversation keeps its order. Held, never dropped."""
    daemon = started
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)

    fake_claude.turns_api_error(500)
    ops.send_message(wo["id"], "first thing")
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    broken_seq = store.latest_turn(wo["id"])["seq"]

    ops.send_message(wo["id"], "second thing")
    _tick(daemon)   # delivery must NOT jump the queue while the pause stands
    assert store.latest_turn(wo["id"])["seq"] == broken_seq
    assert [m["status"] for m in store.list_messages(wo["id"])
            if m["content"] == "second thing"] == ["queued"]
    store.close()


def test_it_stops_and_asks_for_the_user_when_the_api_never_comes_back(
        started, fake_claude, project, settle_turns):
    """An outage is not self-healing, and pretending otherwise hides it behind a work
    order that looks busy. After the cap it fails for real and says why."""
    daemon = started
    fake_claude.turns_api_error(500)
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)

    for _ in range(len(worker_session.TRANSIENT_BACKOFF) + 3):
        _tick(daemon)
        settle_turns(store)
        turn = store.latest_turn(wo["id"])
        if turn and turn["state"] == "failed":
            _due_now(store, wo["id"])   # every pass is due; only the cap stops it
        _tick(daemon)

    row = store.get_work_order(wo["id"])
    assert row["status"] == "failed"
    assert row["needs_attention"]
    events = [e for e in store.list_events(wo["id"])
              if e["kind"] == "turn_retries_exhausted"]
    assert events, "the user is owed the fact that it was retried, not just that it failed"
    store.close()


def test_a_429_never_takes_the_transient_path(started, fake_claude, project,
                                              settle_turns):
    """The spend-cap guard, end to end. A 429 whose message names no reset is a cap the
    user has to clear; it must fail and ask, not back off five times and then ask."""
    daemon = started
    fake_claude.turns_api_error(429)
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)

    assert worker_session.turn_pause(store, wo["id"]) is None
    row = store.get_work_order(wo["id"])
    assert row["status"] == "failed" and row["needs_attention"]
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "turn_failed" in kinds and "turn_paused" not in kinds
    store.close()
