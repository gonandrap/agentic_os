"""One copy of the project's CLAUDE.md reaches a worker, not two.

docs/superpowers/specs/2026-09-29-one-copy-of-the-projects-claude-md.md (issue #850). A
worker's cwd is `<root>/.claude/worktrees/<id>`, inside the project, so Claude Code
resolves the project's CLAUDE.md twice — the branch's copy and the main checkout's, which
can be a different revision. `claudeMdExcludes` suppresses the ancestor copy; the
measurement and the fingerprint read the same one definition so they cannot disagree.

What these tests do NOT prove: that Claude Code honours the key. That was measured live
for this order (spec, "Verified") and is not a claim about Jarvis's code.
"""

from __future__ import annotations

import json

import pytest

from jarvis import context, hooks
from jarvis.catalog import ProjectSpec


@pytest.fixture()
def nest(project):
    """The project's own CLAUDE.md, a worker's worktree under it, and the worktree's."""
    (project / "CLAUDE.md").write_text("the main checkout's rules")
    worktree = project / ".claude" / "worktrees" / "wo-x"
    worktree.mkdir(parents=True)
    (worktree / "CLAUDE.md").write_text("the branch's rules")
    return worktree


# -- 1. the exclusion list -------------------------------------------------------------

def test_the_ancestor_copy_is_excluded_and_the_branchs_is_not(project, nest):
    excludes = hooks.claude_md_excludes(project, nest)

    assert excludes == [project / "CLAUDE.md"]


def test_a_cwd_at_the_root_or_outside_it_excludes_nothing(project, nest, tmp_path):
    """An empty list is the answer, not an error: a turn at the project root has no
    ancestor copy to drop."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (tmp_path / "CLAUDE.md").write_text("above the checkout")

    assert hooks.claude_md_excludes(project, project) == []
    assert hooks.claude_md_excludes(project, outside) == []


def test_a_symlinked_spelling_of_the_root_still_matches(project, nest):
    """The case the comment at src/jarvis/hooks.py:1543 exists for: a catalog path
    through a symlink. Unresolved, the ancestor test never matches and the duplicate
    survives."""
    by_link = project.parent / "as-linked"
    by_link.symlink_to(project)

    assert hooks.claude_md_excludes(by_link, nest) == [project / "CLAUDE.md"]
    assert hooks.claude_md_excludes(project, by_link / ".claude" / "worktrees" / "wo-x") \
        == [project / "CLAUDE.md"]


def test_the_walk_up_stops_at_the_root(project, nest):
    (project.parent / "CLAUDE.md").write_text("belongs to something else")

    assert project.parent / "CLAUDE.md" not in hooks.claude_md_excludes(project, nest)


# -- 2. the settings file carries the key ----------------------------------------------

def test_the_settings_file_excludes_the_ancestor_copy_with_no_worktree_on_disk(
        project, jarvis_home):
    """Written before the spawn that creates the worktree, so the path is PREDICTED —
    an `is_dir` check here would write the key on no launch at all."""
    (project / "CLAUDE.md").write_text("the main checkout's rules")
    spec = ProjectSpec(name="proj_a", path=project)

    out = json.loads(_write_settings(spec, {"id": "wo-x", "title": "t"}).read_text())

    assert out["claudeMdExcludes"] == [str(project / "CLAUDE.md")]
    assert not (project / ".claude" / "worktrees" / "wo-x").exists()


def test_a_catalogs_own_exclusion_survives_the_merge_deduped(project, jarvis_home):
    (project / "CLAUDE.md").write_text("the main checkout's rules")
    spec = ProjectSpec(name="proj_a", path=project, settings_overrides={
        "claudeMdExcludes": ["/opt/shared/CLAUDE.md"]})

    out = json.loads(_write_settings(spec, {"id": "wo-x", "title": "t"}).read_text())

    assert out["claudeMdExcludes"] == ["/opt/shared/CLAUDE.md",
                                       str(project / "CLAUDE.md")]


def test_a_catalog_path_through_a_symlink_excludes_both_spellings(project, jarvis_home):
    """The CLI loads the file under the spelling it resolved the cwd to, and which that
    is depends on the checkout. An entry naming a file it did not load is inert; a
    missing one leaves the duplicate."""
    (project / "CLAUDE.md").write_text("the main checkout's rules")
    by_link = project.parent / "as-linked"
    by_link.symlink_to(project)
    spec = ProjectSpec(name="proj_a", path=by_link)

    out = json.loads(_write_settings(spec, {"id": "wo-x", "title": "t"}).read_text())

    assert out["claudeMdExcludes"] == [str(project / "CLAUDE.md"),
                                       str(by_link / "CLAUDE.md")]


def _write_settings(spec, wo):
    from jarvis.dispatch import _write_worker_settings

    return _write_worker_settings(spec, wo)


# -- 3. the fingerprint and the debug view agree ---------------------------------------

def test_the_fingerprint_and_the_debug_view_list_the_same_files(project, nest):
    """One definition feeds both readers: `hooks._memory_digest` walks `memory_files`
    and `context._memory_row` calls it."""
    walked = hooks.memory_files(project, nest)
    row = context._memory_row(ProjectSpec(name="proj_a", path=project),
                              {"id": "wo-x", "worktree": "wo-x"},
                              {"kind": "dispatch", "seq": 1})

    assert row["detail"]["paths"] == [str(p) for p in walked]
    assert str(project / "CLAUDE.md") not in row["detail"]["paths"]
    assert str(nest / "CLAUDE.md") in row["detail"]["paths"]
    assert row["detail"]["excluded"] == [str(project / "CLAUDE.md")]


def test_the_digest_moves_when_the_branchs_copy_does_and_not_when_the_ancestor_does(
        project, nest):
    """The sharper half of the same claim: the excluded file is out of the hashed
    prefix, so editing it is not drift."""
    before = hooks._memory_digest(project, nest)
    (project / "CLAUDE.md").write_text("the main checkout, revised")
    assert hooks._memory_digest(project, nest) == before

    (nest / "CLAUDE.md").write_text("the branch, revised")
    assert hooks._memory_digest(project, nest) != before


# -- 4. the cwd the turn really runs in ------------------------------------------------

def test_the_dispatch_turn_is_measured_inside_the_worktree_that_does_not_exist_yet(
        project):
    """`worker_session.start` launches with `cwd=project.path` and lets `--worktree`
    create the tree, so at seq 1 there is nothing to `is_dir`."""
    spec = ProjectSpec(name="proj_a", path=project)
    wo = {"id": "wo-x", "worktree": "wo-x"}
    predicted = project / ".claude" / "worktrees" / "wo-x"

    assert context._worktree_cwd(spec, wo, {"kind": "dispatch", "seq": 1}) == predicted
    assert context._worktree_cwd(spec, wo, {"kind": "message", "seq": 2}) == project
