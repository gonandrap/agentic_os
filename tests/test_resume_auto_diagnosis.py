"""`jarvis wo resume-auto` diagnoses before it nudges — GitHub issue 100, bug 2.

The command was written to recover a worker stalled on a permission prompt: flip it to
`auto` and nudge it. But `auto` is `catalog.DEFAULT_PERMISSION_MODE`, the production
catalog sets it explicitly, and no project overrides it — so the flip is always
`auto → auto`, and a worker in a mode that never prompts has never stalled on one. The
only thing the command actually did was send a message, and a message is not free: every
turn boundary re-sends the whole conversation at the cache-write rate. On wo-52a6164d it
was run twice against a worker that had never stalled, was mid-turn, and completed the
release unaided half an hour later.

So it now says what the work order is really waiting on, refuses the pointless nudge, and
keeps `--force` for the user who has decided otherwise. `jarvis status` stops naming it
as the remedy wherever a permission prompt is impossible.
"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.hooks import handle_hook
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """`permission_mode` falls to `auto`, exactly as the production catalog leaves it."""
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def prompting_catalog(tmp_path, project):
    """The one fleet shape where `resume-auto` can still do its original job."""
    data = {
        "os": {"defaults": {"model": "sonnet", "permission_mode": "acceptEdits"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "prompting-catalog.json"
    path.write_text(json.dumps(data))
    return path


def parked_on_neo(daemon, project) -> dict:
    """A work order waiting on Neo — the state three of five sampled production work
    orders were in when the user was told to run this command."""
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    assert ProjectStore(project).get_work_order(wo["id"])["status"] == "waiting_input"
    return wo


# -- the refusal ----------------------------------------------------------------------


def test_it_refuses_to_nudge_a_work_order_parked_on_neo(started, project):
    daemon = started
    wo = parked_on_neo(daemon, project)

    result = ops.resume_in_auto(wo["id"])

    assert result["nudged"] is False
    assert result["changed"] is False
    assert result["waiting_on"] == "neo_question"
    assert "Neo is answering question 1" in result["diagnosis"]
    assert "--force" in result["note"]
    store = ProjectStore(project)
    assert store.queued_messages(wo["id"]) == []          # no conversation re-sent
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "resume_auto_declined" in kinds                # …and the record says so
    assert "permission_mode_changed" not in kinds


def test_force_nudges_anyway(started, project):
    """The user who has diagnosed it themselves keeps the old behaviour, explicitly."""
    daemon = started
    wo = parked_on_neo(daemon, project)

    result = ops.resume_in_auto(wo["id"], force=True)

    assert result["nudged"] is True
    assert ProjectStore(project).queued_messages(wo["id"])


def test_it_declines_while_a_gate_is_with_neo(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "ship it")
    daemon.tick()
    store = ProjectStore(project)
    store.set_status(wo["id"], "waiting_input")
    store.add_approval(wo["id"], kind="release", command="scripts/shipit.sh",
                       matched="shipit", justification="ready", evidence="green")

    result = ops.resume_in_auto(wo["id"])

    assert result["nudged"] is False
    assert result["waiting_on"] == "gate_with_neo"


def test_it_points_at_the_review_when_assumptions_are_pending(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "risky change")
    daemon.tick()
    store = ProjectStore(project)
    store.add_assumption(wo["id"], "chose CSV")
    store.set_status(wo["id"], "needs_review")

    result = ops.resume_in_auto(wo["id"])

    assert result["nudged"] is False
    assert result["waiting_on"] == "assumptions"
    assert f"jarvis wo review {wo['id']}" in result["diagnosis"]


def test_it_declines_for_a_worker_that_is_simply_working(started, project):
    """The wo-52a6164d case: a running worker, mid-turn, nothing wrong with it."""
    daemon = started
    wo = ops.create_work_order("proj_a", "long job")
    daemon.tick()

    result = ops.resume_in_auto(wo["id"])

    assert result["nudged"] is False
    assert result["waiting_on"] == "turn_running"


def test_it_declines_on_an_escalated_question_and_names_the_command(started, project):
    """Escalated is the user's — but the way through is `jarvis neo answer`, not a nudge
    at a worker that is waiting exactly as it was told to."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    daemon._neo_drain()

    result = ops.resume_in_auto(wo["id"])

    assert result["nudged"] is False
    assert result["waiting_on"] == "neo_escalated"
    assert "jarvis neo answer 1" in result["diagnosis"]


# -- what it must still do ------------------------------------------------------------


def test_a_genuine_stall_is_still_nudged(started, project):
    """The one thing left that nothing else can clear: `waiting_input` with no gate, no
    question, no queued message. Nothing is coming for this by itself, so the nudge is a
    repair — even though the mode is `auto` and cannot be the cause."""
    daemon = started
    wo = ops.create_work_order("proj_a", "blocked task")
    daemon.tick()
    store = ProjectStore(project)
    handle_hook(
        {"hook_event_name": "Notification",
         "session_id": store.get_work_order(wo["id"])["session_id"],
         "cwd": str(project), "message": "Claude needs your permission to run npm test"},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )
    assert store.get_work_order(wo["id"])["needs_attention"] == 1

    result = ops.resume_in_auto(wo["id"])

    assert result["nudged"] is True
    assert result["waiting_on"] == "prompt"
    assert store.queued_messages(wo["id"])
    assert store.get_work_order(wo["id"])["needs_attention"] == 0


def test_a_mode_that_can_prompt_is_still_flipped(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "task", permission_mode="acceptEdits")
    daemon.tick()
    store = ProjectStore(project)
    store.set_status(wo["id"], "waiting_input")

    result = ops.resume_in_auto(wo["id"])

    assert result["nudged"] is True and result["changed"] is True
    assert store.get_work_order(wo["id"])["permission_mode"] == "auto"
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "permission_mode_changed" in kinds


# -- and what `jarvis status` steers at -------------------------------------------------


def test_status_does_not_offer_the_remedy_where_no_prompt_is_possible(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "blocked task")
    daemon.tick()
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id="sess-abc123")
    store.set_status(wo["id"], "waiting_input")
    store.flag_attention(wo["id"], "worker is waiting on your input")

    item = [a for a in ops.os_status()["attention"] if a.get("wo_id") == wo["id"]][0]

    assert item["attach"] == "claude --resume sess-abc123"   # opening it still helps
    assert "resume_auto" not in item                         # flipping auto to auto does not


def test_status_still_offers_it_where_a_prompt_is_possible(jarvis_home, fake_claude,
                                                           prompting_catalog, project):
    ops.start_os(str(prompting_catalog), foreground=True)
    daemon = Daemon(load_catalog(prompting_catalog))
    wo = ops.create_work_order("proj_a", "blocked task")
    daemon.tick()
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id="sess-abc123")
    store.set_status(wo["id"], "waiting_input")
    store.flag_attention(wo["id"], "Claude needs your permission")

    item = [a for a in ops.os_status()["attention"] if a.get("wo_id") == wo["id"]][0]

    assert item["resume_auto"] == f"jarvis wo resume-auto {wo['id']}"


def test_the_status_line_prints_only_the_help_that_exists(started, project, capsys):
    daemon = started
    wo = ops.create_work_order("proj_a", "blocked task")
    daemon.tick()
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id="sess-abc123")
    store.set_status(wo["id"], "waiting_input")
    store.flag_attention(wo["id"], "worker is waiting on your input")
    capsys.readouterr()

    cli.main(["status"])

    out = capsys.readouterr().out
    assert "claude --resume sess-abc123" in out
    assert "resume-auto" not in out


def test_the_cli_carries_force_through(started, project, capsys):
    daemon = started
    wo = parked_on_neo(daemon, project)
    capsys.readouterr()

    cli.main(["wo", "resume-auto", wo["id"], "--json"])
    declined = json.loads(capsys.readouterr().out)
    cli.main(["wo", "resume-auto", wo["id"], "--force", "--json"])
    forced = json.loads(capsys.readouterr().out)

    assert declined["nudged"] is False and forced["nudged"] is True


def test_a_question_neo_has_answered_leaves_nothing_to_diagnose(started, project):
    """Once the answer is queued the work order is moving again — the diagnosis has to
    say so rather than reporting a wait that is over."""
    daemon = started
    wo = parked_on_neo(daemon, project)
    daemon._neo_drain()

    result = ops.resume_in_auto(wo["id"])

    assert result["waiting_on"] == "queued_message"
    assert result["nudged"] is False
