"""An auth-paused work order comes back by itself, once the user signs back in"""

from __future__ import annotations

import json

import pytest

from jarvis import claude_cli, db, invariants, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project

# -- the probe


def test_a_sign_in_the_os_cannot_see_is_not_a_sign_in(signin, monkeypatch):
    """None means DO NOT RESUME, and it is the default: an API-key sign-in has nothing to watch"""
    assert claude_cli.signin_changed_at() is None


def test_signing_in_is_the_file_changing(signin):
    path = signin(at=1000.0)
    assert claude_cli.signin_changed_at() == 1000.0
    signin(at=2000.0)
    assert claude_cli.signin_changed_at() == 2000.0
    assert path.exists()


def test_an_expired_refresh_token_is_not_evidence_of_anything(signin):
    """`/login` REPLACES the refresh token rather than touching it"""
    signin(refresh_expires_in=-60)
    assert claude_cli.signin_changed_at() is None


def test_a_bare_sign_in_is_later_than_the_turn_it_rescues(signin):
    """The fixture's contract, asserted rather than assumed"""
    for _ in range(200):
        ended = db.now()
        signin()
        changed = claude_cli.signin_changed_at()
        assert changed is not None and changed > ended


def test_a_credentials_file_it_cannot_parse_is_read_as_no_answer(signin):
    signin().write_text("{not json")
    assert claude_cli.signin_changed_at() is None


# -- the pause's clock


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path)
    s.create_work_order("a work order", origin="manual", wo_id="wo-test")
    yield s
    s.close()


def _auth_turn(store, ago: float = 3600.0):
    """A turn that died on auth `ago` seconds back"""
    turn = store.create_turn("wo-test", kind="message", prompt="do the thing")
    row = store.finish_turn(
        turn["id"], "failed",
        error="Failed to authenticate: OAuth session expired and could not be refreshed")
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (row["started_at"] - ago, row["ended_at"] - ago, turn["id"]))
    return store.get_turn(turn["id"])


def test_the_sign_in_that_failed_is_not_the_one_that_fixes_it(store, signin):
    """A sign-in OLDER than the turn is the one the turn died on"""
    turn = _auth_turn(store)
    signin(at=turn["ended_at"] - 60)
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert pause.retry_at == worker_session.NEVER and not pause.resumable


def test_a_sign_in_after_the_failure_makes_the_pause_due(store, signin):
    """The test that caught the clock race"""
    _auth_turn(store)
    signin()
    pause = worker_session.turn_pause(store, "wo-test")
    assert pause is not None
    assert pause.resumable and pause.due()


# -- end to end


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _tick(daemon):
    """One tick with every cadence-gated pass firing — `tick` increments first"""
    daemon.tick_count = 0
    daemon.tick()


def _sign_in_after(signin, store, wo_id):
    """`/login`, stamped a hair AFTER the turn that failed and still in the past"""
    return signin(at=store.latest_turn(wo_id)["ended_at"] + 0.001)


def _park(daemon, store, fake_claude, settle_turns, title="ship the thing"):
    """Run a work order into an auth pause and return its row"""
    wo = ops.create_work_order("proj_a", title)
    _tick(daemon)
    assert settle_turns(store), "the auth-refused turn never settled"
    _tick(daemon)
    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_input"
    return row


def test_it_waits_for_the_sign_in_and_then_comes_back(started, fake_claude, project,
                                                      settle_turns, signin):
    """THE ASK"""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)

    for _ in range(3):
        _tick(daemon)
    assert store.latest_turn(wo["id"])["seq"] == 1, \
        "relaunched with no sign-in — that is a process spawn per tick against a dead account"

    fake_claude.turns_recover()
    _sign_in_after(signin, store, wo["id"])
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
    """An auth refusal happens BEFORE the turn reaches the model"""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)
    first = store.latest_turn(wo["id"])["prompt"]

    fake_claude.turns_recover()
    _sign_in_after(signin, store, wo["id"])
    _tick(daemon)

    assert store.latest_turn(wo["id"])["prompt"] == first
    store.close()


def test_the_user_can_still_push_it_by_hand(started, fake_claude, project,
                                            settle_turns):
    """The escape hatch for a sign-in the OS cannot see at all"""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    wo = _park(daemon, store, fake_claude, settle_turns)

    fake_claude.turns_recover()
    ops.send_message(wo["id"], "retry")
    _tick(daemon)

    assert store.latest_turn(wo["id"])["seq"] == 2, "the message was held for ever"
    store.close()


# -- fleet-wide


@pytest.fixture()
def two_projects(tmp_path, claude_json, project):
    """A catalog with a second project, so "fleet-wide" is more than a claim"""
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
    """Authentication is an ACCOUNT-level fact"""
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
    # After the LATER of the two failures: one sign-in has to release both
    signin(at=max(a.latest_turn(wos["proj_a"]["id"])["ended_at"],
                  b.latest_turn(wos["proj_b"]["id"])["ended_at"]) + 0.001)
    _tick(daemon)

    assert a.latest_turn(wos["proj_a"]["id"])["seq"] == 2
    assert b.latest_turn(wos["proj_b"]["id"])["seq"] == 2, \
        "the second project's order was left parked — the sweep is not fleet-wide"
    a.close()
    b.close()


# -- what must NOT happen


def test_a_parked_child_does_not_fail_its_feature_order(started, fake_claude, project,
                                                        settle_turns):
    """THE DAMAGE THIS ORDER EXISTS TO UNDO"""
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
    """`waiting_input` renders as "Waiting on you" everywhere"""
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
    """`jarvis wo resume-auto` answers "what is this actually waiting on\""""
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
    """INV-PAUSE-OVERDUE watches for a relaunch that should have happened and did not"""
    daemon = started
    fake_claude.turns_auth_failed()
    store = ProjectStore(project)
    _park(daemon, store, fake_claude, settle_turns)

    assert [v for v in invariants.check_paused_turns_resume(store)] == []
    store.close()
