"""Nothing bounded one MCP tool call, and the pattern that hung one was still writable.

docs/superpowers/specs/2026-09-29-a-runaway-tool-call-is-not-a-slow-subagent.md §3 and
§4, issue #845: Serena's `search_for_pattern` backtracked catastrophically over a
2659-line file and burned 18m50s of CPU on ONE call. Two guards here — the timeout
Claude Code applies itself, and the `PreToolUse` refusal of the shape that hangs.

The alarm half of the same spec (§1, §2) is in `tests/test_inspection.py`.
"""

from __future__ import annotations

import json

import pytest

from jarvis import catalog, dispatch, hooks
from jarvis.catalog import CatalogError, ProjectSpec, WorkerDefaults
from jarvis.dispatch import _write_worker_settings


# -- §3: a per-tool-call ceiling in the worker's environment ---------------------------


def _env(project, *, worker: WorkerDefaults | None = None,
         overrides: dict | None = None, wo_id: str = "wo-mcp01") -> dict:
    spec = ProjectSpec(name="proj_a", path=project, description="",
                       worker=worker or WorkerDefaults(),
                       settings_overrides=overrides or {})
    return json.loads(_write_worker_settings(spec, {"id": wo_id,
                                                    "title": "t"}).read_text())["env"]


def test_every_worker_is_launched_with_an_mcp_tool_timeout(project, jarvis_home):
    """A STRING, like every other value in that dict: Claude Code's settings `env` is a
    `Record<string,string>` and an integer there is a settings file the CLI may reject
    wholesale, taking the hooks and permissions down with it."""
    assert _env(project)["MCP_TOOL_TIMEOUT"] == "300000"


def test_a_project_with_a_slow_mcp_server_raises_it(project, jarvis_home):
    """Per project, because a timeout is a claim about what is normal for THAT server."""
    slow = _env(project, worker=WorkerDefaults(mcp_tool_timeout_ms=900_000))

    assert slow["MCP_TOOL_TIMEOUT"] == "900000"
    assert _env(project, wo_id="wo-mcp02")["MCP_TOOL_TIMEOUT"] == "300000"


def test_the_catalog_setting_wins_over_settings_overrides(project, jarvis_home):
    """Pinned rather than incidental: `dispatch`'s `env.update` is the merge point and it
    beats both `settings.base.json` and the project's `settings_overrides`, which is what
    makes the catalog the single source of the value."""
    env = _env(project, worker=WorkerDefaults(mcp_tool_timeout_ms=600_000),
               overrides={"env": {"MCP_TOOL_TIMEOUT": "60"}})

    assert env["MCP_TOOL_TIMEOUT"] == "600000"


def test_a_millisecond_value_under_a_second_is_refused_naming_the_key(tmp_path):
    """It arrives by a typo in a `jarvis config set`, and a sub-second ceiling would make
    every MCP call in the project fail."""
    with pytest.raises(CatalogError) as e:
        catalog.parse_catalog({"projects": [{"name": "a", "path": str(tmp_path),
                                             "worker": {"mcp_tool_timeout_ms": 5}}]})

    assert "mcp_tool_timeout_ms" in str(e.value)


# -- §4: the hook that refuses a catastrophic pattern ----------------------------------


#: The pattern from §1 of the spec. Nested unbounded quantifier AND two newline-crossing
#: wildcards — it fails both rules.
HUNG_PATTERN = r"^def .*\n(.*\n)*?.*return"


def _payload(pattern, **rest) -> dict:
    tool_input: dict = {"relative_path": "src/jarvis"}
    if pattern is not None:
        tool_input["substring_pattern"] = pattern
    tool_input.update(rest)
    return {"tool_name": "mcp__serena__search_for_pattern", "tool_input": tool_input}


def test_the_pattern_that_hung_serena_for_nineteen_minutes_is_refused():
    decision = hooks.search_pattern_decision(_payload(HUNG_PATTERN), {})

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    # A refusal that does not say what to type instead buys a reworded retry.
    assert "[^\\n]*" in reason
    assert "find_symbol" in reason


@pytest.mark.parametrize("pattern,rest", [
    (r"def [^\n]*process", {}),            # the very thing the denial recommends
    (r"class .*Store", {}),                # ONE wildcard: the common case, not the bug
    ("TODO", {}),                          # a literal
    (r".*foo.*bar", {"multiline": False}), # line-scoped: `.` cannot cross a newline
    (r"^def .{0,200}return", {}),          # a counted range is bounded
])
def test_ordinary_searches_are_allowed(pattern, rest):
    """This hook sits in front of every worker's primary search tool: a predicate that
    over-refuses is worse than the hang it prevents (spec §7 rejected alternative 6)."""
    assert hooks.search_pattern_decision(_payload(pattern, **rest), {}) is None


def test_a_nested_quantifier_is_refused_even_line_scoped():
    """Rule 2, and it does not need DOTALL: `(.*\\n)*` is the classic exponential shape."""
    decision = hooks.search_pattern_decision(
        _payload(r"(.*\n)*x", multiline=False), {})

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("payload", [
    {"tool_name": "mcp__serena__search_for_pattern", "tool_input": {}},
    {"tool_name": "mcp__serena__search_for_pattern", "tool_input": "not a dict"},
    {"tool_name": "mcp__serena__search_for_pattern"},
    {"tool_name": "mcp__serena__search_for_pattern",
     "tool_input": {"substring_pattern": 7}},
])
def test_an_input_the_hook_cannot_read_is_allowed(payload):
    """The tool's schema is not Jarvis's to own: a renamed parameter must not take every
    worker's search tool offline. The cost is that a schema change disarms the hook,
    which is why §2's alarm and §3's timeout sit behind it."""
    assert hooks.search_pattern_decision(payload, {}) is None


def test_the_matcher_names_both_serena_tool_names(project, jarvis_home):
    """A plugin install produces the long prefix and `claude mcp add serena` the short
    one; Jarvis configures no MCP server, so it cannot know which. Built from
    `dispatch.SERENA_TOOL_PREFIXES` rather than spelled a third time."""
    spec = ProjectSpec(name="proj_a", path=project, description="")
    settings = json.loads(
        _write_worker_settings(spec, {"id": "wo-mcp03", "title": "t"}).read_text())
    matchers = [e.get("matcher", "") for e in settings["hooks"]["PreToolUse"]]

    for prefix in dispatch.SERENA_TOOL_PREFIXES:
        assert any(f"{prefix}search_for_pattern" in m for m in matchers)


def test_the_refusal_is_reached_through_the_preflight_mcp_branch():
    """Placed FIRST in that branch: `investigator_write_decision` returns `None` for a
    read-only Serena tool, so an investigator would otherwise fall through to nothing."""
    decision = hooks.preflight_decision(_payload(HUNG_PATTERN),
                                        {"JARVIS_WO_ID": "wo-1"})

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
