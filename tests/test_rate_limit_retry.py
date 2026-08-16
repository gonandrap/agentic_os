"""Self-healing after the Claude usage limit.

A turn Claude Code refuses because the account's window is spent did not happen: no API
call, no cost, and the conversation is exactly where it was. Before this, the OS settled
it as `failed` and waited for a human to come back after the reset and retry by hand.

Three layers, tested separately because they fail separately:
  * `claude_cli.usage_limit` — reading the refusal, and refusing to read anything else
  * `worker_session.rate_limit_pause` — the state, re-derived from the last turn
  * the daemon — pausing instead of failing, relaunching when the window reopens

The end-to-end test is the one that matters: refuse a turn, reopen the window, tick, and
the work order is working again without anyone touching it.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis import claude_cli, invariants, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

#: The refusal exactly as it came back on wo-2fa7c0e9 turn 4 — the work order that
#: prompted this feature. Middle dot and all: it is what the parser must read.
LIVE_REFUSAL = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"


# -- reading the refusal -------------------------------------------------------------


def test_reads_the_live_refusal():
    limit = claude_cli.usage_limit(LIVE_REFUSAL)
    assert limit is not None
    assert limit.reset_at is not None
    when = datetime.fromtimestamp(limit.reset_at, ZoneInfo("America/Los_Angeles"))
    assert (when.hour, when.minute) == (23, 50)
    assert limit.reset_at > time.time()


def test_resolves_the_next_occurrence_of_the_clock_time():
    """No year and no date: the moment is the NEXT time that clock reads so, which is
    what a human does with it, and the difference between waiting minutes and a day."""
    tz = ZoneInfo("America/Los_Angeles")
    # 9pm local: 11:50pm is still ahead, so it is tonight.
    evening = datetime(2026, 8, 10, 21, 0, tzinfo=tz).timestamp()
    tonight = claude_cli.usage_limit(LIVE_REFUSAL, now=evening)
    assert tonight is not None and tonight.reset_at is not None
    assert tonight.reset_at - evening == pytest.approx(2 * 3600 + 50 * 60, abs=1)
    # 11:55pm: 11:50 has gone, so it is tomorrow's.
    late = datetime(2026, 8, 10, 23, 55, tzinfo=tz).timestamp()
    tomorrow = claude_cli.usage_limit(LIVE_REFUSAL, now=late)
    assert tomorrow is not None and tomorrow.reset_at is not None
    assert tomorrow.reset_at - late == pytest.approx(23 * 3600 + 55 * 60, abs=1)


def test_reads_the_epoch_shape():
    """The other shape Claude Code emits states the moment outright."""
    limit = claude_cli.usage_limit("Claude AI usage limit reached|1786344785")
    assert limit is not None and limit.reset_at == 1786344785.0
    ms = claude_cli.usage_limit("Claude AI usage limit reached|1786344785000")
    assert ms is not None and ms.reset_at == 1786344785.0


#: EVERY rate-limit label Claude Code can put in this message, read out of the CLI's own
#: string table (2.1.226, the `HUt` map) rather than guessed at. The message is
#: assembled as `You've hit your ${label} · resets ${when}`, so the label is the only
#: part that varies by which window was spent — and two of these are MODEL names, which
#: is exactly why the matcher keys on the shape and not on the words.
LIMIT_LABELS = [
    ("five_hour", "session limit"),
    ("seven_day", "weekly limit"),
    ("seven_day_opus", "Opus limit"),
    ("seven_day_sonnet", "Sonnet limit"),
    ("seven_day_overage_included", "Fable 5 limit"),
    ("overage", "usage credit limit"),
]


@pytest.mark.parametrize("kind,label", LIMIT_LABELS, ids=[k for k, _ in LIMIT_LABELS])
def test_every_limit_type_is_recognised(kind, label):
    """The 5-hour window, the 7-day window, the per-model ones and usage credits. An
    enumeration of the WORDS would have to grow with the model line-up; this must not."""
    limit = claude_cli.usage_limit(
        f"You've hit your {label} · resets 11:50pm (America/Los_Angeles)")
    assert limit is not None, f"{kind} ({label!r}) was not recognised as a usage limit"
    assert limit.reset_at is not None


def test_an_unreleased_model_name_is_recognised_too():
    """The label carries a model name, so the next one has not been written yet. Keying
    on 'limit … resets' rather than on the name is what makes that a non-event."""
    limit = claude_cli.usage_limit(
        "You've hit your Quintessence 9 limit · resets 11:50pm (America/Los_Angeles)")
    assert limit is not None and limit.reset_at is not None


def test_reads_a_dated_reset():
    """Over 24h away the CLI switches to `toLocaleString("en-US", {month:"short",
    day:"numeric", hour:"numeric", minute:"2-digit", hour12:true})`, which is the
    ordinary rendering for the 7-day window."""
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 10, 12, 0, tzinfo=tz).timestamp()
    limit = claude_cli.usage_limit(
        "You've hit your weekly limit · resets Aug 14, 9:50am (America/New_York)",
        now=now)
    assert limit is not None and limit.reset_at is not None
    when = datetime.fromtimestamp(limit.reset_at, tz)
    assert (when.month, when.day, when.hour, when.minute) == (8, 14, 9, 50)


def test_reads_a_dated_reset_with_the_minutes_dropped():
    """The formatter omits minutes when they are zero, in both renderings."""
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 10, 12, 0, tzinfo=tz).timestamp()
    limit = claude_cli.usage_limit(
        "You've hit your weekly limit · resets Aug 14, 9am (America/New_York)", now=now)
    assert limit is not None and limit.reset_at is not None
    when = datetime.fromtimestamp(limit.reset_at, tz)
    assert (when.month, when.day, when.hour, when.minute) == (8, 14, 9, 0)


def test_reads_a_reset_that_crosses_the_year():
    """The formatter adds the year only when the reset lands in a different one — a
    7-day window spent in late December. Read it, do not skip it: an unread year parses
    as the HOUR and schedules the retry for tonight instead of next year."""
    tz = ZoneInfo("America/New_York")
    now = datetime(2026, 12, 30, 12, 0, tzinfo=tz).timestamp()
    limit = claude_cli.usage_limit(
        "You've hit your weekly limit · resets Jan 3, 2027, 11:50pm (America/New_York)",
        now=now)
    assert limit is not None and limit.reset_at is not None
    when = datetime.fromtimestamp(limit.reset_at, tz)
    assert (when.year, when.month, when.day, when.hour) == (2027, 1, 3, 23)


def test_reads_a_relative_reset():
    """Fast mode renders a duration instead of a clock: "· resets in 2h 15m"."""
    now = 1_800_000_000.0
    limit = claude_cli.usage_limit("You've hit your fast limit · resets in 2h 15m",
                                   now=now)
    assert limit is not None
    assert limit.reset_at == now + 2 * 3600 + 15 * 60
    day = claude_cli.usage_limit("You've hit your weekly limit · resets in 1d 3h",
                                 now=now)
    assert day is not None and day.reset_at == now + 86400 + 3 * 3600


@pytest.mark.parametrize("message", [
    # A spend cap does not reopen on its own — the suffix says so, offering an action
    # instead of a reset. Retrying it would spin until the cap, and the user would never
    # be told the one thing they need to know.
    "You've hit your monthly spend limit. /model to switch models.",
    "You've hit your individual spend limit · run /usage-credits to raise it",
    "You've hit your org's monthly spend limit · ask your admin for a higher limit",
])
def test_a_spend_cap_is_left_for_the_user(message):
    assert claude_cli.usage_limit(message) is None


def test_reads_a_reset_with_no_timezone_against_the_local_clock():
    limit = claude_cli.usage_limit("5-hour limit reached ∙ resets at 3am")
    assert limit is not None and limit.reset_at is not None
    assert time.localtime(limit.reset_at).tm_hour == 3


@pytest.mark.parametrize("error", [
    "",
    None,
    "turn reported is_error",
    "model call failed",
    "the turn's process ended without writing a result",
    "cancelled",
    # Prose ABOUT limits, which is what a worker's own stderr tail looks like. Without
    # a reset clause this is not the CLI refusing a turn, and retrying it for ever is
    # exactly the failure mode a loose matcher would cause.
    "TypeError: session limit must be an int, see docs/limits.md",
    "Traceback: usage limit exceeded in customer code",
    # NEAR MISSES. Matching by shape rather than by wording widened the net, so these
    # pin where the edge now is: the word and a reset in the same breath are not enough,
    # a readable MOMENT has to follow. Without that gate the first of these — a worker
    # talking about this very feature — would park its own work order for an hour.
    "the rate limit reset logic in daemon.py:942 returns early",
    "AssertionError: limit reset failed",
    "429 Too Many Requests: quota limit exceeded; reset window unknown",
    "RateLimitError: limit 40000 tokens, resets per minute",
])
def test_ordinary_failures_are_not_usage_limits(error):
    assert claude_cli.usage_limit(error) is None


def test_a_limit_with_an_unreadable_time_is_still_a_limit():
    """Recognised, but with no moment to wait for — the caller falls back to a delay
    rather than inventing one."""
    limit = claude_cli.usage_limit("You've hit your session limit · resets 99pm")
    assert limit is not None and limit.reset_at is None


def test_an_unknown_timezone_falls_back_to_local_rather_than_failing():
    limit = claude_cli.usage_limit(
        "You've hit your session limit · resets 3am (Mars/Olympus_Mons)")
    assert limit is not None and limit.reset_at is not None
    assert time.localtime(limit.reset_at).tm_hour == 3


# -- the pause, re-derived from the conversation --------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path)
    s.create_work_order("a work order", origin="manual", wo_id="wo-test")
    yield s
    s.close()


def _refused(store, wo_id="wo-test", error=LIVE_REFUSAL, kind="message"):
    turn = store.create_turn(wo_id, kind=kind, prompt="do the thing")
    return store.finish_turn(turn["id"], "failed", error=error)


def test_pause_is_none_for_a_healthy_or_ordinarily_broken_conversation(store):
    assert worker_session.rate_limit_pause(store, "wo-test") is None
    turn = store.create_turn("wo-test", kind="dispatch", prompt="go")
    store.finish_turn(turn["id"], "done", result="did it")
    assert worker_session.rate_limit_pause(store, "wo-test") is None
    _refused(store, error="turn reported is_error")
    assert worker_session.rate_limit_pause(store, "wo-test") is None


def test_pause_carries_the_reset_and_never_retries_instantly(store):
    """The reset is a clock time rounded to the minute, so it can already be in the
    past the moment it is parsed. The floor is what stops the first retry going out
    into the same refusal."""
    _refused(store, error="Claude AI usage limit reached|1000000000")  # long past
    pause = worker_session.rate_limit_pause(store, "wo-test")
    assert pause is not None
    assert pause.reset_at == 1000000000.0
    assert pause.retry_at >= pause.turn["ended_at"] + worker_session.RATE_LIMIT_MIN_DELAY
    assert not pause.due()


def test_a_limit_with_no_readable_time_waits_the_fallback_delay(store):
    _refused(store, error="You've hit your session limit · resets 99pm")
    pause = worker_session.rate_limit_pause(store, "wo-test")
    assert pause is not None and pause.reset_at is None
    assert pause.retry_at == pytest.approx(
        pause.turn["ended_at"] + worker_session.RATE_LIMIT_FALLBACK_DELAY, abs=1)


def test_the_streak_counts_off_the_end_and_resets_when_a_turn_gets_through(store):
    for _ in range(3):
        _refused(store)
    assert worker_session.rate_limit_streak(store, "wo-test") == 3
    assert worker_session.rate_limit_pause(store, "wo-test").attempts == 3
    done = store.create_turn("wo-test", kind="message", prompt="ok")
    store.finish_turn(done["id"], "done", result="through")
    assert worker_session.rate_limit_streak(store, "wo-test") == 0
    _refused(store)
    assert worker_session.rate_limit_streak(store, "wo-test") == 1


def test_the_streak_is_counted_off_the_tail_of_a_long_conversation(store):
    """`list_turns` is capped at its LIMIT from the FRONT, so a hundred healthy turns
    would hide the refusals at the end from anything that used it."""
    for i in range(120):
        turn = store.create_turn("wo-test", kind="message", prompt=f"turn {i}")
        store.finish_turn(turn["id"], "done", result="fine")
    _refused(store)
    assert worker_session.rate_limit_streak(store, "wo-test") == 1


def test_retries_run_out(store):
    for _ in range(worker_session.MAX_RATE_LIMIT_RETRIES + 1):
        _refused(store)
    assert worker_session.rate_limit_pause(store, "wo-test").exhausted


# -- what the user is told -------------------------------------------------------------


def test_the_label_says_why_nothing_is_moving_and_when_it_will(store):
    store.set_status("wo-test", "running")
    _refused(store)
    wo = store.get_work_order("wo-test")
    note = invariants.rate_limit_note(store, wo)
    assert "usage limit" in note and "retrying by itself" in note
    assert invariants.status_label(store, wo).startswith("running — ")


def test_a_healthy_running_work_order_gets_the_bare_status(store):
    store.set_status("wo-test", "running")
    assert invariants.status_label(store, store.get_work_order("wo-test")) == "running"


def test_an_exhausted_pause_stops_claiming_it_will_retry(store):
    store.set_status("wo-test", "running")
    for _ in range(worker_session.MAX_RATE_LIMIT_RETRIES + 1):
        _refused(store)
    assert invariants.rate_limit_note(store, store.get_work_order("wo-test")) == ""


# -- end to end ------------------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    """OS started, with the retry floor removed so a test does not wait a minute for it.

    The floor is a real guard against retrying into the same refusal; it is not what
    these tests are about, and every other clock here is the daemon's own.
    """
    monkeypatch.setattr(worker_session, "RATE_LIMIT_MIN_DELAY", 0)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _tick(daemon):
    """One tick with the rate-limit pass ON — the daemon runs it every 12th tick."""
    daemon.tick_count = 0
    daemon.tick()


def test_a_refused_work_order_resumes_itself(started, fake_claude, project,
                                             settle_turns):
    """THE POINT OF THE WHOLE FEATURE. A work order refused for the usage limit is
    never failed, never flagged, and is working again on the first pass after the
    window reopens — with nobody typing anything."""
    daemon = started
    fake_claude.turns_rate_limited(reset="12:01am (UTC)")
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)

    _tick(daemon)                      # dispatches turn 1, which is refused
    assert settle_turns(store), "the refused turn never settled"
    _tick(daemon)                      # settles it

    row = store.get_work_order(wo["id"])
    assert row["status"] != "failed", "a refused turn must not fail the work order"
    assert not row["needs_attention"], "the OS fixes this one itself; do not ask the user"
    assert store.latest_turn(wo["id"])["state"] == "failed"
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "rate_limited" in kinds and "turn_failed" not in kinds

    # The window reopens. The refusal named a moment hours away, so the test moves the
    # clock the only way it can from out here: by rewriting the refusal to one whose
    # reset is already behind us. The daemon re-reads the error every pass, so this is
    # the same code path a real reset takes, just without the wait.
    fake_claude.turns_recover()
    store.conn.execute("UPDATE wo_turns SET error=? WHERE id=?",
                       ("Claude AI usage limit reached|1000000000",
                        store.latest_turn(wo["id"])["id"]))

    _tick(daemon)                      # the retry pass relaunches it
    assert settle_turns(store), "the retried turn never ran"
    _tick(daemon)

    events = [e["kind"] for e in store.list_events(wo["id"])]
    assert "rate_limit_retry" in events
    # The OPENING turn was refused before a transcript existed, so the retry has to
    # re-open the session rather than resume one that was never written — and under the
    # SAME id, which is immutable for the work order's whole life.
    opening = [c["argv"] for c in fake_claude.calls
               if "-p" in c["argv"] and "--session-id" in c["argv"]]
    assert len(opening) == 2, "the retry of an opening turn must not use --resume"
    assert ({argv[argv.index("--session-id") + 1] for argv in opening}
            == {row["session_id"]}), "the retry must reuse the work order's session id"
    assert store.latest_turn(wo["id"])["state"] == "done"
    # The pause is gone because the turn got through — there is no flag to clear and
    # nothing to reset, which is the whole point of deriving it from the last turn.
    assert worker_session.rate_limit_pause(store, wo["id"]) is None
    # And it settled the ordinary way. `needs_review` is what any turn that ends
    # without `jarvis wo finish` settles to; what matters here is that the usage limit
    # left no trace on where it ended up.
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    store.close()


def test_the_retry_re_sends_the_prompt_the_worker_never_saw(started, fake_claude,
                                                            project, settle_turns):
    """A message is marked `delivered` the instant its turn starts, so the turn row is
    the only surviving copy. Losing it would lose what the user actually said."""
    daemon = started
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)

    fake_claude.turns_rate_limited()
    ops.send_message(wo["id"], "please also update the changelog")
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    refused = store.latest_turn(wo["id"])
    assert refused["state"] == "failed"
    assert refused["prompt"] == "please also update the changelog"

    fake_claude.turns_recover()
    store.conn.execute("UPDATE wo_turns SET error=? WHERE id=?",
                       ("Claude AI usage limit reached|1000000000", refused["id"]))
    _tick(daemon)
    assert settle_turns(store)
    sent = store.latest_turn(wo["id"])
    assert sent["seq"] == refused["seq"] + 1
    assert sent["prompt"] == "please also update the changelog", \
        "the retry must re-send what the worker never got to read"
    store.close()


def test_a_newer_message_waits_for_the_refused_one(started, fake_claude, project,
                                                   settle_turns):
    """Delivery is held while paused, so the refused turn goes out first and the
    conversation keeps its order. Held, never dropped."""
    daemon = started
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)

    fake_claude.turns_rate_limited()
    ops.send_message(wo["id"], "first thing")
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    refused_seq = store.latest_turn(wo["id"])["seq"]

    ops.send_message(wo["id"], "second thing")
    _tick(daemon)   # delivery must NOT jump the queue while the pause stands
    assert store.latest_turn(wo["id"])["seq"] == refused_seq
    assert [m["status"] for m in store.list_messages(wo["id"])
            if m["content"] == "second thing"] == ["queued"]
    store.close()


def test_retrying_stops_and_asks_for_the_user_when_it_never_clears(started, fake_claude,
                                                                   project,
                                                                   settle_turns,
                                                                   monkeypatch):
    """A window that never reopens is not self-healing, and pretending otherwise hides
    it behind a work order that looks busy. After the cap it fails for real."""
    monkeypatch.setattr(worker_session, "MAX_RATE_LIMIT_RETRIES", 1)
    daemon = started
    fake_claude.turns_rate_limited()
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)

    for _ in range(4):
        _tick(daemon)
        settle_turns(store)
        # The reset is always already behind us, so every pass is due.
        turn = store.latest_turn(wo["id"])
        if turn and turn["state"] == "failed":
            store.conn.execute("UPDATE wo_turns SET error=? WHERE id=?",
                               ("Claude AI usage limit reached|1000000000", turn["id"]))
        _tick(daemon)

    row = store.get_work_order(wo["id"])
    assert row["status"] == "failed"
    assert row["needs_attention"]
    assert "rate_limit_exhausted" in [e["kind"] for e in store.list_events(wo["id"])]
    store.close()


def test_an_exhausted_work_order_still_takes_a_message(started, fake_claude, project,
                                                       settle_turns, monkeypatch):
    """The manual escape hatch the user has always had — `jarvis wo send … "retry"` —
    must survive the pause, or the feature replaces one stuck state with another."""
    monkeypatch.setattr(worker_session, "MAX_RATE_LIMIT_RETRIES", 0)
    daemon = started
    fake_claude.turns_rate_limited()
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    assert store.get_work_order(wo["id"])["status"] == "failed"

    fake_claude.turns_recover()
    ops.send_message(wo["id"], "limit restored, retry")
    _tick(daemon)
    assert settle_turns(store)
    assert store.latest_turn(wo["id"])["prompt"] == "limit restored, retry"
    store.close()
