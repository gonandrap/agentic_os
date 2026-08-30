"""An auth-paused work order comes back by itself, once the user signs back in.

The ask, in the user's words: "in the same way than we retry orders that got stuck when
we hit the limits, once I authenticate, those 'auth_failed' orders should automatically
resume."

The design problem it sets, and what this suite pins down: the other two pause reasons
resume on a DEADLINE, and an auth failure has none — it clears when a human runs
`/login`, which may be in thirty seconds or next week. So the trigger is a state check
worn as a clock. `claude_cli.signin_changed_at` reads the mtime of Claude Code's
credentials file, `worker_session._auth_retry_at` hands it back as `retry_at` when it is
newer than the failure, and `Daemon.retry_paused_turns` — which already runs fleet-wide
every other tick — compares it to the clock exactly as it does for the other two.

Three properties this must have, in the order the work order asked for them:

  * NOT RELAUNCHED while the sign-in is unchanged, and relaunched once it changes. Both
    halves matter: a fixed backoff would spawn a process per tick against an account
    that cannot answer.
  * FLEET-WIDE. Signing in is an account-level fact, not a per-project one.
  * NEVER `failed`. `failed` is a DEPENDENCY_DEAD_STATUS, so it strands dependents and
    fails the parent feature order — which is what a dead login did to fo-e353491c on
    2026-08-27, the incident this whole change exists for.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import claude_cli, invariants, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project

# -- the probe -------------------------------------------------------------------------


def test_a_sign_in_the_os_cannot_see_is_not_a_sign_in(signin, monkeypatch):
    """None means DO NOT RESUME, and it is the default: a macOS keychain and
    `ANTHROPIC_API_KEY` both leave nothing at this path to read."""
    assert claude_cli.signin_changed_at() is None


def test_signing_in_is_the_file_changing(signin):
    path = signin(at=1000.0)
    assert claude_cli.signin_changed_at() == 1000.0
    signin(at=2000.0)
    assert claude_cli.signin_changed_at() == 2000.0
    assert path.exists()


def test_an_expired_refresh_token_is_not_evidence_of_anything(signin):
    """`/login` REPLACES the refresh token rather than touching it, so a file whose
    mtime moved while this stayed in the past was moved by something else."""
    signin(refresh_expires_in=-60)
    assert claude_cli.signin_changed_at() is None


def test_a_credentials_file_it_cannot_parse_is_read_as_no_answer(signin):
    signin().write_text("{not json")
    assert claude_cli.signin_changed_at() is None


# -- the pause's clock ------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path)
    s.create_work_order("a work order", origin="manual", wo_id="wo-test")
    yield s
    s.close()


def _auth_turn(store, ended_at: float | None = None):
    turn = store.create_turn("wo-test", kind="message", prompt="do the thing")
    row = store.finish_turn(
        turn["id"], "failed",
        error="Failed to authenticate: OAuth session expired and could not be refreshed")
    if ended_at is None:
        return row
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (ended_at - 1, ended_at, turn["id"]))
    store.conn.commit()
    return dict(row, started_at=ended_at - 1, ended_at=ended_at)


def test_the_sign_in_that_failed_is_not_the_one_that_fixes_it(store, signin):
    """A sign-in OLDER than the turn is the one the turn died on. Without this the very
    first tick after the failure would relaunch, and go on relaunching."""
    _auth_turn(store)
    signin(at=time.time() - 3600)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert pause.retry_at == worker_session.NEVER and not pause.resumable


def test_a_sign_in_after_the_failure_makes_the_pause_due(store, signin):
    # The failure is aged rather than stamped `now`: the two sides of `_auth_retry_at`
    # are different clocks — a file's mtime against `db.now()` — and a sign-in written
    # microseconds after the turn is not reliably NEWER than it. Unaged, this passes
    # locally and fails on CI (3.11 and 3.13 of run 33295018938).
    _auth_turn(store, ended_at=time.time() - 5)
    signin()
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert pause.resumable and pause.due()


# -- end to end -------------------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _tick(daemon):
    """One tick with every cadence-gated pass firing — `tick` increments first."""
    daemon.tick_count = 0
    daemon.tick()


def _park(daemon, store, fake_claude, settle_turns, title="ship the thing"):
    """Run a work order into an auth pause and return its row."""
    wo = ops.create_work_order("proj_a", title)
    _tick(daemon)
    assert settle_turns(store), "the auth-refused turn never settled"
    _tick(daemon)
    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_input"
    return row


def test_it_waits_for_the_sign_in_and_then_comes_back(started, fake_claude, project,
                                                      settle_turns, signin):
    """THE ASK. Both halves in one test, because either alone is satisfiable by a bug:
    never resuming, or resuming on a timer that would hammer a dead account."""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)

    for _ in range(3):
        _tick(daemon)
    assert store.latest_turn(wo["id"])["seq"] == 1, \
        "relaunched with no sign-in — that is a process spawn per tick against a dead account"

    fake_claude.turns_recover()
    signin()
    _tick(daemon)

    assert store.latest_turn(wo["id"])["seq"] == 2, "the sign-in did not release it"
    resumed = [e for e in store.list_events(wo["id"]) if e["kind"] == "turn_resumed"]
    assert json.loads(resumed[-1]["payload"])["reason"] == worker_session.PAUSE_AUTH
    row = store.get_work_order(wo["id"])
    assert row["status"] == "running" and not row["needs_attention"], \
        "a worker that is working must not still read as waiting on you"
    store.close()


def test_the_prompt_goes_again_verbatim(started, fake_claude, project, settle_turns,
                                        signin):
    """An auth refusal happens BEFORE the turn reaches the model, so the conversation is
    untouched and the work order's own dispatch prompt is what has to be re-sent — not
    the "carry on" nudge a turn that died in flight gets (`worker_session.retry`)."""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)
    first = store.latest_turn(wo["id"])["prompt"]

    fake_claude.turns_recover()
    signin()
    _tick(daemon)

    assert store.latest_turn(wo["id"])["prompt"] == first
    store.close()


def test_the_user_can_still_push_it_by_hand(started, fake_claude, project,
                                            settle_turns):
    """The escape hatch for a sign-in the OS cannot see at all. `deliver_messages` holds
    a queued message behind a pause that is coming back on its own — and an auth pause
    with no sign-in never is, so holding on it would hold this for ever."""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)

    fake_claude.turns_recover()
    ops.send_message(wo["id"], "retry")
    _tick(daemon)

    assert store.latest_turn(wo["id"])["seq"] == 2, "the message was held for ever"
    store.close()


# -- fleet-wide -------------------------------------------------------------------------


@pytest.fixture()
def two_projects(tmp_path, claude_json, project):
    """A catalog with a second project, so "fleet-wide" is more than a claim."""
    other = make_git_project(tmp_path, "proj_b")
    claude_json(other)
    path = tmp_path / "catalog-two.json"
    path.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]}},
        "projects": [
            {"name": "proj_a", "path": str(project), "description": "test project"},
            {"name": "proj_b", "path": str(other), "description": "the other one"},
        ],
    }))
    return path, other


def test_one_sign_in_releases_every_project(jarvis_home, fake_claude, two_projects,
                                            project, settle_turns, signin):
    """Authentication is an ACCOUNT-level fact. `retry_paused_turns` is called per
    project, so the thing that could quietly go wrong is a recovery reaching only the
    project the daemon happened to look at first."""
    catalog_path, other = two_projects
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    fake_claude.turns_auth_failed()

    a, b = ProjectStore(project), ProjectStore(other)
    wos = {"proj_a": ops.create_work_order("proj_a", "ship a"),
           "proj_b": ops.create_work_order("proj_b", "ship b")}
    _tick(daemon)
    assert settle_turns(a) and settle_turns(b)
    _tick(daemon)
    assert a.get_work_order(wos["proj_a"]["id"])["status"] == "waiting_input"
    assert b.get_work_order(wos["proj_b"]["id"])["status"] == "waiting_input"

    fake_claude.turns_recover()
    signin()
    _tick(daemon)

    assert a.latest_turn(wos["proj_a"]["id"])["seq"] == 2
    assert b.latest_turn(wos["proj_b"]["id"])["seq"] == 2, \
        "the second project's order was left parked — the sweep is not fleet-wide"
    a.close()
    b.close()


# -- what must NOT happen ----------------------------------------------------------------


def test_a_parked_child_does_not_fail_its_feature_order(started, fake_claude, project,
                                                        settle_turns):
    """THE DAMAGE THIS ORDER EXISTS TO UNDO. `settle_features` fails a feature the
    moment any child is `failed`, and `failed` is where a dead login used to put one —
    so on 2026-08-27 fo-e353491c was failed at 11/12 by an expired OAuth session."""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    fo = store.create_feature_order("ship the exporter", "all of it")
    child = store.create_work_order("the last child", parent_id=fo["id"])
    store.set_feature_status(fo["id"], "executing")

    _tick(daemon)
    assert settle_turns(store)
    for _ in range(3):
        _tick(daemon)

    assert store.get_work_order(child["id"])["status"] == "waiting_input"
    fresh = store.get_feature_order(fo["id"])
    assert fresh["status"] == "executing" and not fresh["needs_attention"], \
        "an expired login failed a feature order that was 11/12 done"
    store.close()


def test_the_attention_reason_names_the_sign_in_and_survives_a_tick(started, fake_claude,
                                                                    project,
                                                                    settle_turns):
    """`waiting_input` renders as "Waiting on you" everywhere, and its generic blocker
    steers the user at a session to type into. The action is `/login`, and
    INV-ATTENTION-REASON only lets a reason stand if `true_blockers` re-derives it."""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)
    _tick(daemon)

    row = store.get_work_order(wo["id"])
    assert row["attention_reason"] == invariants.AUTH_BLOCKER
    assert invariants.true_blockers(store, row) == [invariants.AUTH_BLOCKER], \
        "'worker is waiting on your input' would send them looking for a session"
    store.close()


def test_resume_auto_names_the_sign_in_and_not_a_permission_prompt(started, fake_claude,
                                                                   project,
                                                                   settle_turns):
    """`jarvis wo resume-auto` answers "what is this actually waiting on". A parked auth
    order has the exact shape its fall-through calls an unanswered permission prompt —
    `waiting_input`, nothing running, nothing queued — so it would send the user at the
    one thing that cannot help."""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)

    wait = ops.waiting_on(store, store.get_work_order(wo["id"]))
    assert wait["what"] == "signin" and not wait["stalled"]
    assert "/login" in wait["detail"]
    store.close()


def test_a_parked_order_is_not_reported_as_an_overdue_retry(started, fake_claude,
                                                            project, settle_turns):
    """INV-PAUSE-OVERDUE watches for a relaunch that should have happened and did not.
    Waiting on an action the user has not taken is not overdue — `retry_at` is `NEVER`
    — and reporting it would make the check cry wolf for the whole of every logged-out
    night."""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    _park(daemon, store, fake_claude, settle_turns)

    assert [v for v in invariants.check_paused_turns_resume(store)] == []
    store.close()
