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


def test_a_missing_root_is_an_honest_absence(tmp_path, monkeypatch):
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(tmp_path / "gone"))

    vol = nav_volume.read_tree(cfg=CFG)

    assert vol.found is False
    assert vol.as_dict()["found"] is False


# -- the payload: the renderer derives nothing ----------------------------------------

def test_as_dict_carries_every_number_the_renderer_prints(tree):
    tree("s6", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                tool_result_row("t1", "x" * 10)])

    payload = nav_volume.read_session("s6", CFG).as_dict()

    assert payload["scope"] == "s6"
    assert payload["before"] == nav_volume.BEFORE_NOTE
    lead = payload["sides"]["lead"]
    for key in ("transcripts", "symbol_calls", "text_search_calls", "nav_bash_calls",
                "code_nav_bash_calls", "other_bash_calls", "read_tool_calls",
                "result_bytes", "nav_bash_bytes", "code_nav_bash_bytes",
                "symbol_bytes", "unattributed_bytes", "code_nav_share"):
        assert key in lead, key
    assert lead["code_nav_share"] == pytest.approx(1.0)


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

    with pytest.raises(ops.OpsError, match="--fleet"):
        ops.navigation_report()


def test_the_cli_prints_the_shares_and_the_before_line(tree, capsys):
    from jarvis import cli

    tree("s7", [tool_use_row("t1", "Bash", command="cat src/a.py"),
                tool_result_row("t1", "x" * 10)])
    payload = nav_volume.read_tree(cfg=CFG, days=7).as_dict()

    cli._print_navigation(payload)

    out = capsys.readouterr().out
    assert "lead" in out and "subagent" in out
    assert "41.3%" in out and "0 symbol calls" in out


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
