"""Workers that write to Claude Code's own per-project memory dir must have that
knowledge mirrored into the Jarvis knowledge base.

Claude Code ships a built-in file memory (`<claude config>/projects/<slug>/memory/*.md`).
A worker told to "remember this" reaches for it by reflex, and until this capture existed
the knowledge vanished: the file lives outside the repo, outside the work order record,
and outside `jarvis learn` — so the user saw "project memory updated" in a work order
and nothing at all under Knowledge.
"""

from __future__ import annotations

import json

import pytest

from jarvis import ops
from jarvis.central_store import CentralStore
from jarvis.hooks import handle_hook, memory_topic
from jarvis.project_store import ProjectStore


@pytest.fixture()
def memory_dir(tmp_path):
    """A stand-in for ~/.claude/projects/<slug>/memory."""
    d = tmp_path / ".claude" / "projects" / "-home-user-proj-a" / "memory"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def wo(jarvis_home, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return ops.create_work_order("proj_a", "do the thing", origin="jarvis")


def worker_env(project, wo_id):
    return {
        "JARVIS_WO_ID": wo_id,
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
    }


def write_payload(path, tool="Write"):
    return {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-1",
        "cwd": str(path.parent),
        "tool_name": tool,
        "tool_input": {"file_path": str(path)},
    }


# -- path recognition ---------------------------------------------------------------


def test_memory_topic_recognises_claude_memory_files(memory_dir):
    assert memory_topic(str(memory_dir / "project_tesis.md")) == "project_tesis"
    # the index is a table of contents, not knowledge
    assert memory_topic(str(memory_dir / "MEMORY.md")) is None
    # anything outside a claude projects/<slug>/memory dir is not memory
    assert memory_topic(str(memory_dir.parent / "notes.md")) is None
    assert memory_topic("/home/user/proj/memory/thoughts.md") is None
    assert memory_topic(str(memory_dir / "notes.txt")) is None


# -- capture ------------------------------------------------------------------------


def test_worker_memory_write_becomes_knowledge(wo, project, memory_dir):
    path = memory_dir / "project_tesis.md"
    path.write_text(
        "---\nname: project-tesis\ndescription: thesis status\n"
        "metadata:\n  type: project\n---\n\nPhase 1-2 sit unmerged in PRs #1-#2.\n"
    )

    result = handle_hook(write_payload(path), worker_env(project, wo["id"]))
    assert result and result.get("captured") == "project_tesis"

    central = CentralStore()
    try:
        rows = central.search_knowledge("", limit=50)
    finally:
        central.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["project"] == "proj_a"
    assert row["topic"] == "project_tesis"
    assert "Phase 1-2 sit unmerged" in row["content"]
    assert "claude-memory" in row["tags"]

    # and the work order record shows it happened
    store = ProjectStore(project)
    try:
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
    finally:
        store.close()
    assert "learning_captured" in kinds


def test_rewriting_the_same_memory_file_refreshes_one_entry(wo, project, memory_dir):
    path = memory_dir / "project_tesis.md"
    env = worker_env(project, wo["id"])

    path.write_text("first version\n")
    handle_hook(write_payload(path), env)
    path.write_text("second version\n")
    handle_hook(write_payload(path, tool="Edit"), env)

    central = CentralStore()
    try:
        rows = central.search_knowledge("", limit=50)
    finally:
        central.close()
    assert len(rows) == 1
    assert "second version" in rows[0]["content"]
    assert "first version" not in rows[0]["content"]


def test_unchanged_rewrite_is_not_recorded_twice(wo, project, memory_dir):
    path = memory_dir / "project_tesis.md"
    env = worker_env(project, wo["id"])
    path.write_text("same content\n")
    handle_hook(write_payload(path), env)
    assert handle_hook(write_payload(path), env) is None

    store = ProjectStore(project)
    try:
        captured = [e for e in store.list_events(wo["id"]) if e["kind"] == "learning_captured"]
    finally:
        store.close()
    assert len(captured) == 1


def test_ordinary_worker_writes_are_ignored(wo, project, memory_dir):
    path = project / "README.md"
    path.write_text("# proj\nchanged\n")
    assert handle_hook(write_payload(path), worker_env(project, wo["id"])) is None

    central = CentralStore()
    try:
        assert central.search_knowledge("", limit=50) == []
    finally:
        central.close()


def test_non_worker_session_memory_write_is_ignored(wo, project, memory_dir):
    """An interactive session in a managed project is not a worker — no capture."""
    path = memory_dir / "project_tesis.md"
    path.write_text("interactive note\n")
    assert handle_hook(write_payload(path), {"JARVIS_PROJECT_PATH": str(project)}) is None

    central = CentralStore()
    try:
        assert central.search_knowledge("", limit=50) == []
    finally:
        central.close()


def test_missing_memory_file_does_not_raise(wo, project, memory_dir):
    path = memory_dir / "gone.md"
    assert handle_hook(write_payload(path), worker_env(project, wo["id"])) is None


# -- wiring -------------------------------------------------------------------------


def test_injected_settings_register_the_posttooluse_hook(jarvis_home):
    from jarvis.bootstrap import build_settings

    hooks = build_settings({})["hooks"]
    assert "PostToolUse" in hooks
    matchers = [h.get("matcher", "") for h in hooks["PostToolUse"]]
    assert any("Write" in m and "Edit" in m for m in matchers)


def test_worker_prompt_points_memory_at_the_knowledge_base(project):
    from jarvis.catalog import ProjectSpec
    from jarvis.dispatch import build_worker_prompt

    spec = ProjectSpec(name="proj_a", path=project, description="")
    prompt = build_worker_prompt({"id": "wo-1", "title": "t", "description": "d"}, spec, [])
    assert "jarvis learn add" in prompt
    lowered = prompt.lower()
    assert "memory" in lowered
    # it must be explicit that private memory files are not the OS's memory
    assert "knowledge base" in lowered


def test_captured_learning_is_offered_to_the_next_worker(wo, project, memory_dir):
    path = memory_dir / "project_tesis.md"
    path.write_text("PRs #1-#2 are unmerged.\n")
    handle_hook(write_payload(path), worker_env(project, wo["id"]))

    central = CentralStore()
    try:
        rows = central.relevant_knowledge("proj_a", limit=8)
    finally:
        central.close()
    assert any("PRs #1-#2 are unmerged." in r["content"] for r in rows)


def test_timeline_renders_the_capture(wo, project, memory_dir):
    from jarvis.timeline import build_timeline

    path = memory_dir / "project_tesis.md"
    path.write_text("something durable\n")
    handle_hook(write_payload(path), worker_env(project, wo["id"]))

    store = ProjectStore(project)
    try:
        fresh = store.get_work_order(wo["id"])
        entries = build_timeline(fresh, store.list_events(wo["id"]), [])
    finally:
        store.close()
    entry = next(e for e in entries if e["kind"] == "learning_captured")
    assert entry["level"] == "signal"
    assert entry["label"] == "Learning captured"
    assert "project_tesis" in entry["detail"]
    assert json.dumps(entry)  # serialisable for the UI
