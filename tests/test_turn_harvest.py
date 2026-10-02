"""A turn whose process died without writing a result, and what the OS reads off disk.

Issue #888, spec docs/specs/2026-09-30-harvesting-a-dead-turn.md. Three halves, in the
order the OS meets them: the reap that harvests (§2), the checkpoint commit that keeps
uncommitted work (§3), and the relaunch brief the harvest replaces `RETRY_NOTE` with
(§6).

NOTHING HERE FAKES GIT, for `tests/test_landing.py`'s reason: the harvest is a reading of
what git actually says, and a fake would only test the fake.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import background, harvest, ops, timeline, worker_session  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def fleet(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    return {"daemon": Daemon(catalog), "project": catalog.projects[0],
            "store": ProjectStore(project), "path": project,
            "fake": fake_claude}


def _order(fleet, *, code: str | None = None, dirty: str | None = None) -> dict:
    """A work order with a worktree cut off `main`, carrying `code` and `dirty`.

    `worktree` is set by `worker_session.start`; the directory is made here because the
    fake CLI never honours `--worktree` (spec §7's "no worktree on disk" case is the
    test that leaves it out).
    """
    project = fleet["path"]
    wo = ops.create_work_order("proj_a", "build the exporter")
    wt = project / ".claude" / "worktrees" / wo["id"]
    _git(project, "worktree", "add", "-q", "-b", f"worktree-{wo['id']}", str(wt), "main")
    if code:
        (wt / f"{code}.py").write_text(f"def {code}():\n    return 1\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"add {code}")
    if dirty:
        (wt / f"{dirty}.py").write_text(f"def {dirty}():\n    return 2\n")
    return {**wo, "worktree_path": wt}


def _start(fleet, wo_id: str, mode: str = "silent") -> None:
    """Launch turn 1 under a transport fault. `silent` is the issue's own shape: exit 0,
    no result JSON, no stderr."""
    if mode == "silent":
        fleet["fake"].turns_fail("silent")
    store = fleet["store"]
    worker_session.start(store, fleet["project"], store.get_work_order(wo_id), "go")


def _dead_turn(fleet, wo_id: str, settle, said: str = "", during=None) -> dict:
    """A real turn through the transport whose process wrote nothing at all.

    The REAP is what has to harvest — nothing here is called by hand. `said` is the
    worker's last words as the `Stop` hook recorded them, which is the only place they
    exist for a turn that wrote no result. `during(turn)` runs after the launch and
    before the reap: what the turn itself did, in the window the harvest reads (§2).
    """
    _start(fleet, wo_id)
    if said:
        fleet["store"].add_event(wo_id, "hook:Stop", {"last_assistant_message": said})
    if during is not None:
        during(fleet["store"].latest_turn(wo_id))
    assert settle(fleet["store"]), "the turn never settled"
    return fleet["store"].latest_turn(wo_id)


def _harvest_payload(store, wo_id: str) -> dict:
    events = store.events_of_kind(wo_id, timeline.TURN_HARVESTED)
    assert len(events) == 1, f"expected one harvest, got {len(events)}"
    return json.loads(events[0]["payload"])


def _last_harvest_payload(store, wo_id: str) -> dict:
    events = store.events_of_kind(wo_id, timeline.TURN_HARVESTED)
    assert events, "no harvest was written"
    return json.loads(events[-1]["payload"])


def _turn_started(store, wo_id: str, seq: int) -> dict:
    for event in store.events_of_kind(wo_id, "turn_started"):
        payload = json.loads(event["payload"])
        if payload.get("seq") == seq:
            return payload
    raise AssertionError(f"no turn_started for seq {seq}")


# -- 1. what the reap reads off disk ---------------------------------------------------


def test_harvest_records_last_message_and_commits(fleet, settle_turns):
    """§2: the payload names the turn, what the branch carries, and what it said."""
    wo = _order(fleet, code="exporter")
    turn = _dead_turn(fleet, wo["id"], settle_turns,
                      said="Exporter written; the CSV path is still untested.")

    payload = _harvest_payload(fleet["store"], wo["id"])
    assert payload["said"] == "Exporter written; the CSV path is still untested."
    assert payload["seq"] == turn["seq"]
    assert payload["version"] == 1
    assert payload["empty"] is False
    assert payload["authored"]["branch"] == f"worktree-{wo['id']}"
    assert payload["authored"]["commits"] == 1
    assert payload["authored"]["dirty"] == []
    # Committed BEFORE the launch, so `head..HEAD` is empty: the turn itself made none.
    assert payload["since"] == "turn"
    assert payload["turn_commits"] == []
    assert payload["unreadable"] == ""
    # §5: ONE render contract, and both surfaces read it.
    state = ops.harvest_state(fleet["store"], fleet["store"].get_work_order(wo["id"]))
    assert state["line"].startswith("1 commit on ")
    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "What the OS saved from the last turn" in page.text


def test_turn_commits_are_scoped_to_the_launch_sha(fleet, settle_turns):
    """§2: `turn_started.head` scopes the reading to THIS turn, not the whole order."""
    wo = _order(fleet, code="exporter")
    wt = wo["worktree_path"]
    head_before = _git(wt, "rev-parse", "HEAD").strip()

    def commit_in_the_turn(_turn):
        (wt / "csv_path.py").write_text("def csv_path():\n    return 3\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "add csv_path")

    turn = _dead_turn(fleet, wo["id"], settle_turns, during=commit_in_the_turn)
    after = _git(wt, "rev-parse", "HEAD").strip()

    assert _turn_started(fleet["store"], wo["id"], turn["seq"])["head"] == head_before
    payload = _harvest_payload(fleet["store"], wo["id"])
    assert payload["since"] == "turn"
    assert len(payload["turn_commits"]) == 1
    assert after.startswith(payload["turn_commits"][0])
    # The whole branch is two commits; only one of them was made in the turn.
    assert payload["authored"]["commits"] == 2


def test_turn_commits_fall_back_to_the_order_span(fleet, settle_turns):
    """§2: no `head` on the record — a turn launched before this shipped — so the span
    is `base..HEAD` and the payload says `order` rather than guessing."""
    wo = _order(fleet, code="exporter")
    wt = wo["worktree_path"]
    store = fleet["store"]

    def commit_and_forget_the_head(turn):
        (wt / "csv_path.py").write_text("def csv_path():\n    return 3\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "add csv_path")
        # `_head_at_launch` reads the LAST `turn_started` for the seq, so this one wins.
        store.add_event(wo["id"], "turn_started", {"seq": turn["seq"]})

    _dead_turn(fleet, wo["id"], settle_turns, during=commit_and_forget_the_head)

    payload = _harvest_payload(store, wo["id"])
    assert payload["since"] == "order"
    assert len(payload["turn_commits"]) == 2
    assert payload["authored"]["commits"] == 2


def test_harvest_checkpoints_uncommitted_work(fleet, settle_turns):
    """§3: uncommitted edits outlive the worktree, as a local commit with the trailer."""
    wo = _order(fleet, dirty="halfway")
    wt = wo["worktree_path"]
    _dead_turn(fleet, wo["id"], settle_turns)

    payload = _harvest_payload(fleet["store"], wo["id"])
    assert len(payload["checkpoint"]) >= 7, payload["checkpoint_skipped"]
    assert payload["checkpoint_skipped"] == ""
    assert payload["authored"]["dirty"] == ["halfway.py"]
    log = _git(wt, "log", "-1", "--format=%B")
    assert f"Jarvis-Checkpoint: {wo['id']}/{payload['seq']}" in log
    assert "WIP: Jarvis checkpoint" in log
    assert _git(wt, "status", "--porcelain", "--untracked-files=all").strip() == ""


def test_checkpoint_is_not_pushed_and_makes_no_branch(fleet, settle_turns):
    """§3: local only. Never a push, never a new branch, never a pull request."""
    wo = _order(fleet, dirty="halfway")
    wt = wo["worktree_path"]
    branches_before = _git(fleet["path"], "branch", "--list").splitlines()
    _dead_turn(fleet, wo["id"], settle_turns)

    assert _git(fleet["path"], "branch", "--list").splitlines() == branches_before
    # No upstream was ever set, and the harvest did not set one.
    assert subprocess.run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref",
                           "HEAD@{upstream}"], capture_output=True).returncode != 0
    payload = _harvest_payload(fleet["store"], wo["id"])
    assert payload["upstream"] == "" and payload["unpushed"] == 0
    assert payload["pr_url"] == ""


def test_nothing_to_harvest_is_recorded_as_such(fleet, settle_turns):
    """§7: the event IS written, and it never claims something was saved."""
    wo = _order(fleet)
    _dead_turn(fleet, wo["id"], settle_turns)

    store = fleet["store"]
    payload = _harvest_payload(store, wo["id"])
    assert payload["empty"] is True
    assert payload["checkpoint"] == ""
    assert harvest.retry_brief(store, store.get_work_order(wo["id"])) == ""
    entry = [e for e in timeline.build_timeline(
        store.get_work_order(wo["id"]), store.list_events(wo["id"]), [])
        if e["kind"] == timeline.TURN_HARVESTED][0]
    assert entry["label"] == "Nothing to harvest"


def test_harvest_raises_turn_still_settles(fleet, settle_turns, monkeypatch):
    """§7: the harvest is best effort and the settlement is not. Nothing escapes."""
    wo = _order(fleet, dirty="halfway")

    def boom(*a, **k):
        raise RuntimeError("harvest exploded")

    monkeypatch.setattr(harvest, "write", boom)
    # Dispatched by the DAEMON here, unlike every test above: what is under test is that
    # the settlement outlives the harvest, and the reconciler only settles an order it
    # claimed.
    store = fleet["store"]
    fleet["fake"].turns_fail("silent")
    fleet["daemon"].tick()
    assert settle_turns(store)
    fleet["daemon"].tick_count = 0
    fleet["daemon"].tick()

    assert store.latest_turn(wo["id"])["state"] == "failed"
    assert store.events_of_kind(wo["id"], timeline.TURN_HARVESTED) == []
    assert store.get_work_order(wo["id"])["status"] == "failed"


@pytest.mark.parametrize("fault", ["rate_limit", "api_error", "budget", "auth"])
def test_no_harvest_on_pause_paths(fleet, settle_turns, fault):
    """Decision 2: a PAUSE resumes the same session, and a commit under a live worker
    surprises it mid-task. No harvest, no commit."""
    wo = _order(fleet, dirty="halfway")
    wt = wo["worktree_path"]
    head_before = _git(wt, "rev-parse", "HEAD").strip()
    fake = fleet["fake"]
    if fault == "rate_limit":
        fake.turns_rate_limited()
    elif fault == "api_error":
        fake.turns_api_error(500)
    elif fault == "budget":
        # Not a pause, and excluded for its own reason: the turn RAN and spent real
        # money, and `finish_turn` takes it down the result branch, not this one.
        fake.turns_fail("budget")
    else:
        fake.turns_auth_failed()
    store = fleet["store"]
    worker_session.start(store, fleet["project"], store.get_work_order(wo["id"]), "go")
    assert settle_turns(store)

    assert store.events_of_kind(wo["id"], timeline.TURN_HARVESTED) == []
    assert _git(wt, "rev-parse", "HEAD").strip() == head_before


def test_harvest_skips_checkpoint_during_rebase(fleet, settle_turns):
    """§7: no commit on top of a half-finished merge. The read-only half still runs."""
    wo = _order(fleet, code="exporter", dirty="halfway")
    wt = wo["worktree_path"]
    gitdir = Path(_git(wt, "rev-parse", "--absolute-git-dir").strip())
    (gitdir / "MERGE_HEAD").write_text(_git(wt, "rev-parse", "HEAD"))

    _dead_turn(fleet, wo["id"], settle_turns)

    payload = _harvest_payload(fleet["store"], wo["id"])
    assert payload["checkpoint"] == ""
    assert "merge" in payload["checkpoint_skipped"]
    assert payload["authored"]["commits"] == 1
    assert payload["authored"]["dirty"] == ["halfway.py"]


def test_refused_checkpoint_commit_records_gits_own_words(fleet, settle_turns):
    """§7: git refuses the commit — ITS OWN STDERR is recorded, clipped to one line, and
    the rest of the harvest stands."""
    wo = _order(fleet, code="exporter", dirty="halfway")
    heads = fleet["path"] / ".git" / "refs" / "heads"
    heads.chmod(0o500)  # no ref lock can be made here, so the commit cannot land
    try:
        _dead_turn(fleet, wo["id"], settle_turns, said="Half of the CSV path is written.")
    finally:
        heads.chmod(0o700)

    payload = _harvest_payload(fleet["store"], wo["id"])
    reason = payload["checkpoint_skipped"]
    print(f"git said: {reason!r}")
    assert payload["checkpoint"] == ""
    assert reason.startswith("git refused the checkpoint commit: ")
    assert "\n" not in reason  # the CLI and the HTML page both render it on one line
    assert len(reason) <= len("git refused the checkpoint commit: ") + harvest.SKIP_CHARS
    # what git ACTUALLY says here, verbatim from the run above
    assert "cannot lock ref 'HEAD'" in reason
    assert "Permission denied" in reason
    assert payload["said"] == "Half of the CSV path is written."
    assert payload["authored"]["commits"] == 1
    assert payload["authored"]["dirty"] == ["halfway.py"]
    assert payload["empty"] is False


def test_a_refusal_with_nothing_to_say_falls_back_to_the_fixed_reason(
        fleet, settle_turns, monkeypatch):
    """§7: the fixed string is the FALLBACK — the field is never empty when git failed
    with an empty stderr, or could not be run at all. Monkeypatched: a real git that
    exits non-zero and says nothing is not reachable from a test."""
    from jarvis import branchproof

    real = branchproof.attempt

    def mute(repo, *args, **kwargs):
        return (None, "") if "commit" in args else real(repo, *args, **kwargs)

    monkeypatch.setattr(branchproof, "attempt", mute)
    wo = _order(fleet, code="exporter", dirty="halfway")

    _dead_turn(fleet, wo["id"], settle_turns)

    payload = _harvest_payload(fleet["store"], wo["id"])
    assert payload["checkpoint"] == ""
    assert payload["checkpoint_skipped"] == (
        "git refused the checkpoint commit — see the daemon log")


def test_second_harvest_skips_an_already_checkpointed_head(fleet, settle_turns):
    """§3: the trailer is the machine handle — a harvest that reads a HEAD already
    carrying this turn's checkpoint never commits a second one."""
    wo = _order(fleet, dirty="halfway")
    wt = wo["worktree_path"]
    store = fleet["store"]
    turn = _dead_turn(fleet, wo["id"], settle_turns)
    first = _harvest_payload(store, wo["id"])
    assert first["checkpoint"]

    (wt / "more.py").write_text("def more():\n    return 4\n")
    head_before = _git(wt, "rev-parse", "HEAD").strip()
    harvest.write(store, store.get_work_order(wo["id"]), turn, said="")

    payload = _last_harvest_payload(store, wo["id"])
    assert payload["checkpoint"] == ""
    assert payload["checkpoint_skipped"] == "this turn was already checkpointed"
    assert _git(wt, "rev-parse", "HEAD").strip() == head_before


def test_no_worktree_records_unreadable(fleet, settle_turns):
    """§7: `said` and the jobs are still harvested when the directory is gone."""
    wo = _order(fleet, dirty="halfway")
    store = fleet["store"]
    _start(fleet, wo["id"])
    # Taken away before the reap, the way the disk sweep of issue #232 took 172 of them.
    shutil.rmtree(wo["worktree_path"])
    assert settle_turns(store)

    payload = _harvest_payload(store, wo["id"])
    assert payload["unreadable"] == "no worktree on disk"
    assert payload["checkpoint"] == ""


def test_harvest_does_not_write_result_summary(fleet, settle_turns):
    """Neo q1131, pinned: machine prose there makes a failed order read as delivered."""
    wo = _order(fleet, code="exporter", dirty="halfway")
    _dead_turn(fleet, wo["id"], settle_turns)

    assert fleet["store"].get_work_order(wo["id"])["result_summary"] in (None, "")


# -- 2. the relaunch brief -------------------------------------------------------------


def test_retry_brief_replaces_retry_note(fleet, settle_turns):
    """§6: the OS reads it off disk so the worker does not have to re-derive it."""
    wo = _order(fleet, code="exporter", dirty="halfway")
    _dead_turn(fleet, wo["id"], settle_turns, said="Half of the CSV path is written.")
    store = fleet["store"]
    store.set_status(wo["id"], "failed")

    out = ops.retry(wo["id"], project_name="proj_a")

    body = [m for m in store.list_messages(wo["id"]) if m["id"] == out["msg_id"]][0]
    assert body["content"] != ops.RETRY_NOTE
    assert "[Jarvis]" in body["content"]
    assert "you last said: \"Half of the CSV path is written.\"" in body["content"]
    assert "1 commit over `main`" in body["content"] or "carries 1 commit" in body["content"]
    assert "WIP checkpoint" in body["content"]
    assert "Do not start again." in body["content"]
    assert out["authored"] is False


def test_retry_note_when_no_harvest(fleet, settle_turns):
    """An order with nothing harvested falls back to `RETRY_NOTE`, unchanged."""
    wo = _order(fleet)
    _dead_turn(fleet, wo["id"], settle_turns)
    store = fleet["store"]
    store.set_status(wo["id"], "failed")

    out = ops.retry(wo["id"], project_name="proj_a")

    body = [m for m in store.list_messages(wo["id"]) if m["id"] == out["msg_id"]][0]
    assert body["content"] == ops.RETRY_NOTE


# -- 3. the background jobs, recorded by their one writer ------------------------------


def test_background_jobs_recorded_once(fleet, settle_turns, monkeypatch):
    """§2: `background.record` stays the ONE writer of `background_orphaned`, and the
    harvest payload carries only the count."""
    wo = _order(fleet, dirty="halfway")
    from jarvis import background as bg

    job = bg.Job("b51fl7bhe", "uv run pytest tests/ -q")
    monkeypatch.setattr(bg, "orphaned_in_turn", lambda *a, **k: [job])
    _dead_turn(fleet, wo["id"], settle_turns)

    store = fleet["store"]
    events = store.events_of_kind(wo["id"], background.EVENT)
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["jobs"] == [
        {"id": "b51fl7bhe", "command": "uv run pytest tests/ -q"}]
    assert _harvest_payload(store, wo["id"])["jobs"] == 1
    assert background.resume_note(store, store.get_work_order(wo["id"]))
