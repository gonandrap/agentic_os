"""`jarvis navigation` — the fleet's read volume, split by side.

Spec: docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md
§2 and test-plan item 6. The reader moved out of `navigation` under §3 of
`.jarvis/features/fo-b9a3fb06/sections/wo-f4b04708.md`; the classifier it now calls is
pinned by `tests/test_navigation.py`.

NO TEST HERE READS A LIVE TRANSCRIPT. Every tree is written under
`monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, …)`, the pattern
`tests/test_inspection.py`'s `write_transcript` fixture set, with the root `conftest.py`
isolation gate as the floor under it.
"""

import json
import os
import time

import pytest

from jarvis import nav_volume, usage
from jarvis.catalog import NavigationConfig

CFG = NavigationConfig()


# -- fixture plumbing ------------------------------------------------------------------

def tool_use_row(tool_id: str, name: str, **payload) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": tool_id,
                                     "name": name, "input": payload}]}}


def tool_result_row(tool_id: str, content) -> dict:
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
                                     "content": content}]}}


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """A transcript root the OS reads instead of `~/.claude/projects`."""
    root = tmp_path / "projects"
    root.mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict], *, slug: str = "-proj",
              subagents: dict[str, list[dict]] | None = None):
        directory = root / slug
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        for name, sub_rows in (subagents or {}).items():
            sub_dir = directory / session_id / "subagents"
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / f"{name}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in sub_rows))
        return path

    write.root = root
    return write


# -- attribution: bytes land on the call that produced them ---------------------------

def test_result_bytes_are_attributed_to_the_producing_tool_use(tree):
    path = tree("s1", [
        tool_use_row("t1", "Bash", command="cat src/x.py"),
        tool_result_row("t1", "x" * 100),
        tool_use_row("t2", "mcp__serena__find_symbol", name_path_pattern="f"),
        tool_result_row("t2", "y" * 10),
        tool_use_row("t3", "Grep", pattern="foo"),
        tool_result_row("t3", "z" * 5),
    ])

    vol = nav_volume.read_transcript(path, nav_volume.SIDE_LEAD, CFG)

    assert vol.side == nav_volume.SIDE_LEAD
    assert vol.transcripts == 1
    assert (vol.nav_bash_calls, vol.code_nav_bash_calls) == (1, 1)
    assert (vol.symbol_calls, vol.text_search_calls) == (1, 1)
    assert vol.result_bytes == 115
    assert vol.nav_bash_bytes == 100
    assert vol.code_nav_bash_bytes == 100
    assert vol.symbol_bytes == 10
    assert vol.unattributed_bytes == 0
    assert vol.code_nav_share() == pytest.approx(100 / 115)


def test_an_unknown_tool_use_id_is_reported_and_never_in_a_share(tree):
    """REPORTED, never silently dropped and never in a share's numerator."""
    path = tree("s2", [
        tool_use_row("t1", "Bash", command="cat src/x.py"),
        tool_result_row("t1", "x" * 40),
        tool_result_row("nobody-made-this", "q" * 999),
    ])

    vol = nav_volume.read_transcript(path, nav_volume.SIDE_LEAD, CFG)

    assert vol.unattributed_bytes == 999
    assert vol.result_bytes == 40
    assert vol.code_nav_share() == pytest.approx(1.0)


def test_other_bash_and_read_tool_calls_are_counted_apart(tree):
    path = tree("s3", [
        tool_use_row("t1", "Bash", command="uv run pytest"),
        tool_result_row("t1", "ok"),
        tool_use_row("t2", "Read", file_path="/tmp/x.py"),
        tool_result_row("t2", "body"),
    ])

    vol = nav_volume.read_transcript(path, nav_volume.SIDE_LEAD, CFG)

    assert vol.other_bash_calls == 1
    assert vol.nav_bash_calls == 0
    assert vol.read_tool_calls == 1
    assert vol.result_bytes == 6
    assert vol.code_nav_bash_bytes == 0
    # A zero share is a FINDING; it is not None, because the corpus was measured.
    assert vol.code_nav_share() == 0.0


def test_the_reader_uses_the_strict_classifier_not_a_token_match(tree):
    """§3: a `.py` inside a quoted string is prose, and a bookkeeping read is not
    navigation."""
    path = tree("s9", [
        tool_use_row("t1", "Bash", command='git commit -m "fix pricing.py"'),
        tool_result_row("t1", "x" * 11),
        tool_use_row("t2", "Bash", command="uv run pytest tests/test_x.py"),
        tool_result_row("t2", "x" * 22),
    ])

    vol = nav_volume.read_transcript(path, nav_volume.SIDE_LEAD, CFG)

    assert vol.nav_bash_calls == 0
    assert vol.other_bash_calls == 2
    assert vol.code_nav_bash_bytes == 0


# -- the side split, which comes from the PATH ----------------------------------------

def test_the_side_split_comes_from_the_path_with_one_lead_and_two_subagents(tree):
    tree("s4", [
        tool_use_row("t1", "Bash", command="grep -rn foo src/a.py"),
        tool_result_row("t1", "L" * 300),
    ], subagents={
        "agent-aaa": [tool_use_row("u1", "Bash", command="cat src/b.py"),
                      tool_result_row("u1", "A" * 50)],
        "agent-bbb": [tool_use_row("v1", "mcp__plugin_serena_serena__find_symbol",
                                   name_path_pattern="x"),
                      tool_result_row("v1", "B" * 20)],
    })

    vol = nav_volume.read_session("s4", CFG)

    assert vol.found is True
    lead = vol.sides[nav_volume.SIDE_LEAD]
    sub = vol.sides[nav_volume.SIDE_SUBAGENT]
    assert (lead.transcripts, sub.transcripts) == (1, 2)
    assert lead.code_nav_bash_bytes == 300
    assert sub.code_nav_bash_bytes == 50
    assert sub.symbol_calls == 1
    assert sub.symbol_bytes == 20
    assert sub.result_bytes == 70
    assert sub.code_nav_share() == pytest.approx(50 / 70)
    # Both keys always present, so a renderer never tests for one.
    assert set(vol.sides) == set(nav_volume.SIDES)


def test_a_session_with_no_transcript_is_not_found(tree):
    vol = nav_volume.read_session("nothing-here", CFG)

    assert vol.found is False
    assert vol.sides[nav_volume.SIDE_LEAD].transcripts == 0


def test_an_empty_corpus_has_no_share_rather_than_a_zero_one(tree):
    """None and NEVER 0.0: a zero share is a finding and an unmeasured one is not,
    which is `usage.rewrite_ttl_share`'s rule."""
    vol = nav_volume.read_session("nothing-here", CFG)

    assert vol.sides[nav_volume.SIDE_LEAD].code_nav_share() is None
    assert nav_volume.SideVolume(side=nav_volume.SIDE_LEAD).code_nav_share() is None
    assert vol.as_dict()["sides"]["lead"]["code_nav_share"] is None


# -- the fleet path: read_tree, and its window ----------------------------------------

def test_read_tree_walks_leads_and_subagents_and_names_the_window(tree):
    tree("s5", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                tool_result_row("t1", "x" * 10)],
         subagents={"agent-aaa": [tool_use_row("u1", "Grep", pattern="f"),
                                  tool_result_row("u1", "y" * 7)]})

    vol = nav_volume.read_tree(cfg=CFG, days=7)

    assert vol.found is True
    assert vol.window_days == 7
    assert vol.sides[nav_volume.SIDE_LEAD].transcripts == 1
    assert vol.sides[nav_volume.SIDE_SUBAGENT].transcripts == 1
    assert vol.sides[nav_volume.SIDE_SUBAGENT].text_search_calls == 1


def test_days_excludes_a_file_by_mtime_before_opening_it(tree):
    fresh = tree("fresh", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                           tool_result_row("t1", "x" * 10)])
    stale = tree("stale", [tool_use_row("t2", "Bash", command="cat src/b.py"),
                           tool_result_row("t2", "x" * 9999)])
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))

    windowed = nav_volume.read_tree(cfg=CFG, days=7)
    everything = nav_volume.read_tree(cfg=CFG)

    assert fresh.exists()
    assert windowed.sides[nav_volume.SIDE_LEAD].transcripts == 1
    assert windowed.sides[nav_volume.SIDE_LEAD].result_bytes == 10
    assert everything.sides[nav_volume.SIDE_LEAD].transcripts == 2
    assert everything.window_days is None


def test_a_sibling_project_with_a_shared_slug_prefix_is_not_folded_in(tree):
    """`slug_of` maps EVERY non-alphanumeric character to `-`, so `/ws/jarvis`'s slug is
    a bare-`startswith` prefix of `/ws/jarvis_os`'s. PR 927 review: three of four seats.

    Two layers, because `slug_of` is not injective: the separator rule rejects a slug
    that merely shares characters, and `slug_exclude` — the catalog's other projects —
    rejects a sibling whose own slug is longer and matches too.
    """
    mine = nav_volume.slug_of("/ws/jarvis")
    theirs = nav_volume.slug_of("/ws/jarvis_os")
    tree("lead", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                  tool_result_row("t1", "x" * 10)], slug=mine)
    tree("wt", [tool_use_row("t2", "Bash", command="cat src/b.py"),
                tool_result_row("t2", "x" * 20)],
         slug=nav_volume.slug_of("/ws/jarvis/.claude/worktrees/wo-1"))
    tree("sib", [tool_use_row("t3", "Bash", command="cat src/c.py"),
                 tool_result_row("t3", "x" * 400)], slug=theirs)
    tree("sibwt", [tool_use_row("t4", "Bash", command="cat src/d.py"),
                   tool_result_row("t4", "x" * 800)],
         slug=nav_volume.slug_of("/ws/jarvis_os/.claude/worktrees/wo-2"))

    vol = nav_volume.read_tree(cfg=CFG, slug_prefix=mine, slug_exclude=(theirs,))

    # The project's own transcript and its own worktree's — and NEITHER of the sibling's.
    assert vol.sides[nav_volume.SIDE_LEAD].transcripts == 2
    assert vol.sides[nav_volume.SIDE_LEAD].result_bytes == 30

    # And the separator rule on its own: a prefix that is not followed by the `-` a path
    # separator becomes is not a parent directory, it is a different path.
    partial = nav_volume.read_tree(cfg=CFG, slug_prefix=nav_volume.slug_of("/ws/jarvi"))
    assert partial.sides[nav_volume.SIDE_LEAD].transcripts == 0
    assert partial.found is False


def test_a_sibling_projects_slug_comes_from_the_catalog(tree, monkeypatch):
    """The exclusion list is not guessed: it is every OTHER project the catalog names
    whose slug would match this one's too."""
    from pathlib import Path

    from jarvis import ops
    from jarvis.catalog import Catalog, OsConfig, ProjectSpec

    catalog = Catalog(os=OsConfig(),
                      projects=[ProjectSpec(name="jarvis", path=Path("/ws/jarvis")),
                                ProjectSpec(name="jarvis_os",
                                            path=Path("/ws/jarvis_os"))])
    monkeypatch.setattr(ops, "resolve_catalog", lambda *a, **k: catalog)
    tree("lead", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                  tool_result_row("t1", "x" * 10)],
         slug=nav_volume.slug_of("/ws/jarvis"))
    tree("sib", [tool_use_row("t2", "Bash", command="cat src/c.py"),
                 tool_result_row("t2", "x" * 400)],
         slug=nav_volume.slug_of("/ws/jarvis_os"))

    payload = ops.navigation_report(project="jarvis", days=7)

    assert payload["sides"]["lead"]["transcripts"] == 1
    assert payload["sides"]["lead"]["result_bytes"] == 10


def test_both_readers_agree_about_what_a_subagent_file_is(tree):
    """ONE pattern, named once: `_subagents_of` globbed `*.jsonl` and `read_tree`
    `agent-*.jsonl`, so the per-order and the fleet report could disagree about the same
    session. `agent-*.jsonl` is the layout the module docstring documents."""
    tree("s10", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                 tool_result_row("t1", "x" * 10)],
         subagents={"agent-aaa": [tool_use_row("u1", "Grep", pattern="f"),
                                  tool_result_row("u1", "y" * 7)],
                    "helper": [tool_use_row("v1", "Grep", pattern="f"),
                               tool_result_row("v1", "z" * 500)]})

    session = nav_volume.read_session("s10", CFG)
    fleet = nav_volume.read_tree(cfg=CFG, days=7)

    for vol in (session, fleet):
        sub = vol.sides[nav_volume.SIDE_SUBAGENT]
        assert (sub.transcripts, sub.result_bytes) == (1, 7)
    assert nav_volume.SUBAGENT_GLOB == "agent-*.jsonl"


def test_a_missing_root_is_an_honest_absence(tmp_path, monkeypatch):
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(tmp_path / "gone"))

    vol = nav_volume.read_tree(cfg=CFG)

    assert vol.found is False
    assert vol.as_dict()["found"] is False


# -- the payload: the renderer derives nothing ----------------------------------------

CALL_KEYS = ("symbol_calls", "text_search_calls", "nav_bash_calls",
             "code_nav_bash_calls", "other_bash_calls", "read_tool_calls",
             "whole_file_read_calls", "doc_dump_bash_calls")

BYTE_KEYS = ("result_bytes", "nav_bash_bytes", "code_nav_bash_bytes", "symbol_bytes",
             "unattributed_bytes", "read_tool_bytes", "doc_read_bytes",
             "doc_dump_bash_bytes", "bytes_by_tool")


def test_as_dict_carries_every_number_the_renderer_prints(tree):
    tree("s6", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                tool_result_row("t1", "x" * 10)])

    payload = nav_volume.read_session("s6", CFG).as_dict()

    assert payload["scope"] == "s6"
    assert payload["before"] == nav_volume.BEFORE_NOTE
    assert payload["calls_reported"] is False
    lead = payload["sides"]["lead"]
    for key in ("side", "transcripts", *BYTE_KEYS, "code_nav_share"):
        assert key in lead, key
    for key in CALL_KEYS:
        assert key not in lead, key
    assert lead["code_nav_share"] == pytest.approx(1.0)


def test_the_per_order_payload_omits_the_six_call_keys_and_keeps_every_byte_key(tree):
    """Neo q1242: per-order call counts are the sealed-span ones in
    `inspection.nav_profile`, so this reader reports byte volumes and the side split."""
    tree("s9", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                tool_result_row("t1", "x" * 10)],
         subagents={"agent-1": [tool_use_row("t2", "Grep", pattern="x"),
                                tool_result_row("t2", "y" * 4)]})

    payload = nav_volume.read_session("s9", CFG).as_dict()

    assert payload["calls_reported"] is False
    for side in ("lead", "subagent"):
        row = payload["sides"][side]
        assert set(row) == {"side", "transcripts", *BYTE_KEYS, "code_nav_share"}


def test_the_fleet_payload_still_carries_all_six_call_counts(tree):
    """Neo q1242: the FLEET surface is unchanged — only the per-order reader projects
    the call counts away."""
    tree("s10", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                 tool_result_row("t1", "x" * 10)])

    payload = nav_volume.read_tree(cfg=CFG).as_dict()

    assert payload["calls_reported"] is True
    for key in CALL_KEYS:
        assert key in payload["sides"]["lead"], key
    assert payload["sides"]["lead"]["code_nav_bash_calls"] == 1


def test_folding_per_order_volumes_keeps_the_rollup_per_order(tree):
    """Neo q1242: a rollup of per-order volumes is still per-order."""
    tree("s11", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                 tool_result_row("t1", "x" * 10)])
    tree("s12", [tool_use_row("t2", "Bash", command="cat src/b.py"),
                 tool_result_row("t2", "x" * 10)])

    rollup = nav_volume.read_session("s11", CFG)
    rollup.fold(nav_volume.read_session("s12", CFG))

    assert rollup.calls_reported is False
    assert rollup.as_dict()["calls_reported"] is False
    assert rollup.sides["lead"].code_nav_bash_calls == 2


def test_nav_volume_imports_usage_catalog_and_the_leaf_and_nothing_else_of_jarvis():
    """A LEAF module: a report over files on disk must not fail because a catalog or a
    database moved. `inspection`'s constraint, for its reason."""
    import ast

    tree_src = ast.parse(open(nav_volume.__file__).read())
    local = set()
    for node in ast.walk(tree_src):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                local.add(node.module)
            else:
                local.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            local.update(a.name.split(".")[0] for a in node.names
                         if a.name.startswith("jarvis"))

    assert local == {"catalog", "usage", "navigation"}


# -- ops and the CLI ------------------------------------------------------------------

def test_navigation_config_falls_back_to_the_defaults_not_to_none():
    """`ops.inspect_config`'s shape: every default here is a pattern list with a
    measured justification, and having none would mean having no report."""
    from jarvis import ops

    cfg = ops.navigation_config("no-such-project")

    assert cfg.bash_commands == NavigationConfig().bash_commands
    assert cfg.window_days == 7


def test_the_fleet_report_requires_an_explicit_scope(tree):
    """`~/.claude/projects` is 2.7G and 11,889 lead transcripts: a no-argument
    `jarvis navigation` must not walk it."""
    from jarvis import ops

    # Against the CONSTANT, not a copy of its words: the CLI and the dashboard quote
    # that string, so a reworded refusal must fail here and nowhere else.
    with pytest.raises(ops.OpsError) as caught:
        ops.navigation_report()
    assert str(caught.value) == ops.NAVIGATION_NEEDS_SCOPE

    with pytest.raises(ops.OpsError) as explicit:
        ops.navigation_report(None, None, fleet=False)
    assert str(explicit.value) == ops.NAVIGATION_NEEDS_SCOPE


def test_the_cli_prints_the_shares_and_the_before_line(tree, capsys):
    from jarvis import cli

    tree("s7", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                tool_result_row("t1", "x" * 10)])
    payload = nav_volume.read_tree(cfg=CFG, days=7).as_dict()

    cli._print_navigation(payload)

    out = capsys.readouterr().out
    assert "lead" in out and "subagent" in out
    assert "restated" in out
    assert "351 transcripts" in out and "439 transcripts" in out
    assert "1,148 symbol calls" in out
    assert "does not" in out and "reproduce" in out


def test_the_before_note_names_its_reproducing_command_and_both_sides_apart():
    """Neo q1246: the §5.3 figure is not reproducible, so the restatement must carry the
    exact command and lead/subagent separately rather than one blended share."""
    note = nav_volume.BEFORE_NOTE

    assert "jarvis navigation --project jarvis_os --days 36500" in note
    assert "lead" in note and "subagent" in note
    assert "38.2% of 72.2 MB" in note and "34.7% of 58.6 MB" in note
    assert "41.3%" in note and "does not" in note


def test_the_cli_prints_no_call_columns_on_a_per_order_payload(tree, capsys):
    """Neo q1242: the renderer branches off `calls_reported` and derives nothing."""
    from jarvis import cli

    tree("s13", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                 tool_result_row("t1", "x" * 10)])
    payload = nav_volume.read_session("s13", CFG).as_dict()

    cli._print_navigation(payload)

    out = capsys.readouterr().out
    assert "symbol" not in out.split("code reads via bash")[0]
    assert "text-search" not in out and "bash-nav" not in out
    assert "1 transcripts" in out
    assert "code reads via bash: 100.0% of" in out


def test_the_cli_fleet_path_runs_end_to_end(tree, capsys):
    from jarvis import cli

    tree("s8", [tool_use_row("t1", "Bash", command="grep -rn x src/a.py"),
                tool_result_row("t1", "x" * 10)])
    parser = cli.build_parser()

    args = parser.parse_args(["navigation", "--fleet", "--days", "7", "--json"])
    assert cli.main(["navigation", "--fleet", "--days", "7", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert args.fleet is True
    assert payload["scope"] == "fleet"
    assert payload["sides"]["lead"]["code_nav_bash_calls"] == 1


def test_both_reports_resolve_a_target_through_one_helper(monkeypatch):
    """`jarvis inspect` and `jarvis navigation` must never disagree about what an id
    means, so the feature-order-first lookup is written once and the ONLY difference is
    explicit: whether a target that is no id at all may be read as a project name."""
    from jarvis import ops

    tried: list[str] = []

    def no_feature(target, project=None):
        tried.append("feature_order")
        raise ops.OpsError("no such feature order")

    def no_work_order(target, project=None):
        tried.append("work_order")
        raise ops.OpsError("no such work order")

    monkeypatch.setattr(ops, "find_feature_order", no_feature)
    monkeypatch.setattr(ops, "find_work_order", no_work_order)

    # `navigation`'s third resolution, and it is the only thing the flag changes.
    assert ops._resolve_report_target("proj_a", None, project_fallback=True) == (
        ops._TARGET_PROJECT, None, None, None)
    assert tried == ["feature_order", "work_order"]

    # `inspect`'s: a target that resolves to nothing is the work-order lookup's error,
    # unchanged, because there is no third thing a time report could mean.
    with pytest.raises(ops.OpsError, match="no such work order"):
        ops._resolve_report_target("proj_a", None)


def test_a_mistyped_order_id_is_not_read_as_a_project_name(monkeypatch):
    """`jarvis navigation wo-deadbeef` must say the work order does not exist, not that
    there is no project of that name: `project_fallback` turned EVERY failure into the
    project reading (PR 927 review)."""
    from jarvis import ops

    def no_feature(target, project=None):
        raise ops.OpsError("no such feature order")

    def no_work_order(target, project=None):
        raise ops.OpsError(f"no work order {target}")

    monkeypatch.setattr(ops, "find_feature_order", no_feature)
    monkeypatch.setattr(ops, "find_work_order", no_work_order)

    for mistyped in ("wo-deadbeef", "fo-deadbeef"):
        with pytest.raises(ops.OpsError, match=f"no work order {mistyped}"):
            ops._resolve_report_target(mistyped, None, project_fallback=True)

    # A target that is no id at all still has the third reading this flag exists for.
    assert ops._resolve_report_target("proj_a", None, project_fallback=True) == (
        ops._TARGET_PROJECT, None, None, None)


def test_a_non_positive_days_window_is_refused(tree):
    """`--days 0` disabled the window the `--fleet` opt-in exists to enforce: the walk it
    bounds is 2.7G. Against the CONSTANT, as the scope refusal is."""
    from jarvis import ops

    for days in (0, -1):
        with pytest.raises(ops.OpsError) as caught:
            ops.navigation_report(fleet=True, days=days)
        assert str(caught.value) == ops.NAVIGATION_DAYS_POSITIVE
    assert "--days" in ops.NAVIGATION_DAYS_POSITIVE

    # None still means the catalog's window, unchanged.
    assert ops.navigation_report(fleet=True)["window_days"] == 7


def test_inspect_reads_the_transcripts_again_only_when_asked(monkeypatch):
    """`jarvis inspect` opened every transcript twice — once in `inspection.read_session`
    and once more through `nav_volume.read_session` (PR 927 review). The second pass is
    now a caller's request, and `cli.cmd_inspect` — which PRINTS the section — is the
    caller that makes it; the dashboard's debugging page renders nothing from it.
    """
    from jarvis import nav_volume as nav_mod
    from jarvis import ops

    asked: list[str] = []

    def counted(session_id, cfg, *, index=None):
        asked.append(session_id)
        return nav_mod.NavigationVolume(scope=session_id)

    monkeypatch.setattr(nav_mod, "read_session", counted)

    def resolved(target, project=None):
        return "proj_a", "/nowhere", {"id": "wo-1", "title": "t", "status": "completed",
                                      "session_id": "sid-1"}

    monkeypatch.setattr(ops, "find_feature_order",
                        lambda *a, **k: (_ for _ in ()).throw(ops.OpsError("no")))
    monkeypatch.setattr(ops, "find_work_order", resolved)
    monkeypatch.setattr(ops, "ProjectStore", lambda path: _NoStore())

    quiet = ops.inspect_report("wo-1")
    assert asked == []
    assert "navigation" not in quiet["units"][0]

    loud = ops.inspect_report("wo-1", with_navigation=True)
    assert asked == ["sid-1"]
    assert loud["units"][0]["navigation"]["scope"] == "sid-1"


class _NoStore:
    """A project store that answers the two reads `inspect_report`'s unit makes."""

    def turn_starts(self, wo_id):
        return []

    def list_events(self, wo_id, limit=None):
        return []

    def list_turns(self, wo_id):
        return []

    def close(self):
        pass


def test_cmd_navigation_renders_both_sides_with_the_payloads_own_counts(tree, capsys):
    """END TO END through `cmd_navigation`'s renderer, which `--json` never reaches.

    The counts are asserted against the PAYLOAD as well as against literals, so a
    renderer that derived its own number — summing both sides, or re-reading the tree —
    fails here rather than disagreeing with the dashboard (PR 65). The FLEET surface,
    because only that payload carries call counts (`calls_reported`, Neo q1242).
    """
    from jarvis import cli, ops

    tree("s9", [tool_use_row("t1", "mcp__serena__find_symbol", name_path_pattern="x"),
                tool_result_row("t1", "L" * 40),
                tool_use_row("t2", "mcp__serena__find_symbol", name_path_pattern="y"),
                tool_result_row("t2", "L" * 60)],
         subagents={"agent-aaa": [tool_use_row("u1", "Grep", pattern="x"),
                                  tool_result_row("u1", "A" * 20)]})

    assert cli.main(["navigation", "--fleet", "--days", "7"]) == 0

    out = capsys.readouterr().out
    payload = ops.navigation_report(fleet=True, days=7)
    lead, sub = payload["sides"]["lead"], payload["sides"]["subagent"]
    assert (lead["symbol_calls"], sub["text_search_calls"]) == (2, 1)
    assert "lead" in out and "subagent" in out
    assert "fleet" in out                       # the scope line `cmd_navigation` prints
    assert (f"{lead['transcripts']:>4} transcripts  "
            f"{lead['symbol_calls']:>4} symbol") in out
    assert (f"{sub['symbol_calls']:>4} symbol  "
            f"{sub['text_search_calls']:>4} text-search") in out


# -- the doc counters: §3.2 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md -------

def test_bytes_by_tool_folds_across_a_lead_and_two_subagent_transcripts(tree):
    """THE FOLD, and it is why the field is a `Counter`: `_merge` folds every field with
    `+` over `vars()`, and a plain `dict[str, int]` raises `TypeError` there. A
    single-transcript test passes vacuously."""
    tree("d1", [
        tool_use_row("t1", "Read", file_path="/tmp/a.py"),
        tool_result_row("t1", "x" * 100),
        tool_use_row("t2", "Bash", command="cat src/a.py"),
        tool_result_row("t2", "x" * 10),
    ], subagents={
        "agent-aaa": [tool_use_row("u1", "Read", file_path="/tmp/b.py"),
                      tool_result_row("u1", "y" * 5),
                      tool_use_row("u2", "Bash", command="cat src/b.py"),
                      tool_result_row("u2", "y" * 7)],
        "agent-bbb": [tool_use_row("v1", "Read", file_path="/tmp/c.py"),
                      tool_result_row("v1", "z" * 3),
                      tool_use_row("v2", "Bash", command="cat src/c.py"),
                      tool_result_row("v2", "z" * 11)],
    })

    vol = nav_volume.read_session("d1", CFG)

    assert vol.sides["lead"].bytes_by_tool == {"Read": 100, "Bash": 10}
    assert vol.sides["subagent"].bytes_by_tool == {"Read": 8, "Bash": 18}
    payload = vol.as_dict()
    assert payload["sides"]["subagent"]["bytes_by_tool"] == {"Bash": 18, "Read": 8}
    assert list(payload["sides"]["subagent"]["bytes_by_tool"]) == ["Bash", "Read"]


def test_whole_file_read_calls_count_a_read_with_no_limit_only(tree):
    """§1(a): `limit is None` IS the predicate, answerable from `tool_input` alone."""
    path = tree("d2", [
        tool_use_row("t1", "Read", file_path="/tmp/a.py", limit=200),
        tool_result_row("t1", "x" * 10),
        tool_use_row("t2", "Read", file_path="/tmp/b.py"),
        tool_result_row("t2", "x" * 20),
    ])

    vol = nav_volume.read_transcript(path, nav_volume.SIDE_LEAD, CFG)

    assert vol.whole_file_read_calls == 1
    assert vol.read_tool_calls == 2
    assert vol.read_tool_bytes == 30


def test_doc_read_bytes_are_the_markdown_half_of_read_tool_bytes(tree):
    path = tree("d3", [
        tool_use_row("t1", "Read", file_path="/tmp/docs/spec.md"),
        tool_result_row("t1", "m" * 40),
        tool_use_row("t2", "Read", file_path="/tmp/src/x.py"),
        tool_result_row("t2", "p" * 15),
    ])

    vol = nav_volume.read_transcript(path, nav_volume.SIDE_LEAD, CFG)

    assert vol.doc_read_bytes == 40
    assert vol.read_tool_bytes == 55
    assert vol.whole_file_read_calls == 2


def test_doc_dump_bash_calls_go_through_the_shipped_classifier(tree):
    """`grep` over a `.md` is text search and stays legal — this feature's MUST NOT
    (§1.1). A `pytest` run is no dump either."""
    path = tree("d4", [
        tool_use_row("t1", "Bash", command="cat docs/a.md"),
        tool_result_row("t1", "x" * 70),
        tool_use_row("t2", "Bash", command="grep -rn foo docs/a.md"),
        tool_result_row("t2", "x" * 9),
        tool_use_row("t3", "Bash", command="uv run pytest"),
        tool_result_row("t3", "ok"),
    ])

    vol = nav_volume.read_transcript(path, nav_volume.SIDE_LEAD, CFG)

    assert vol.doc_dump_bash_calls == 1
    assert vol.doc_dump_bash_bytes == 70


def test_the_doc_before_note_is_a_second_note_and_both_reach_the_payload(tree):
    """§2.2: `BEFORE_NOTE` is not edited — §1's figures travel as a second note."""
    tree("d5", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                tool_result_row("t1", "x" * 10)])

    payload = nav_volume.read_tree(cfg=CFG, days=7).as_dict()

    assert nav_volume.DOC_BEFORE_NOTE is not nav_volume.BEFORE_NOTE
    assert payload["before"] == nav_volume.BEFORE_NOTE
    assert payload["doc_before"] == nav_volume.DOC_BEFORE_NOTE


def test_the_doc_before_note_names_its_corpus_its_date_and_its_figures():
    """A RECORDED BASELINE, not an assertion: the acceptance criterion is that the
    command reproduces a number (§3.2)."""
    note = nav_volume.DOC_BEFORE_NOTE

    assert "877 transcripts" in note
    assert "~/.claude/projects/*agentic*/" in note
    assert "2026-10-06" in note
    assert "chars // 4" in note
    assert "1,032,302" in note and "199 calls" in note
    assert "1,252,687" in note and "2,189 calls" in note
    assert "487,048" in note and "133 calls" in note
    assert "jarvis navigation" in note
    assert "recorded baseline" in note.lower()


def test_the_catalog_doc_defaults_are_the_leafs_sets_and_not_a_second_definition():
    """Identity, not equality: a copied body passes equality and then drifts (§2.5)."""
    from jarvis import catalog, navigation

    assert catalog.DEFAULT_NAVIGATION_DOC_SUFFIXES is navigation.DOC_SUFFIXES
    assert catalog.DEFAULT_NAVIGATION_DOC_DUMP_COMMANDS is navigation.DOC_DUMP_COMMANDS
    assert NavigationConfig().doc_suffixes is navigation.DOC_SUFFIXES
    assert NavigationConfig().doc_dump_commands is navigation.DOC_DUMP_COMMANDS
    assert "doc_suffixes" in catalog.NAVIGATION_PATTERN_KEYS
    assert "doc_dump_commands" in catalog.NAVIGATION_PATTERN_KEYS


def test_an_empty_doc_pattern_list_and_a_dotless_doc_suffix_are_refused():
    from jarvis.catalog import CatalogError, parse_catalog

    with pytest.raises(CatalogError, match="navigation.doc_suffixes"):
        parse_catalog({"os": {"navigation": {"doc_suffixes": []}}, "projects": []})
    with pytest.raises(CatalogError, match="navigation.doc_dump_commands"):
        parse_catalog({"os": {"navigation": {"doc_dump_commands": []}},
                       "projects": []})
    with pytest.raises(CatalogError, match="doc_suffixes"):
        parse_catalog({"os": {"navigation": {"doc_suffixes": ["md"]}}, "projects": []})


def test_the_cli_prints_the_doc_figures_and_the_doc_before_line(tree, capsys):
    from jarvis import cli

    tree("d6", [tool_use_row("t1", "Read", file_path="/tmp/docs/spec.md"),
                tool_result_row("t1", "m" * 40),
                tool_use_row("t2", "Bash", command="cat docs/a.md"),
                tool_result_row("t2", "d" * 70)])
    payload = nav_volume.read_tree(cfg=CFG, days=7).as_dict()

    cli._print_navigation(payload)

    out = capsys.readouterr().out
    assert "whole-file Read" in out
    assert "doc dumps" in out
    assert "docs:" in out
    assert "877 transcripts" in out


def test_the_cli_prints_no_doc_call_columns_on_a_per_order_payload(tree, capsys):
    """§3.2's second trap: new CALL counters are gated on `calls_reported`, new BYTE
    counters are not."""
    from jarvis import cli

    tree("d7", [tool_use_row("t1", "Read", file_path="/tmp/docs/spec.md"),
                tool_result_row("t1", "m" * 40)])
    payload = nav_volume.read_session("d7", CFG).as_dict()

    cli._print_navigation(payload)

    out = capsys.readouterr().out
    assert "whole-file Read" not in out
    assert "doc dumps" not in out
    assert "docs:" in out
