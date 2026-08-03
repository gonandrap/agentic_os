"""Phase 3 of feature orders: the planning team, and the postures that make it safe.

The design's requirement is that a planner produces a plan others work from and never
the work itself, and that the same applies per team member — an architect must not
produce code either. Two enforcement layers were established by live probe:

* **Layer 1, declarative and CLI-enforced**: the `tools:` key of an agent definition.
  A seat declared `tools: Read, Glob` reported CANNOT-WRITE *and left no file on disk*,
  against a control seat differing only in that key.
* **Layer 2, gate mediation**: `PreToolUse` fires for a subagent's calls and the payload
  carries `agent_type`, which the lead's own calls omit entirely.

This file pins layer 1 for the two seats, the delivery path that makes them reachable at
all, and — for layer 2 — the one thing the design flagged as easy to get wrong: a gate a
SEAT trips files against the planner's work order, so the record has to name the seat or
it says the planner did it.

The postures themselves are asserted against the shipped markdown rather than against a
paraphrase of it, because the frontmatter IS the enforcement. A test that checked a
Python constant would keep passing while the file that the CLI reads said something else.
"""

from __future__ import annotations

import json

import pytest

from jarvis import gates, ops
from jarvis.bootstrap import ASSETS, install_agent_assets
from jarvis.hooks import handle_hook
from jarvis.project_store import ProjectStore

SEATS = ("jarvis-architect", "jarvis-test-lead")

# Every tool that can put bytes on disk, directly or through a shell. `Bash` is on the
# list deliberately and after review (2026-08-03): withholding `Write` while granting a
# shell is not a prohibition, because `cat > f <<EOF` writes the file just as well. An
# advisory prohibition is not one.
EDITING_TOOLS = ("Write", "Edit", "NotebookEdit", "Bash")


def frontmatter(text: str) -> dict[str, str]:
    """The `---` block at the top of an agent definition, as key -> value."""
    assert text.startswith("---\n"), "an agent definition must open with frontmatter"
    block = text.split("---\n", 2)[1]
    out = {}
    for line in block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


# -- layer 1: the postures --------------------------------------------------------------


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_declares_the_tools_it_may_use(seat):
    fm = frontmatter((ASSETS / "agents" / f"{seat}.md").read_text())

    assert fm["name"] == seat, "the CLI resolves a subagent type by its `name` key"
    assert fm["description"], "the lead picks a seat by its description; it cannot be blank"
    assert "tools" in fm, (
        "without a `tools:` key the seat inherits the lead's full capabilities, which is "
        "the exact posture this phase exists to remove"
    )


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_cannot_produce_code(seat):
    """The load-bearing assertion of the whole phase.

    Not 'the prompt says do not write code' — the prompt is a preference the model can
    talk itself out of at hour three. This is the capability the CLI never grants.
    """
    granted = {t.strip() for t in
               frontmatter((ASSETS / "agents" / f"{seat}.md").read_text())["tools"]
               .split(",")}

    assert granted, "an empty tools list would make the seat useless, not safe"
    assert not granted & set(EDITING_TOOLS), (
        f"{seat} may use {granted & set(EDITING_TOOLS)}, so it can put code on disk"
    )


@pytest.mark.parametrize("seat", SEATS)
def test_a_seat_can_still_read_the_codebase(seat):
    """Sighted by design: an architect that cannot open a file cannot decompose anything,
    and a test lead that cannot read the existing suite invents a harness that is not
    there. Blindness in this design belongs to the reviewer, who was never in the room."""
    granted = {t.strip() for t in
               frontmatter((ASSETS / "agents" / f"{seat}.md").read_text())["tools"]
               .split(",")}

    assert "Read" in granted and "Grep" in granted


# -- delivery: who gets the seats -------------------------------------------------------


def test_the_seats_are_laid_out_where_add_dir_finds_them(project):
    """`--add-dir X` loads subagent definitions from `X/.claude/agents/` — the same
    mechanism that already delivers skills from `X/.claude/skills/`, verified live with a
    negative control."""
    roots = install_agent_assets(project, "planner")

    seats = [r for r in roots if (r / ".claude" / "agents").is_dir()]
    assert len(seats) == 1
    for seat in SEATS:
        assert (seats[0] / ".claude" / "agents" / f"{seat}.md").is_file()


def test_an_ordinary_worker_is_handed_no_seats(project):
    """Design decision 4: ordinary workers get no profile. A worker is an individual with
    exactly one job, and a role would be dressing up a session that has one."""
    roots = install_agent_assets(project, "worker")

    assert not any((r / ".claude" / "agents").exists() for r in roots)
    # ...and it still gets the skills, which are for everybody.
    assert any((r / ".claude" / "skills").is_dir() for r in roots)


def test_the_seats_live_in_their_own_root(project):
    """Two roots, not one. A single root would make the worker path delete `agents/` to
    keep owning its whole generated tree — and that delete lands while a concurrently
    dispatched planner is reading it."""
    worker_roots = install_agent_assets(project, "worker")
    planner_roots = install_agent_assets(project, "planner")

    # Rebuilding for a worker did not disturb the seats a planner was given.
    assert all((r / ".claude" / "agents").is_dir()
               for r in planner_roots if "seats" in r.name)
    install_agent_assets(project, "worker")
    assert all((r / ".claude" / "agents").is_dir()
               for r in planner_roots if "seats" in r.name)
    assert worker_roots[0] == planner_roots[0], "the skills root is shared, and unchanged"


def test_the_seats_are_generated_not_authored(project):
    """They live under the gitignored state dir and heal themselves, like the skills."""
    roots = install_agent_assets(project, "planner")
    seats = next(r for r in roots if (r / ".claude" / "agents").is_dir())
    assert ".jarvis" in seats.relative_to(project).parts

    defn = seats / ".claude" / "agents" / "jarvis-architect.md"
    defn.write_text("locally mangled")
    install_agent_assets(project, "planner")
    assert defn.read_text() != "locally mangled"


def test_a_planner_turn_is_launched_with_the_seat_directory(project, fake_claude,
                                                            catalog_file, jarvis_home):
    """The whole delivery path, end to end: a planner's briefing flags reach the CLI with
    the seats' directory on `--add-dir`. A resumed turn re-derives its flags from argv,
    so this is also what keeps the seats available after turn 1."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    ops.create_feature_order("proj_a", "CSV export", description=(
        "Add a CSV exporter to the reporting module, with a command that calls it and "
        "tests over the happy path and an empty result set."))
    daemon.tick()

    call = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"], count=1)[0]
    dirs = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "--add-dir"]
    assert any(d.endswith("agent-seats") for d in dirs), dirs
    assert any(d.endswith("agent-skills") for d in dirs), dirs


def test_the_planner_briefing_names_its_seats(project, fake_claude, catalog_file,
                                              jarvis_home):
    """Two definitions nothing ever invokes are two files on disk. The briefing is what
    makes the team real, so it has to name each seat and say when to reach for it."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    ops.create_feature_order("proj_a", "CSV export", description=(
        "Add a CSV exporter to the reporting module, with a command that calls it and "
        "tests over the happy path and an empty result set."))
    daemon.tick()

    prompt = fake_claude.wait_calls(
        lambda c: "--session-id" in c["argv"], count=1)[0]["argv"][-1]
    for seat in SEATS:
        assert seat in prompt, f"the planner is never told {seat} exists"
    # And that they cannot run anything for it — a lead that assumes otherwise burns a
    # consult discovering it.
    assert "anything that needs a shell is yours to run" in prompt


# -- layer 2: whose attempt was it ------------------------------------------------------


def gate_payload(command: str, agent_type: str | None = None) -> dict:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": command}}
    # Absence is the discriminator, not a sentinel: the lead's own calls carry no
    # `agent_type` key at all.
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return payload


@pytest.fixture()
def gated(jarvis_home, fake_claude, tmp_path, project):
    """A dispatched work order in a project that gates every privileged action.

    Built through the real dispatch path rather than by hand, so the environment the hook
    reads is the one `_write_worker_settings` actually wrote — a hand-rolled env would
    let this file keep passing while real workers lost their gate config.
    """
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    catalog = tmp_path / "catalog-gated.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project",
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }))
    ops.start_os(str(catalog), foreground=True)
    daemon = Daemon(load_catalog(catalog))
    wo = ops.create_work_order("proj_a", "ship it", description="merge the PR")
    daemon.tick()

    settings = json.loads(
        (project / ".jarvis" / "worker-settings" / f"{wo['id']}.json").read_text())
    store = ProjectStore(project)
    yield store, wo, settings["env"]
    store.close()


def test_a_gate_the_lead_trips_records_no_seat(gated):
    store, wo, env = gated

    handle_hook(gate_payload("gh pr merge 12 --squash"), env)

    approval = store.list_approvals(wo["id"])[0]
    assert approval["agent_type"] is None


def test_a_gate_a_seat_trips_says_which_seat(gated):
    """`JARVIS_WO_ID` is per-session, so the request is filed against the work order
    whichever agent attempted the command. That ownership is correct — the lead is
    answerable for what its team did — but without the seat on the record the audit trail
    says the planner ran a command the architect ran."""
    store, wo, env = gated

    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-architect"), env)

    approval = store.list_approvals(wo["id"])[0]
    assert approval["wo_id"] == wo["id"]
    assert approval["agent_type"] == "jarvis-architect"


def test_the_reviewer_is_told_a_seat_attempted_it(gated):
    """Whoever decides sees only the question text, never the session. If the seat is on
    the row but not in the prose, the reviewer still reads it as the worker's attempt."""
    store, wo, env = gated

    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-architect"), env)

    approval = store.list_approvals(wo["id"])[0]
    question = ops.show_gate(approval["id"])["neo_question"]
    assert "jarvis-architect" in question["question"]
    assert "seat" in question["question"]


def test_the_timeline_says_which_seat_asked(gated):
    """A timeline event whose renderer reads a key nobody writes is blank forever, and
    the level assertions keep passing. So the detail is asserted, not just the kind."""
    from jarvis import timeline

    store, wo, env = gated
    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-architect"), env)

    rows = timeline.build_timeline(wo, store.list_events(wo["id"]), [])
    asked = [r for r in rows if r["kind"] == "gate_requested"]
    assert asked and "jarvis-architect" in asked[0]["label"], asked


def test_the_lead_still_gets_an_unqualified_line(gated):
    """The overwhelming majority of gates are tripped by a plain worker with no team at
    all. Their record must read exactly as it did before this column existed."""
    from jarvis import timeline

    store, wo, env = gated
    handle_hook(gate_payload("gh pr merge 12 --squash"), env)

    rows = timeline.build_timeline(wo, store.list_events(wo["id"]), [])
    asked = [r for r in rows if r["kind"] == "gate_requested"]
    assert asked and "seat" not in asked[0]["label"], asked
    approval = store.list_approvals(wo["id"])[0]
    question = ops.show_gate(approval["id"])["neo_question"]
    assert question["question"].startswith("PRIVILEGED ACTION REQUEST")
    assert "The worker for work order" in question["question"]


def test_gate_request_from_the_cli_never_carries_a_seat(gated):
    """`jarvis gate request` is a shell command, so whoever ran it had a shell — and the
    seats have none. Recorded so the asymmetry is deliberate rather than an omission."""
    store, wo, env = gated

    ops.request_gate_approval(wo["id"], "gh pr merge 12 --squash",
                              why="tests pass", evidence="PR 12")

    assert store.list_approvals(wo["id"])[0]["agent_type"] is None


def test_the_column_is_additive_for_a_database_that_predates_it(project, jarvis_home):
    """`approvals` is migrated through ADDED_COLUMNS, so an existing project's database
    gains the column on open and every historical row reads NULL."""
    store = ProjectStore(project)
    try:
        cols = {r["name"] for r in
                store.conn.execute("PRAGMA table_info(approvals)").fetchall()}
        assert "agent_type" in cols
    finally:
        store.close()


def test_json_of_a_gate_shows_the_seat(gated):
    """`jarvis gate show --json` is what an operator reads; the row is dumped whole, so
    the column reaches them without a rendering change."""
    store, wo, env = gated
    handle_hook(gate_payload("gh pr merge 12 --squash", "jarvis-test-lead"), env)

    data = ops.show_gate(store.list_approvals(wo["id"])[0]["id"])
    assert data["agent_type"] == "jarvis-test-lead"
    assert json.dumps(data, default=str)  # and it survives the CLI's serialisation
