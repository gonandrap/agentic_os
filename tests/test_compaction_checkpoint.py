"""A compacted worker must not lose the parts of its brief that have to be exact.

Auto-compaction summarizes the conversation and discards the rest. Claude Code's summary
is a model's account of what happened; what it cannot be trusted to carry verbatim is the
work order's identifiers and contract — the id, the branch, the PR, which assumptions are
already recorded, the finishing protocol. Those Jarvis already holds as structured state,
so the re-assertion is rendered from the record rather than summarized, and is exact by
construction.

The delivery is two hooks and a flag file, because neither compaction hook can inject:
`PreCompact` arms, and the next `PostToolUse` spends the flag and returns the brief as
`additionalContext`. Design and measurements:
docs/superpowers/specs/2026-08-10-resume-cost-and-the-cache.md
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.hooks import compaction_flag, handle_hook
from jarvis.project_store import ProjectStore


@pytest.fixture()
def wo(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return ops.create_work_order("proj_a", "make the thing work", origin="jarvis",
                                 description="The user's original ask, verbatim.")


def env(project, wo_id):
    return {
        "JARVIS_WO_ID": wo_id,
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
    }


def pre_compact(trigger="auto"):
    return {"hook_event_name": "PreCompact", "session_id": "sess-1", "trigger": trigger}


def post_tool(tool="Bash"):
    return {"hook_event_name": "PostToolUse", "session_id": "sess-1",
            "tool_name": tool, "tool_input": {"command": "ls"}}


def context_of(result):
    return (result or {}).get("hookSpecificOutput", {}).get("additionalContext")


# -- arming -----------------------------------------------------------------------------


def test_precompact_arms_the_flag_and_records_that_it_happened(wo, project):
    handle_hook(pre_compact(), env(project, wo["id"]))

    assert compaction_flag(project, wo["id"]).exists()
    store = ProjectStore(project)
    try:
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
    finally:
        store.close()
    assert "compacted" in kinds, (
        "a compaction is invisible in the record unless the hook writes it down — "
        f"events were {kinds}")


def test_an_interactive_session_is_not_managed(project):
    """No JARVIS_WO_ID means a session the user started; Jarvis does not touch it."""
    assert handle_hook(pre_compact(), {"JARVIS_PROJECT_PATH": str(project)}) is None


# -- delivery ---------------------------------------------------------------------------


def test_the_brief_arrives_on_the_next_tool_call(wo, project):
    handle_hook(pre_compact(), env(project, wo["id"]))

    ctx = context_of(handle_hook(post_tool(), env(project, wo["id"])))

    assert ctx, "the armed compaction produced no additionalContext"
    assert wo["id"] in ctx
    assert "The user's original ask, verbatim." in ctx, (
        "the original ask is the one thing a summary is most likely to paraphrase")
    assert f"jarvis wo finish {wo['id']}" in ctx, "the finishing protocol did not survive"


def test_it_carries_the_identifiers_a_summary_would_blur(wo, project):
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], branch="worktree-x", worktree=wo["id"],
                                pr_url="https://github.com/o/r/pull/7")
    finally:
        store.close()
    handle_hook(pre_compact(), env(project, wo["id"]))

    ctx = context_of(handle_hook(post_tool(), env(project, wo["id"])))

    assert "worktree-x" in ctx and "https://github.com/o/r/pull/7" in ctx


def test_already_recorded_assumptions_are_named_so_they_are_not_recorded_twice(wo, project):
    store = ProjectStore(project)
    try:
        store.add_assumption(wo["id"], "Chose the 150k window because 90% peak under 120k")
    finally:
        store.close()
    handle_hook(pre_compact(), env(project, wo["id"]))

    ctx = context_of(handle_hook(post_tool(), env(project, wo["id"])))

    assert "150k window" in ctx


def test_delivery_is_not_gated_on_the_worker_editing_a_file(wo, project):
    """The first call after a compaction is usually Bash. Waiting for Write/Edit would
    leave the worker running blind until it happened to make one."""
    handle_hook(pre_compact(), env(project, wo["id"]))

    assert context_of(handle_hook(post_tool(tool="Bash"), env(project, wo["id"])))


# -- exactly once -----------------------------------------------------------------------


def test_the_flag_is_spent_once(wo, project):
    handle_hook(pre_compact(), env(project, wo["id"]))

    first = context_of(handle_hook(post_tool(), env(project, wo["id"])))
    second = context_of(handle_hook(post_tool(), env(project, wo["id"])))

    assert first and second is None, (
        "re-injecting on every tool call would re-grow the context the compaction "
        "just reclaimed")
    assert not compaction_flag(project, wo["id"]).exists()


def test_no_compaction_means_no_injection(wo, project):
    assert context_of(handle_hook(post_tool(), env(project, wo["id"]))) is None


def test_a_second_compaction_re_arms(wo, project):
    handle_hook(pre_compact(), env(project, wo["id"]))
    handle_hook(post_tool(), env(project, wo["id"]))

    handle_hook(pre_compact(), env(project, wo["id"]))

    assert context_of(handle_hook(post_tool(), env(project, wo["id"])))


# -- wiring -----------------------------------------------------------------------------


def test_the_hooks_are_actually_registered():
    """The handler is only reachable if the injected settings ask for these events.

    Both halves are load-bearing and neither fails loudly: an unregistered `PreCompact`
    never arms, and a `PostToolUse` still matching only `Write|Edit|NotebookEdit` would
    hold the brief until the worker happened to edit a file.
    """
    from jarvis.bootstrap import build_settings

    hooks = build_settings({})["hooks"]

    assert "PreCompact" in hooks, "nothing arms the checkpoint"
    post = hooks["PostToolUse"]
    assert all("matcher" not in entry for entry in post), (
        f"PostToolUse must fire for every tool, not a subset; got {post}")
