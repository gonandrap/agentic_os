"""Per-project wiring: which of the USER'S MCP servers, skills and plugins reach a
worker (issue #164 item 4; spec docs/superpowers/specs/2026-09-16-per-project-wiring.md).

Three claims are load-bearing here and each has its own test rather than riding on
another's assertions:

* the SHIPPED DEFAULT changes nothing — the worker settings file is what it was, down to
  the Serena permission rules;
* a deselection reaches the SPAWN, through the one file `dispatch._write_worker_settings`
  writes, for ordinary workers and for a feature order's planner and manager alike;
* a deselection is COHERENT — unwiring Serena also takes the permission rules that name
  its tools, the navigation briefing that recommends it, and the seat definitions that
  open by telling a subagent to call it.

What this file does NOT prove: that Claude Code honours the keys. That is not a claim
about Jarvis's code and cannot be asserted from here — it was measured live against
2.1.272 (see the PR body, and `jarvis learn` topic `worker environment`).
"""

from __future__ import annotations

import json

import pytest

from jarvis import bootstrap, ops, wiring
from jarvis.catalog import CatalogError, ProjectSpec, WiringConfig, parse_catalog
from jarvis.dispatch import _write_worker_settings, serena_allow_rules
from jarvis.worker_brief import navigation_section

SERENA = "serena@claude-plugins-official"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """`ops.set_config` refuses a worker session, and this suite is run by one."""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def _settings(project, w: WiringConfig, wo: dict | None = None) -> dict:
    spec = ProjectSpec(name="proj_a", path=project, wiring=w)
    out = _write_worker_settings(spec, wo or {"id": "wo-wiring", "title": "t"})
    return json.loads(out.read_text())


# -- the default -----------------------------------------------------------------------

def test_the_shipped_default_wires_everything_and_writes_nothing(project, jarvis_home):
    """The whole opt-out promise in one assertion: a project that never opens /config
    launches the session it launched before this block existed."""
    settings = _settings(project, WiringConfig())
    for key in ("disableClaudeAiConnectors", "disableBundledSkills",
                "enabledPlugins", "skillOverrides"):
        assert key not in settings, f"{key} reached a worker at the shipped default"
    assert settings["env"]["JARVIS_SERENA"] == "1"
    assert set(serena_allow_rules()) <= set(settings["permissions"]["allow"])


def test_no_file_of_the_users_own_is_written_by_a_deselection(monkeypatch, tmp_path,
                                                              project, jarvis_home,
                                                              fake_claude, catalog_file):
    """THE load-bearing constraint, pinned on the FILESYSTEM: the user's Claude
    configuration is read to populate the list and never written, and neither is the
    project's own `.claude/` tree, which belongs to the sessions they open themselves.

    Snapshot-and-compare rather than an assertion on a return value, because the claim
    is about absence: nothing an output contains can catch a write to a file the test
    never looked at. The whole path runs between the two snapshots — discovery,
    `ops.set_wiring`, and the dispatch seam that turns the result into a spawn."""
    home = tmp_path / "user-home"
    skill = home / ".claude" / "skills" / "note-taker"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: note-taker\ndescription: takes notes\n---\nbody")
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {SERENA: True}}))
    # The file `/mcp disable` would write `disabledMcpServers` into, which is why a
    # hand-added server gets no button: that lever cannot be pulled from here.
    (home / ".claude.json").write_text(json.dumps({"projects": {str(project): {}}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    assert wiring.claude_home() == home / ".claude"

    ops.start_os(str(catalog_file), foreground=True)  # writes `<project>/.claude`; then
    (project / ".claude" / "settings.local.json").write_text(  # this one is the user's
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))

    def snapshot() -> dict[str, tuple[bytes, int]]:
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for root in (home, project / ".claude")
                for p in sorted(root.rglob("*")) if p.is_file()}

    before = snapshot()
    assert len(before) >= 4

    monkeypatch.setattr(wiring, "read_plugins", lambda **k: [
        {"id": SERENA, "enabled": True, "scope": "user", "installPath": str(tmp_path)}])
    monkeypatch.setattr(wiring, "read_mcp_servers",
                        lambda **k: [("plugin:serena:serena", "uvx")])
    inv = wiring.discover(refresh=True)
    # Proof the snapshotted tree was READ, without which "unchanged" would be trivial.
    assert "note-taker" in {i.name for i in inv.items}

    ops.set_wiring(f"{wiring.LEVER_PLUGIN}{SERENA}", False, project="proj_a")
    w = ops.wiring_config("proj_a")
    assert w.disabled_plugins == (SERENA,)
    # ...and it did take effect, so the comparison below is about a live deselection.
    assert _settings(project, w)["enabledPlugins"] == {SERENA: False}

    assert snapshot() == before


def test_a_project_inherits_the_fleet_base_and_may_deselect_more(tmp_path):
    """`os.wiring` is a base, not a default nobody can reach: the fleet answer holds
    for a project that says nothing, field by field."""
    catalog = parse_catalog({
        "os": {"wiring": {"bundled_skills": False, "disabled_plugins": ["a@m"]}},
        "projects": [
            {"name": "quiet", "path": str(tmp_path)},
            {"name": "loud", "path": str(tmp_path),
             "wiring": {"claude_ai_connectors": False, "disabled_plugins": ["b@m"]}},
        ],
    })
    quiet, loud = catalog.project("quiet"), catalog.project("loud")
    assert quiet.wiring.bundled_skills is False
    assert quiet.wiring.disabled_plugins == ("a@m",)
    assert quiet.wiring.claude_ai_connectors is True
    # Field-level: `loud` names two fields and keeps the fleet's answer for the third,
    # and its own list REPLACES rather than merges (ScheduleConfig.jobs' rule).
    assert loud.wiring.claude_ai_connectors is False
    assert loud.wiring.bundled_skills is False
    assert loud.wiring.disabled_plugins == ("b@m",)


def test_a_list_that_is_not_a_list_is_refused_by_name(tmp_path):
    with pytest.raises(CatalogError) as e:
        parse_catalog({"os": {}, "projects": [
            {"name": "p", "path": str(tmp_path), "wiring": {"disabled_skills": "caveman"}},
        ]})
    assert "disabled_skills" in str(e.value)


def test_an_id_naming_nothing_installed_is_inert_rather_than_fatal(tmp_path):
    """A catalog is read by the daemon on a machine whose Claude configuration moves
    under it. Uninstalling a deselected plugin must not take the fleet down."""
    catalog = parse_catalog({"os": {}, "projects": [
        {"name": "p", "path": str(tmp_path),
         "wiring": {"disabled_plugins": ["never-installed@nowhere"]}},
    ]})
    assert catalog.project("p").wiring.disabled_plugins == ("never-installed@nowhere",)


# -- the patch that reaches a spawn -----------------------------------------------------

@pytest.mark.parametrize("config,expected", [
    (WiringConfig(claude_ai_connectors=False), {"disableClaudeAiConnectors": True}),
    (WiringConfig(bundled_skills=False), {"disableBundledSkills": True}),
    (WiringConfig(disabled_plugins=("caveman@caveman",)),
     {"enabledPlugins": {"caveman@caveman": False}}),
    (WiringConfig(disabled_skills=("heycrypto-pr",)),
     {"skillOverrides": {"heycrypto-pr": "off"}}),
])
def test_each_deselection_reaches_the_settings_file_as_its_own_key(
        project, jarvis_home, config, expected):
    settings = _settings(project, config)
    for key, value in expected.items():
        assert settings[key] == value


def test_a_deselected_plugin_does_not_clobber_the_projects_own_overrides(project,
                                                                        jarvis_home):
    """`enabledPlugins` is a dict, so the patch must merge into a project's
    `settings_overrides` rather than replace the block."""
    spec = ProjectSpec(name="proj_a", path=project,
                       settings_overrides={"enabledPlugins": {"keep@m": True}},
                       wiring=WiringConfig(disabled_plugins=("drop@m",)))
    out = _write_worker_settings(spec, {"id": "wo-merge", "title": "t"})
    assert json.loads(out.read_text())["enabledPlugins"] == {"keep@m": True,
                                                             "drop@m": False}


# -- coherence: everywhere Serena is named ----------------------------------------------

def test_unwiring_serena_takes_its_permission_rules_with_it(project, jarvis_home):
    """The trap `dispatch.SERENA_READ_TOOLS` documents, seen from the other side: a
    settings file that removes a server and grants its tools in the same breath."""
    settings = _settings(project, WiringConfig(disabled_plugins=(SERENA,)))
    assert not set(serena_allow_rules()) & set(settings["permissions"]["allow"])
    assert settings["env"]["JARVIS_SERENA"] == "0"
    # The rest of the permission block is untouched — this removes rules, not rights.
    assert any(r.startswith("Edit(") for r in settings["permissions"]["allow"])


def test_the_navigation_briefing_stops_recommending_a_server_that_was_removed():
    assert "Serena" in navigation_section()
    without = navigation_section(serena=False)
    assert "Serena" in without.splitlines()[1]  # it says WHY, once
    assert "activate_project" not in without
    assert "find_referencing_symbols" not in without
    assert "Grep" in without


def test_jarvis_brief_navigation_agrees_with_the_dispatch_that_spawned_it(monkeypatch):
    """The briefing is fetched by a SEPARATE process, so the answer travels as env —
    `JARVIS_GATES`' pattern. Without this the worker is told to use what dispatch
    just removed."""
    from jarvis import cli

    monkeypatch.setenv("JARVIS_SERENA", "0")
    args = cli.build_parser().parse_args(["brief", "navigation"])
    out = _capture(cli.cmd_brief, args)
    assert "activate_project" not in out


def test_the_prompts_own_index_line_stops_advertising_it_too(project):
    """`section_index` is in EVERY worker's opening prompt, so a stale "Serena first"
    there is the same defect one layer earlier than the briefing it points at — the rule
    the `gates` line already follows: never point at territory this project lacks."""
    from jarvis.dispatch import build_worker_prompt

    spec = ProjectSpec(name="proj_a", path=project,
                       wiring=WiringConfig(disabled_plugins=(SERENA,)))
    prompt = build_worker_prompt({"id": "wo-i", "title": "t"}, spec)
    assert "Serena first" not in prompt
    assert "- `navigation` — Glob and Grep" in prompt
    assert "Serena first" in build_worker_prompt({"id": "wo-i", "title": "t"},
                                                 ProjectSpec(name="p", path=project))


def test_a_planners_seats_lose_their_serena_tools_and_say_so(project, jarvis_home):
    roots = bootstrap.install_agent_assets(project, kind="planner", serena=False)
    seats = list((roots[-1] / ".claude" / "agents").glob("*.md"))
    assert seats, "a planner still gets its seats"
    for seat in seats:
        text = seat.read_text()
        assert "mcp__serena__" not in text and "mcp__plugin_serena_serena__" not in text
        assert "tools: Read, Grep, Glob" in text
        assert "not wired for this project" in text


def test_the_seats_are_untouched_when_serena_is_wired(project, jarvis_home):
    roots = bootstrap.install_agent_assets(project, kind="planner")
    for seat in (roots[-1] / ".claude" / "agents").glob("*.md"):
        text = seat.read_text()
        assert "mcp__plugin_serena_serena__find_symbol" in text
        assert "not wired for this project" not in text


# -- feature orders: the planner and the manager go through the same seam ---------------

@pytest.mark.parametrize("kind", ["worker", "planner", "manager"])
def test_every_kind_of_work_order_carries_the_projects_selection(project, jarvis_home,
                                                                 kind):
    """The user named work orders AND feature orders. A feature order is a planner, a
    manager and children — all of them spawn through `_write_worker_settings`."""
    settings = _settings(project, WiringConfig(claude_ai_connectors=False),
                         {"id": f"wo-{kind}", "title": "t", "kind": kind})
    assert settings["disableClaudeAiConnectors"] is True


def test_the_planners_prompt_does_not_recommend_an_unwired_serena(project):
    from jarvis.dispatch import build_worker_prompt

    spec = ProjectSpec(name="proj_a", path=project,
                       wiring=WiringConfig(disabled_plugins=(SERENA,)))
    prompt = build_worker_prompt({"id": "wo-p", "title": "t", "kind": "planner",
                                  "parent_id": "fo-1"}, spec)
    assert "activate_project" not in prompt


# -- the levers, and the one place that decides what a lever writes ---------------------

def test_a_lever_round_trips_through_the_catalog_without_a_reason(jarvis_home,
                                                                  fake_claude,
                                                                  catalog_file):
    """Wiring is not a `SAFETY_KEYS` path: unwiring narrows what a worker can reach and
    wiring back restores what the user's own configuration already says, so neither
    demands the justification a permission change does (kn-64f4922c)."""
    ops.start_os(str(catalog_file), foreground=True)
    ops.set_wiring(f"{wiring.LEVER_PLUGIN}{SERENA}", False, project="proj_a")
    assert ops.wiring_config("proj_a").disabled_plugins == (SERENA,)

    ops.set_wiring(f"{wiring.LEVER_PLUGIN}{SERENA}", True, project="proj_a")
    assert ops.wiring_config("proj_a").disabled_plugins == ()
    paths = [c["path"] for row in ops.config_history(limit=10) for c in row["changes"]]
    assert "projects.proj_a.wiring.disabled_plugins" in paths


def test_the_fleet_base_is_settable_from_the_same_lever(jarvis_home, fake_claude,
                                                        catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    ops.set_wiring(wiring.LEVER_CONNECTORS, False)
    assert ops.wiring_config().claude_ai_connectors is False
    # A project that says nothing inherits it — that is what makes this a fleet answer.
    assert ops.wiring_config("proj_a").claude_ai_connectors is False


def test_a_second_identical_deselection_writes_the_same_document(jarvis_home,
                                                                 fake_claude,
                                                                 catalog_file):
    """The lists are sorted, so a deselection made twice is the same catalog and
    therefore the same content-addressed config version."""
    ops.start_os(str(catalog_file), foreground=True)
    first = ops.set_wiring(f"{wiring.LEVER_SKILL}b", False, project="proj_a")
    ops.set_wiring(f"{wiring.LEVER_SKILL}a", False, project="proj_a")
    ops.set_wiring(f"{wiring.LEVER_SKILL}a", True, project="proj_a")
    assert ops.wiring_config("proj_a").disabled_skills == ("b",)
    assert ops.current_config_version() == first["version"]["id"]


def test_an_unknown_lever_is_an_error_not_a_silent_write(jarvis_home, fake_claude,
                                                         catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    with pytest.raises(ops.OpsError):
        ops.set_wiring("nonsense:x", False, project="proj_a")


# -- reading the user's configuration ---------------------------------------------------

def test_a_server_name_maps_to_the_tool_prefix_claude_code_calls_it_under():
    """Both spellings of one server, which is why `SERENA_TOOL_PREFIXES` has two."""
    assert wiring.tool_prefix("plugin:serena:serena") == "mcp__plugin_serena_serena__"
    assert wiring.tool_prefix("serena") == "mcp__serena__"
    assert wiring.tool_prefix("claude.ai Google Drive") == "mcp__claude_ai_Google_Drive__"


def test_the_mcp_listing_is_parsed_leniently(monkeypatch):
    monkeypatch.setattr(wiring, "_claude", lambda *a, **k: (
        "Checking MCP server health…\n\n"
        "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected\n"
        "plugin:serena:serena: uvx --from git+https://x serena start - ✔ Connected\n"
        "rubbish\n"))
    assert wiring.read_mcp_servers() == [
        ("claude.ai Gmail", "https://gmailmcp.googleapis.com/mcp/v1"),
        ("plugin:serena:serena", "uvx --from git+https://x serena start"),
    ]


def test_every_source_gets_the_lever_that_can_actually_unwire_it(monkeypatch, tmp_path):
    """The page may only offer a control that exists. A claude.ai connector has no
    per-server lever — `/mcp disable` writes the user's own config, which this feature
    may not — so it gets the block's, and a server from neither source gets none."""
    (tmp_path / "skills" / "mine").mkdir(parents=True)
    (tmp_path / "skills" / "mine" / "SKILL.md").write_text(
        "---\nname: mine\ndescription: a skill of my own\n---\nbody")
    monkeypatch.setattr(wiring, "claude_home", lambda: tmp_path)
    monkeypatch.setattr(wiring, "read_plugins", lambda **k: [
        {"id": SERENA, "enabled": True, "scope": "user", "installPath": str(tmp_path)},
        {"id": "off@m", "enabled": False, "scope": "user", "installPath": str(tmp_path)},
    ])
    monkeypatch.setattr(wiring, "read_mcp_servers", lambda **k: [
        ("claude.ai Gmail", "https://x"),
        ("plugin:serena:serena", "uvx"),
        ("hand-added", "npx thing"),
    ])
    inv = wiring.discover(refresh=True)
    levers = {i.name: i.lever for i in inv.items}
    assert levers["claude.ai Gmail"] == wiring.LEVER_CONNECTORS
    assert levers["plugin:serena:serena"] == f"{wiring.LEVER_PLUGIN}{SERENA}"
    assert levers["hand-added"] == ""
    assert levers["mine"] == f"{wiring.LEVER_SKILL}mine"
    assert levers["Claude Code's bundled skills"] == wiring.LEVER_BUNDLED
    # A plugin the USER has turned off is not Jarvis's to list as wired.
    assert "off@m" not in levers
    # Unwiring the plugin unwires the server it provides — one lever, both rows.
    applied = wiring.applied(WiringConfig(disabled_plugins=(SERENA,)), inv)
    assert {i.name for i in applied.unwired} == {SERENA, "plugin:serena:serena"}


def test_a_source_that_cannot_be_read_is_a_line_rather_than_an_exception(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("claude: not found")

    monkeypatch.setattr(wiring, "read_plugins", boom)
    monkeypatch.setattr(wiring, "read_mcp_servers", boom)
    inv = wiring.discover(refresh=True)
    assert len(inv.errors) == 2
    assert any(i.lever == wiring.LEVER_BUNDLED for i in inv.items)


def _capture(fn, args) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return buf.getvalue()
