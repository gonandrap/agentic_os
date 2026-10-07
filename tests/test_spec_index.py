"""§4.4 pins for `spec_index`: delegation, one matcher, two documents, and the verb.

docs/specs/2026-10-06-navigate-specs-like-code.md §4.4. The traversal pin is a SYMLINK
and never a `../` string: a prefix test passes the `../` case and is defeated by the link,
so the `../` version of this test pins nothing.
"""

from __future__ import annotations

import inspect
import re
import shlex
from pathlib import Path

import pytest

from jarvis import cli, hooks, ops, sections, spec_index
from jarvis.central_store import CentralStore

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SPEC = REPO_ROOT / "docs" / "specs" / "2026-09-24-order-observability.md"

FIXTURE = """# Exporter

Intro prose.

## 1. Data model

Rows are dicts, the union of keys.

## 2. Failure handling

An empty result set is not an error.

### 2.1 Retries

Three, then give up.

## Agent profile

Writes exporters.
"""


@pytest.mark.parametrize("which", ["2", "9", "failure HANDLING", "no such heading"])
def test_section_delegates_to_sections_extract_section(which):
    assert spec_index.section(FIXTURE, which) == sections.extract_section(FIXTURE, which)


def test_section_number_without_a_numbered_heading_is_none():
    assert spec_index.section(FIXTURE, "9") is None


def test_section_name_matches_case_insensitively_as_a_substring():
    got = spec_index.section(FIXTURE, "failure HANDLING")
    assert got is not None and "An empty result set" in got


def test_section_includes_nested_heading_and_stops_at_the_next_peer():
    got = spec_index.section(FIXTURE, "2")
    assert got is not None
    assert "### 2.1 Retries" in got and "Three, then give up." in got
    assert "Agent profile" not in got and "Rows are dicts" not in got


def test_spec_index_defines_no_heading_regex():
    """§4.1: a second heading matcher in this tree is the bug."""
    src = Path(inspect.getsourcefile(spec_index)).read_text()
    assert not re.search(r"re\.compile\([^)]*#", src)
    assert "#{1,6}" not in src


def test_toc_derives_from_sections_heading_re_by_identity(monkeypatch):
    """Identity, so a retyped copy that merely passes equality fails here."""
    seen: list[str] = []
    real = sections.HEADING_RE

    class Spy:
        def finditer(self, text):
            seen.append(text)
            return real.finditer(text)

    monkeypatch.setattr(sections, "HEADING_RE", Spy())
    spec_index.toc(FIXTURE)
    assert seen == [FIXTURE]


def test_toc_over_the_fixture():
    got = spec_index.toc(FIXTURE)
    assert [(s.number, s.level) for s in got] == [
        ("", 1), ("1", 2), ("2", 2), ("2.1", 3), ("", 2),
    ]
    lines = FIXTURE.splitlines()
    for s in got:
        assert lines[s.line - 1].startswith("#") and s.name in lines[s.line - 1]
    data_model = got[1]
    assert data_model.tokens == len(sections.extract_section(FIXTURE, "1")) // 4
    assert data_model.tokens > 0


@pytest.mark.parametrize("source", ["fixture", "real"])
def test_every_toc_ref_resolves_back_to_its_own_heading(source):
    """§4.1: the ref is one `jarvis spec section` accepts, verbatim.

    A `### 2.1` row carrying number `2` lands on `## 2.`, a different section — the
    bug §4.1 names, one row down.
    """
    md = FIXTURE if source == "fixture" else REAL_SPEC.read_text()
    heading_lines = md.splitlines()
    for s in spec_index.toc(md):
        ref = s.number or s.name
        got = spec_index.section(md, ref)
        assert got is not None, ref
        assert got.splitlines()[0] == heading_lines[s.line - 1], ref


def test_toc_over_the_real_committed_spec():
    """§4.4: one tidy fixture is where this suite goes vacuous."""
    md = REAL_SPEC.read_text()
    got = spec_index.toc(md)
    lines = md.splitlines()
    top = [s for s in got if s.level == 2]
    assert [s.number for s in top] == [str(n) for n in range(1, 12)] + [""]
    assert top[-1].name == "Agent profile"
    assert lines[top[-1].line - 1] == "## Agent profile"
    assert lines[top[0].line - 1].startswith("## 1. The evidence")
    for s in top:
        assert s.tokens == len(sections.extract_section(md, s.number or s.name)) // 4


# -- §4.1 `search`: section granularity, and the walk ------------------------------------

SEARCHABLE = """# Exporter

## 1. Data model

The retry budget is three.

## 2. Failure handling

The retry budget is read here too.
And the retry budget is named a third time.

## 3. Shipping

Nothing about budgets.
"""


def test_search_reports_one_hit_per_section_with_a_resolvable_ref(tmp_path):
    """§4.1: section granularity, deduplicated — not a grep line list."""
    (tmp_path / "doc.md").write_text(SEARCHABLE)
    hits = spec_index.search(tmp_path, "retry budget")
    assert [(h.path, h.section) for h in hits] == [("doc.md", "1"), ("doc.md", "2")]
    for h in hits:
        got = spec_index.section(SEARCHABLE, h.section)
        assert got is not None and "retry budget" in got
    second = hits[1]
    assert second.line == SEARCHABLE.splitlines().index(
        "The retry budget is read here too.") + 1
    assert second.context == "The retry budget is read here too."


def test_search_requires_every_term_case_insensitively(tmp_path):
    (tmp_path / "doc.md").write_text(SEARCHABLE)
    assert spec_index.search(tmp_path, "RETRY BUDGET")
    assert spec_index.search(tmp_path, "retry shipping") == []


def test_search_prunes_vendor_dirs_and_keeps_dot_jarvis(tmp_path):
    """§4.2's sub-second scan is false on the first project with a virtualenv."""
    for where in (".venv/lib", "node_modules/pkg", ".git", "__pycache__"):
        d = tmp_path / where
        d.mkdir(parents=True)
        (d / "vendored.md").write_text("the retry budget is three\n")
    materialised = tmp_path / ".jarvis" / "features" / "fo-1"
    materialised.mkdir(parents=True)
    (materialised / "spec.md").write_text("## 1. Slice\n\nthe retry budget is three\n")
    assert [h.path for h in spec_index.search(tmp_path, "retry budget")] \
        == [".jarvis/features/fo-1/spec.md"]


def test_search_prunes_sibling_git_worktrees(tmp_path):
    """A worktree is a copy of the tree, so an unpruned walk ranks another branch first."""
    sibling = tmp_path / ".claude" / "worktrees" / "other" / "docs"
    sibling.mkdir(parents=True)
    (sibling / "doc.md").write_text("## 1. Slice\n\nthe retry budget is nine\n")
    (tmp_path / "doc.md").write_text("## 1. Slice\n\nthe retry budget is three\n")
    hits = spec_index.search(tmp_path, "retry budget")
    assert [h.path for h in hits] == ["doc.md"]
    assert hits[0].context.endswith("three")


def test_search_limit_caps_the_hits(tmp_path):
    for n in range(6):
        (tmp_path / f"d{n}.md").write_text(f"## {n}. Part\n\nthe retry budget\n")
    assert len(spec_index.search(tmp_path, "retry budget", limit=2)) == 2


def test_search_context_is_one_capped_line(tmp_path):
    (tmp_path / "doc.md").write_text(
        "## 1. Long\n\n   the retry budget " + "x" * 500 + "\n\nnext paragraph\n")
    hit = spec_index.search(tmp_path, "retry budget")[0]
    assert "\n" not in hit.context
    assert len(hit.context) == spec_index.CONTEXT_CHARS
    assert hit.context.startswith("the retry budget")


# -- §4.2 root resolution, containment, payloads -----------------------------------------


@pytest.fixture()
def registered(project, monkeypatch):
    """`proj_a` in the central store, with the cwd inside it — the default scope."""
    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(project))
    finally:
        central.close()
    (project / ".jarvis").mkdir(exist_ok=True)
    monkeypatch.chdir(project)
    return project


def test_spec_toc_and_search_payloads_carry_the_command_and_the_estimate(registered):
    toc = ops.spec_toc("docs/specs/exporter.md")
    assert toc["project"] == "proj_a"
    row = toc["sections"][1]
    assert row["command"] == f"jarvis spec section {toc['path']} {row['ref']}"
    assert row["tokens_estimate"] >= 0
    parser = cli.build_parser()
    parser.parse_args(row["command"].split()[1:])

    found = ops.spec_search("separable piece")
    assert found["hits"]
    for hit in found["hits"]:
        assert hit["command"].startswith("jarvis spec ")
        parser.parse_args(hit["command"].split()[1:])


def test_spec_section_names_the_headings_that_exist(registered):
    with pytest.raises(ops.OpsError) as e:
        ops.spec_section("docs/specs/exporter.md", "no such heading")
    assert "Data model" in str(e.value)


def test_spec_search_unknown_project_names_it_and_scans_nothing(registered, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("scanned despite an unknown project")

    monkeypatch.setattr(spec_index, "search", refuse)
    with pytest.raises(ops.OpsError) as e:
        ops.spec_search("separable piece", project="no_such_project")
    assert "no_such_project" in str(e.value)


def test_comma_separated_project_is_rejected(registered, capsys):
    """§4.2: one project, never a union — so `a,b` is simply an unknown name."""
    assert cli.main(["spec", "search", "separable", "--project", "proj_a,proj_a"]) == 1
    assert "proj_a,proj_a" in capsys.readouterr().err


def test_the_argparse_surface_has_no_all_flag():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["spec", "search", "words", "--all"])


def test_no_project_and_a_cwd_outside_every_project_names_the_fix(registered, tmp_path,
                                                                 monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ops.OpsError) as e:
        ops.spec_toc("README.md")
    assert "--project" in str(e.value)


def _execute(command: str) -> dict:
    """Run an emitted `jarvis spec section …` string the way a reader would type it.

    Parsing it and calling `ops` is the whole test: a command that only PARSES can still
    open a different file, which is what a root-relative path does from any cwd but the
    root. The parser runs too, since `ops` alone would accept a `--project` placed where
    argparse rejects it.
    """
    tokens = shlex.split(command)
    assert tokens[:3] == ["jarvis", "spec", "section"], command
    cli.build_parser().parse_args(tokens[1:])
    rest = tokens[3:]
    project = None
    if rest and rest[0] == "--project":
        project, rest = rest[1], rest[2:]
    path, ref = rest
    return ops.spec_section(path, ref, project=project)


def test_a_relative_path_is_read_from_the_cwd_and_reported_root_relative(registered,
                                                                         monkeypatch):
    """A worker's worktree sits under the root, so a root-relative base reads MAIN's copy.

    Same filename at the root and in the subdirectory: the one next to the caller wins.
    """
    (registered / "doc.md").write_text("# The copy at the root\n\nnot this one.\n")
    sub = registered / "sub"
    sub.mkdir()
    (sub / "doc.md").write_text("# The copy beside the caller\n\nthis one.\n")
    monkeypatch.chdir(sub)
    out = ops.spec_toc("doc.md")
    assert [s["name"] for s in out["sections"]] == ["The copy beside the caller"]
    assert out["path"] == "sub/doc.md"
    row = out["sections"][0]
    assert _execute(row["command"])["content"].startswith("# The copy beside the caller")


def test_an_emitted_toc_command_opens_that_row_from_the_callers_cwd(registered,
                                                                    monkeypatch):
    """§4.2's "exact command that shows it": it must run from the cwd that printed it."""
    sub = registered / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    out = ops.spec_toc("../docs/specs/exporter.md")
    assert out["path"] == "docs/specs/exporter.md"
    for row in out["sections"][1:4]:
        first = _execute(row["command"])["content"].splitlines()[0]
        assert first.endswith(row["name"]), (row["command"], first)


def test_an_emitted_search_command_opens_a_hit_in_another_directory(registered,
                                                                   monkeypatch):
    """The hit's DISPLAY path is root-relative; the command's must be callable.

    The hit lives in `docs/specs/`, the caller in `sub/`, so a root-relative string in
    the command resolves to nothing at all.
    """
    sub = registered / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    found = ops.spec_search("separable piece")
    assert found["hits"]
    hit = found["hits"][0]
    assert hit["path"].startswith("docs/specs/")
    content = _execute(hit["command"])["content"]
    assert content.startswith("#") and hit["context"] in content, hit["command"]


def test_an_emitted_command_names_the_project_the_caller_named(registered, tmp_path,
                                                               monkeypatch):
    """`--project` plus an absolute path: from outside every root, nothing else resolves."""
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    # Absolute: from outside every root a relative path reads nothing, as `_spec_file` says.
    toc = ops.spec_toc(str(registered / "docs" / "specs" / "exporter.md"),
                       project="proj_a")
    row = toc["sections"][1]
    assert "--project proj_a" in row["command"]
    assert _execute(row["command"])["content"].splitlines()[0].endswith(row["name"])

    hit = ops.spec_search("separable piece", project="proj_a")["hits"][0]
    assert "--project proj_a" in hit["command"]
    content = _execute(hit["command"])["content"]
    assert content.startswith("#") and hit["context"] in content, hit["command"]


def test_a_symlink_out_of_the_tree_is_refused_naming_the_root(registered, tmp_path):
    """§4.2: both sides resolved — a prefix test is passed by this link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# Secret\n\nnot in the project\n")
    (registered / "escape.md").symlink_to(outside / "secret.md")
    with pytest.raises(ops.OpsError) as e:
        ops.spec_toc("escape.md")
    assert str(registered) in str(e.value)
    assert cli.main(["spec", "toc", "escape.md"]) == 1


# -- §4.3 the three command spellings ----------------------------------------------------


def test_the_three_spellings_parse_as_the_contract_names_them():
    parser = cli.build_parser()
    parser.parse_args(["spec", "toc", "docs/x.md"])
    parser.parse_args(["spec", "section", "docs/x.md", "4.2"])
    parser.parse_args(["spec", "search", "two words"])


def test_both_spec_helps_exit_zero():
    """A new top-level verb can reorder argparse subcommands."""
    parser = cli.build_parser()
    for argv in (["spec", "--help"], ["fo", "spec", "--help"]):
        with pytest.raises(SystemExit) as e:
            parser.parse_args(argv)
        assert e.value.code == 0


def test_a_piped_query_is_not_auto_allowed_and_the_help_says_so(capsys):
    assert hooks.is_jarvis_command_chain('jarvis spec search "a|b"') is False
    assert hooks.is_jarvis_command_chain('jarvis spec search "a b"') is True
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["spec", "search", "--help"])
    help_text = capsys.readouterr().out
    assert "is_jarvis_command_chain" in help_text
    for char in "|;`$<>":
        assert char in help_text
