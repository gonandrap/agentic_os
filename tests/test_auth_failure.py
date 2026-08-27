"""The turn that died and would not say why — and the third pause reason it earned.

On 2026-08-27 three work orders (wo-c2793bf0, wo-5def741d, wo-2df8828c) settled to
`failed` and told the user, in the timeline and in Telegram, only this:

    the turn's process ended without writing a result

Every one of them had really said "Failed to authenticate: OAuth session expired and
could not be refreshed", in its own session transcript, as a `<synthetic>` assistant
message — the one place `claude -p` writes when it exits with neither a result JSON nor
a byte of stderr. The OS already opened that file for money (`bill.py`) and never for
diagnosis.

Two things are under test, and they fail separately:

  * RECOVERY — `worker_session._transcript_error`, which goes and reads it, and the
    time window that keeps it reading THIS turn's words rather than the next turn's.
  * CLASSIFICATION — `claude_cli.auth_failure` and `PAUSE_AUTH`, whose whole job is to
    stop the OS backing off five times against an account that cannot answer any of
    them, and to put the real sentence in front of the user instead (Neo, question 167).
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from jarvis import claude_cli, invariants, notify, ops, timeline, usage, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

#: Verbatim from wo-c2793bf0's transcript, 2026-08-27T04:15:05Z.
LIVE_OAUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"

#: The other two wordings the same incident produced, from wo-2df8828c.
LIVE_LOGIN = "Login expired · Please run /login"


# -- reading the failure ---------------------------------------------------------------


@pytest.mark.parametrize("text", [
    LIVE_OAUTH,
    LIVE_LOGIN,
    "OAuth token expired",
    "Invalid API key · Please run /login",
    "API Error: 401 Unauthorized",
])
def test_every_wording_the_incident_produced_is_recognised(text):
    fail = claude_cli.auth_failure(text)
    assert fail is not None and fail.message


def test_prose_that_merely_mentions_an_auth_failure_is_not_one():
    """The false positive that matters, and it is not hypothetical HERE: this repo's
    own workers write about this exact failure, and one of them dying for an unrelated
    reason must not be filed as an auth failure because its last words quoted one."""
    assert claude_cli.auth_failure(
        "I checked whether it could have been 'Failed to authenticate' and it was not"
    ) is None
    assert claude_cli.auth_failure("nothing to see here") is None
    assert claude_cli.auth_failure(None) is None


def test_an_auth_failure_is_never_read_as_transient_or_as_the_usage_limit():
    """`_diagnose` asks auth first for one reason: a backoff against an expired login
    spends five attempts to learn what the first one already said."""
    assert claude_cli.transient_failure(LIVE_OAUTH, terminal_reason="api_error",
                                        api_error_status=None) is None
    assert claude_cli.usage_limit(LIVE_OAUTH) is None


# -- recovering it from the transcript --------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path)
    s.create_work_order("a work order", origin="manual", wo_id="wo-test",
                        session_id="sess-1")
    yield s
    s.close()


@pytest.fixture()
def transcript(tmp_path, monkeypatch):
    """Write assistant rows into a fake `~/.claude/projects` tree, as Claude Code does."""
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[tuple[str, str, float]]) -> None:
        (root / "-proj" / f"{session_id}.jsonl").write_text("".join(
            json.dumps({
                "type": "assistant",
                "timestamp": _stamp(at),
                "message": {"id": f"m{i}", "model": model,
                            "content": [{"type": "text", "text": text}]},
            }) + "\n"
            for i, (model, text, at) in enumerate(rows)))

    return write


def _stamp(at: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _died(store, started_at: float, ended_at: float):
    """A turn whose process wrote no result and no stderr — the incident's shape."""
    turn = store.create_turn("wo-test", kind="dispatch", prompt="ship the thing")
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (started_at, turn["id"]))
    store.conn.commit()
    return dict(store.get_turn(turn["id"]), ended_at=ended_at)


def test_the_real_reason_is_recovered_from_the_transcript(store, transcript):
    transcript("sess-1", [("<synthetic>", LIVE_OAUTH, 1000.0)])
    turn = _died(store, 990.0, 1010.0)
    assert worker_session._transcript_error(store, "wo-test", turn) == LIVE_OAUTH


def test_it_reads_this_turn_s_words_and_not_the_next_turn_s(store, transcript):
    """THE TRAP wo-c2793bf0 SETS. Its transcript holds the auth refusal that killed
    turn 1 and, eight minutes later, an unrelated "No response requested." synthetic
    from turn 2. The last synthetic in the FILE is the wrong answer."""
    transcript("sess-1", [("<synthetic>", LIVE_OAUTH, 1000.0),
                          ("<synthetic>", "No response requested.", 1500.0)])
    assert worker_session._transcript_error(
        store, "wo-test", _died(store, 990.0, 1010.0)) == LIVE_OAUTH


def test_the_worker_s_own_last_words_are_context_not_a_diagnosis(store, transcript):
    """Only `<synthetic>` is the CLI speaking. A worker's prose is shown — it beats
    nothing — but wrapped, so `_diagnose` cannot mistake it for a verdict."""
    transcript("sess-1", [("claude-opus-5", "running the test suite now", 1000.0)])
    error = worker_session._transcript_error(store, "wo-test", _died(store, 990.0, 1010.0))
    assert error.startswith(worker_session.NO_RESULT)
    assert "running the test suite now" in error


def test_no_transcript_means_no_claim(store, transcript):
    assert worker_session._transcript_error(
        store, "wo-test", _died(store, 990.0, 1010.0)) == ""


# -- the classification -----------------------------------------------------------------


def _auth_turn(store, error=LIVE_OAUTH):
    turn = store.create_turn("wo-test", kind="message", prompt="do the thing")
    return store.finish_turn(turn["id"], "failed", error=error)


def test_an_auth_failure_is_a_pause_with_the_real_string_on_it(store):
    _auth_turn(store)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert pause.reason == worker_session.PAUSE_AUTH
    assert "OAuth session expired" in pause.message


def test_it_never_exhausts_however_long_the_sign_in_takes(store):
    """`failed` is a DEPENDENCY_DEAD_STATUS: exhausting an auth pause strands the order's
    dependents and fails its parent feature order — what happened to fo-e353491c — for
    something a `/login` fixes. So the one thing this pause can never become is spent
    (Neo, question 169, reversing 167)."""
    _auth_turn(store)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert not pause.exhausted
    assert not replace(pause, attempts=99).exhausted


def test_with_no_sign_in_it_is_parked_rather_than_waiting(store):
    """`resumable` is the distinction `due()` cannot draw: not yet, versus not ever."""
    _auth_turn(store)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert pause.retry_at == worker_session.NEVER and not pause.resumable
    store.set_status("wo-test", "running")
    assert "sign in" in invariants.pause_note(store, store.get_work_order("wo-test")), \
        "no clock to promise, but the action that ends it is worth naming"


def test_the_streak_counts_auth_turns_apart_from_the_others(store):
    """A conversation that hit the usage limit and then lost its login is on its FIRST
    auth failure, not its fourth of anything."""
    limit = store.create_turn("wo-test", kind="message", prompt="x")
    store.finish_turn(limit["id"], "failed",
                      error="You've hit your session limit · resets 11:40am")
    _auth_turn(store)
    assert worker_session.pause_streak(
        store, "wo-test", worker_session.PAUSE_AUTH) == 1
    assert worker_session.pause_streak(
        store, "wo-test", worker_session.PAUSE_USAGE_LIMIT) == 0


def test_the_timeline_names_auth_rather_than_a_generic_failure():
    label, detail = timeline._describe("turn_paused", {"reason": "auth",
                                                       "error": LIVE_OAUTH})
    assert "sign-in" in label.lower()
    assert "OAuth session expired" in detail and "sign in again" in detail


# -- end to end -------------------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _tick(daemon):
    daemon.tick_count = 0
    daemon.tick()


def test_the_user_is_told_what_actually_happened(started, fake_claude, project,
                                                 settle_turns):
    """THE POINT OF THE WHOLE ORDER. A turn dies exactly as the incident's did — no
    result, no stderr, the reason only in the transcript — and the notification that
    reaches Telegram carries the CLI's own sentence, under a title that names auth."""
    daemon = started
    fake_claude.turns_auth_failed()
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)

    _tick(daemon)
    assert settle_turns(store), "the auth-refused turn never settled"
    _tick(daemon)

    turn = store.latest_turn(wo["id"])
    assert turn is not None and turn["state"] == "failed"
    assert turn["error"] == LIVE_OAUTH, \
        f"the generic string survived: {turn['error']!r}"

    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_input", \
        "`failed` is a DEPENDENCY_DEAD_STATUS — it would fail the parent feature order"
    assert row["needs_attention"]
    assert "authenticate" in row["attention_reason"].lower()

    paused = [e for e in store.list_events(wo["id"]) if e["kind"] == "turn_paused"]
    assert paused, "a failure the user cannot see in `wo show` is the same bug twice"
    assert json.loads(paused[-1]["payload"])["reason"] == worker_session.PAUSE_AUTH
    assert [e for e in store.list_events(wo["id"])
            if e["kind"] == "turn_retries_exhausted"] == [], \
        "nothing was retried, so do not tell the user it was"

    note = dict(store.conn.execute(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT 1").fetchone())
    assert LIVE_OAUTH in note["body"]
    assert "authenticate" in note["title"].lower()
    # What Telegram would actually send, since the body is the half that was empty.
    assert LIVE_OAUTH in notify.render_telegram(note | {"project": "proj_a"},
                                                daemon.catalog)
    store.close()


def test_it_is_not_retried_while_the_sign_in_has_not_changed(started, fake_claude,
                                                             project, settle_turns):
    """The account cannot answer, so a backoff is five more of the same refusal. Nothing
    moves until the credentials do — which is also what stops the OS spawning a process
    per tick against a login nobody has fixed yet."""
    daemon = started
    fake_claude.turns_auth_failed()
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)

    _tick(daemon)
    assert settle_turns(store)
    for _ in range(4):
        _tick(daemon)

    last = store.latest_turn(wo["id"])
    assert last is not None and last["seq"] == 1, "the retry pass must not touch it"
    assert [e["kind"] for e in store.list_events(wo["id"])].count("turn_resumed") == 0
    store.close()


def test_the_attention_reason_survives_a_reconcile_tick(started, fake_claude, project,
                                                        settle_turns):
    """INV-ATTENTION-REASON rewrites any flag `true_blockers` cannot re-derive, so an
    auth reason raised only by the settler would be relabelled "worker failed — review
    and retry" on the very next tick — the sentence this order exists to remove."""
    daemon = started
    fake_claude.turns_auth_failed()
    wo = ops.create_work_order("proj_a", "ship the thing")
    store = ProjectStore(project)

    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    _tick(daemon)

    assert store.get_work_order(wo["id"])["attention_reason"] == invariants.AUTH_BLOCKER
    assert invariants.true_blockers(
        store, store.get_work_order(wo["id"]))[0] == invariants.AUTH_BLOCKER
    store.close()
