import json
import re

from jarvis.bootstrap import (
    bootstrap_project,
    build_settings,
    deep_merge,
    settings_drift,
)
from jarvis.catalog import ProjectSpec


def spec(path, **kw):
    return ProjectSpec(name="proj_a", path=path, **kw)


def test_deep_merge():
    base = {"a": 1, "b": {"x": 1, "y": 2}, "c": [1]}
    over = {"b": {"y": 3, "z": 4}, "c": [9], "d": True}
    assert deep_merge(base, over) == {"a": 1, "b": {"x": 1, "y": 3, "z": 4}, "c": [9], "d": True}


def test_bootstrap_creates_everything(project):
    report = bootstrap_project(spec(project))
    assert not report.warnings
    assert (project / "OPERATION.md").exists()
    assert (project / ".jarvis").is_dir()
    assert ".jarvis/" in (project / ".gitignore").read_text()
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    assert settings["_jarvis"]["managed"] is True
    assert "Stop" in settings["hooks"]
    # idempotent
    report2 = bootstrap_project(spec(project))
    assert "settings.json already up to date" in report2.actions


def test_bootstrap_trusts_workspace(tmp_path, monkeypatch):
    from jarvis.bootstrap import workspace_trusted
    from jarvis.testing import make_git_project
    p = make_git_project(tmp_path, "untrusted")
    cfg = tmp_path / "claude.json"
    # a pre-existing entry with other keys we must not clobber
    cfg.write_text(json.dumps({
        "numStartups": 7,
        "projects": {str(p): {"hasTrustDialogAccepted": False, "lastCost": 1.5}},
    }))
    monkeypatch.setenv("JARVIS_CLAUDE_JSON", str(cfg))

    assert workspace_trusted(p) is False
    report = bootstrap_project(spec(p))
    assert not report.warnings
    assert workspace_trusted(p) is True
    data = json.loads(cfg.read_text())
    assert data["numStartups"] == 7                       # top-level key preserved
    assert data["projects"][str(p)]["lastCost"] == 1.5    # sibling key preserved


def test_bootstrap_generates_readme_stub(tmp_path):
    from jarvis.testing import make_git_project
    p = make_git_project(tmp_path, "noreadme", readme=None)
    report = bootstrap_project(spec(p, description="does things"))
    assert (p / "README.md").exists()
    assert "does things" in (p / "README.md").read_text()
    assert any("README" in w for w in report.warnings)


def test_bootstrap_requires_git(tmp_path):
    p = tmp_path / "nogit"
    p.mkdir()
    report = bootstrap_project(spec(p))
    assert any("not a git repository" in w for w in report.warnings)


def test_settings_backup_and_drift(project):
    claude_dir = project / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"model": "opus"}))

    bootstrap_project(spec(project))
    backup = claude_dir / "settings.json.pre-jarvis"
    assert json.loads(backup.read_text()) == {"model": "opus"}
    assert settings_drift(claude_dir / "settings.json") is None

    # manual edit → drift detected, not overwritten without force
    current = json.loads((claude_dir / "settings.json").read_text())
    current["model"] = "haiku"
    (claude_dir / "settings.json").write_text(json.dumps(current))
    assert settings_drift(claude_dir / "settings.json") == "manually edited since injection"

    report = bootstrap_project(spec(project))
    assert any("manually edited" in w for w in report.warnings)
    assert json.loads((claude_dir / "settings.json").read_text())["model"] == "haiku"

    report = bootstrap_project(spec(project), force_config=True)
    assert settings_drift(claude_dir / "settings.json") is None


def test_settings_overrides_merged(project):
    overrides = {"env": {"FOO": "1"}, "permissions": {"allow": ["Bash(npm *)"]}}
    bootstrap_project(spec(project, settings_overrides=overrides))
    settings = json.loads((project / ".claude" / "settings.json").read_text())
    assert settings["env"] == {"JARVIS_MANAGED": "1", "FOO": "1"}
    assert settings["permissions"] == {"allow": ["Bash(npm *)"]}
    assert "hooks" in settings  # base preserved


def test_build_settings_hash_stable():
    s1 = build_settings({})
    s2 = build_settings({})
    assert s1["_jarvis"]["hash"] == s2["_jarvis"]["hash"]


SKILL = ".claude/skills/jarvis-inject-session/SKILL.md"


def test_bootstrap_installs_the_inject_skill(project):
    """The user's own sessions only ever load <project>/.claude/skills/"""
    report = bootstrap_project(spec(project))
    body = (project / SKILL).read_text()
    assert "name: jarvis-inject-session" in body
    assert any("installed .claude/skills/" in a for a in report.actions)
    # second pass: unchanged content is left alone, not rewritten
    report2 = bootstrap_project(spec(project))
    assert ".claude/skills/ already up to date" in report2.actions


def test_inject_skill_uses_the_session_id_claude_exports(project):
    """The id comes from the environment Claude Code sets"""
    bootstrap_project(spec(project))
    body = (project / SKILL).read_text()
    assert 'jarvis wo inject "$CLAUDE_CODE_SESSION_ID"' in body
    assert '-z "$CLAUDE_CODE_SESSION_ID"' in body       # guard: unset
    assert '-n "$JARVIS_WO_ID"' in body                 # guard: already a work order


def test_inject_skill_heals_local_edits_but_spares_the_user_s_own(project):
    bootstrap_project(spec(project))
    skills = project / ".claude" / "skills"
    (skills / "jarvis-inject-session" / "SKILL.md").write_text("mangled\n")
    mine = skills / "my-own-skill"
    mine.mkdir()
    (mine / "SKILL.md").write_text("mine\n")

    report = bootstrap_project(spec(project))
    assert "name: jarvis-inject-session" in (project / SKILL).read_text()
    assert any("installed .claude/skills/" in a for a in report.actions)
    assert (mine / "SKILL.md").read_text() == "mine\n"   # never clears the whole tree


def test_inject_skill_is_not_handed_to_workers(project):
    """A worker session is already a work order"""
    from jarvis.bootstrap import install_agent_assets
    root = install_agent_assets(project)[0]
    names = {p.name for p in (root / ".claude" / "skills").iterdir()}
    assert "report-jarvis-bug" in names
    assert "jarvis-inject-session" not in names


def test_dry_run_writes_no_skill(project):
    report = bootstrap_project(spec(project), dry_run=True)
    assert not (project / ".claude" / "skills").exists()
    assert any(".claude/skills/" in a for a in report.actions)


def test_operation_md_preserves_specifics(project):
    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    from jarvis.bootstrap import TEMPLATE_VERSION
    current = f"template v{TEMPLATE_VERSION}"
    text = op.read_text().replace(current, "template v0")  # simulate old version
    text = text.replace("_None yet._", "Run `make test` before shipping.")
    op.write_text(text)
    bootstrap_project(spec(project))
    assert "Run `make test` before shipping." in op.read_text()
    assert current in op.read_text().split("\n", 1)[0]


# -- the worker contract: `jarvis wo finish --evidence`


def _rendered_operation(project) -> str:
    bootstrap_project(spec(project))
    return (project / "OPERATION.md").read_text()


def _evidence_prose(text: str, window: int = 900) -> str:
    """The prose around `--evidence`, which is what the no-leaking rule governs"""
    at = text.index("--evidence")
    return text[max(0, at - window):at + window]


def _worker_prompt(project, project_spec=None) -> str:
    """What a running worker actually reads — the same call the daemon makes"""
    from jarvis.dispatch import build_worker_prompt
    from jarvis.project_store import ProjectStore

    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the thing")
    finally:
        store.close()
    return build_worker_prompt(wo, project_spec or spec(project))


def test_both_worker_texts_teach_the_evidence_flag(project):
    """OPERATION.md is what a worker reads if it goes LOOKING"""
    op, prompt = _rendered_operation(project), _worker_prompt(project)

    for text, where in ((op, "OPERATION.md"), (prompt, "the worker prompt")):
        assert "--evidence" in text, f"{where} never mentions the flag"
        # near the finishing paragraph, not filed off in a corner of its own
        finish_at = text.index("jarvis wo finish")
        assert 0 < text.index("--evidence") - finish_at < 1200, (
            f"{where} mentions --evidence nowhere near `jarvis wo finish`")


def test_neither_worker_text_reveals_who_reads_the_evidence(project):
    """The implementor and the reviewer do not know about each other — a design rule, not a style note"""
    from jarvis.project_store import VALIDATOR_SEATS

    for where, text in (("OPERATION.md", _rendered_operation(project)),
                        ("the worker prompt", _worker_prompt(project))):
        para = _evidence_prose(text)
        assert "--evidence" in para, f"{where} never added the paragraph"
        assert "review feedback" in para.lower(), (
            f"{where} does not frame it as review feedback")
        for word in ("panel", "seat", "seats", "validator", *VALIDATOR_SEATS):
            assert not re.search(rf"\b{word}\b", para, re.I), (
                f"{where} names {word!r} to the worker")


def test_no_post_block_exit_asks_the_worker_to_re_type_the_command(project):
    """The advice a blocked worker is given must be advice it can RUN"""
    from jarvis.gates import GateConfig
    from jarvis.worker_brief import render_section

    gated = spec(project, gates=GateConfig(enabled=frozenset({"release"})))
    bootstrap_project(gated)
    texts = {
        "OPERATION.md": (project / "OPERATION.md").read_text(),
        "the dispatched prompt": _worker_prompt(project, gated),
        "jarvis brief gates": render_section("gates", wo_id="wo-1",
                                             gates_enabled=("release",)),
    }
    for where, text in texts.items():
        for line in text.splitlines():
            line = line.strip().lstrip("`")
            if not line.startswith("jarvis gate "):
                continue
            verb = line.split()[2]
            if verb in ("contest", "explain"):
                # Neither is reachable before a block: there is nothing to contest and nothing to explain until the...
                assert "<request-number>" in line, (
                    f"{where} offers a post-block exit that re-types the command: {line}")
            elif verb == "request":
                assert "<request-number>" in line or "<the exact command>" in line, (
                    f"{where} has an unrecognised request spelling: {line}")
        assert "<request-number>" in text, f"{where} never teaches the number spelling"


def test_the_template_version_was_bumped_for_the_new_contract(project):
    """Without the bump the paragraph never reaches an already-bootstrapped project"""
    from jarvis.bootstrap import TEMPLATE_VERSION

    assert TEMPLATE_VERSION >= 9
    assert f"template v{TEMPLATE_VERSION}" in _rendered_operation(project)


def test_an_already_bootstrapped_project_is_regenerated_by_the_bump(project):
    """The bump's whole job"""
    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    from jarvis.bootstrap import TEMPLATE_VERSION
    stale = op.read_text().replace(f"template v{TEMPLATE_VERSION}", "template v8")
    op.write_text(stale.replace("--evidence", "--nothing-of-the-sort"))
    assert "--evidence" not in op.read_text()

    bootstrap_project(spec(project))

    assert "--evidence" in op.read_text()


# -- issue 279: the OS must not dirty a tree it never commits to


def _commit_all(path):
    """Make the fixture repo actually OWN its files — `make_git_project` only inits"""
    import subprocess

    for args in (["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)


def _restamp(op, version):
    """Rewrite the version marker in place, simulating a repo left behind by a bump"""
    from jarvis.bootstrap import TEMPLATE_VERSION

    op.write_text(op.read_text().replace(f"template v{TEMPLATE_VERSION}",
                                         f"template v{version}"))


def test_regenerating_an_unchanged_operation_md_writes_nothing(project):
    """The loop's first half: a second bootstrap must not rewrite an identical file"""
    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    before = op.stat().st_mtime_ns

    report = bootstrap_project(spec(project))

    assert "OPERATION.md already up to date" in report.actions
    assert op.stat().st_mtime_ns == before, "rewrote a file it did not need to touch"


def test_a_stale_gates_section_is_regenerated_under_an_unchanged_version(project):
    """The proxy was wrong in the other direction too: a gates section that no longer matched the catalog..."""
    from jarvis.gates import GateConfig

    bootstrap_project(spec(project))
    assert "gated, not forbidden" not in (project / "OPERATION.md").read_text()

    bootstrap_project(spec(project, gates=GateConfig(enabled=frozenset({"release"}))))

    assert "gated, not forbidden" in (project / "OPERATION.md").read_text()


def test_a_committed_operation_md_is_never_overwritten(project):
    """The invariant that terminates the loop: the OS never commits"""
    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    _restamp(op, 3)
    _commit_all(project)
    stale = op.read_text()

    report = bootstrap_project(spec(project))

    assert op.read_text() == stale, "overwrote a file the repository owns"
    assert any("does not overwrite files the repository owns" in w
               for w in report.warnings), report.warnings
    assert any("template v3" in w for w in report.warnings), report.warnings


def test_force_config_regenerates_a_committed_operation_md(project):
    """The escape hatch the warning names"""
    from jarvis.bootstrap import TEMPLATE_VERSION

    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    _restamp(op, 3)
    _commit_all(project)

    report = bootstrap_project(spec(project), force_config=True)

    assert f"template v{TEMPLATE_VERSION}" in op.read_text().split("\n", 1)[0]
    assert not any("repository owns" in w for w in report.warnings)


def test_an_untracked_operation_md_is_still_refreshed(project):
    """The rule is about ownership, not staleness: a project that does not commit its contract keeps..."""
    from jarvis.bootstrap import TEMPLATE_VERSION

    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    _restamp(op, 3)

    bootstrap_project(spec(project))

    assert f"template v{TEMPLATE_VERSION}" in op.read_text().split("\n", 1)[0]


def test_the_template_does_not_prescribe_the_foreground_for_long_work(project):
    """§6 of docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md"""
    from jarvis.bootstrap import ASSETS

    template = (ASSETS / "OPERATION.md.tmpl").read_text()

    assert "runs in the *foreground* and you wait for it" not in template
    assert "in the FOREGROUND and wait" not in template
    for rhythm in ("run_in_background", "BashOutput", "3-4 minutes", "240 seconds",
                   "uncollected"):
        assert rhythm in template, rhythm
    # Still true, and still there: nothing about backgrounding changes what starts the next turn
    assert "Exactly three things start your next turn" in template
    assert "I'll pick this up when the background run finishes" in template
    # …and the rendered contract carries it, which is the copy a worker reads
    assert "3-4 minutes" in _rendered_operation(project)


def test_this_repos_committed_operation_md_matches_its_generator():
    """The drift that made issue 279: the committed contract sat at template v7 while the generator..."""
    from pathlib import Path

    from jarvis.bootstrap import operation_specifics, render_operation_md
    from jarvis.gates import GateConfig

    root = Path(__file__).resolve().parent.parent
    committed = (root / "OPERATION.md").read_text()
    rendered = render_operation_md(
        ProjectSpec(name="jarvis_os", path=root,
                    gates=GateConfig(enabled=frozenset(
                        {"pr_merge", "release", "service_restart"}))),
        operation_specifics(committed))

    assert committed == rendered, (
        "this repo's committed OPERATION.md is not what the generator produces — "
        "run `jarvis adopt . --force-config` and commit the result")


def test_install_agent_assets_returns_three_roots_for_worker(project, jarvis_home):
    """Spec 2026-09-23-the-crew-a-worker-must-use.md SS5: skills, the crew, and nothing of the planner's"""
    from jarvis.bootstrap import install_agent_assets

    roots = install_agent_assets(project, kind="worker")
    assert [r.name for r in roots] == ["agent-skills", "agent-crew"]
    assert [r.name for r in install_agent_assets(project, kind="planner")] == [
        "agent-skills", "agent-seats"]


# -- offboard: the repo strip (docs/superpowers/specs/2026-10-28-offboard-a-project.md §4)


def _snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.parts}


def test_unbootstrap_keeps_user_edits_and_is_idempotent(tmp_path, monkeypatch):
    from jarvis.bootstrap import unbootstrap_project, workspace_trusted
    from jarvis.testing import make_git_project
    p = make_git_project(tmp_path, "offboarded")
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"projects": {str(p): {"lastCost": 1.5}}}))
    monkeypatch.setenv("JARVIS_CLAUDE_JSON", str(cfg))
    bootstrap_project(spec(p))
    (p / SKILL).write_text("mine\n")
    op = p / "OPERATION.md"
    op.write_text(op.read_text() + "\nmy notes\n")

    first = unbootstrap_project(spec(p), purge=False, dry_run=False)
    assert "removed .claude/settings.json" in first.actions
    assert not (p / ".claude" / "settings.json").exists()
    assert workspace_trusted(p) is False
    assert json.loads(cfg.read_text())["projects"][str(p)] == {"lastCost": 1.5}
    assert (p / ".jarvis").is_dir() and ".jarvis/" in (p / ".gitignore").read_text()

    kept = _snapshot(p)
    second = unbootstrap_project(spec(p), purge=False, dry_run=False)
    assert _snapshot(p) == kept
    assert any("already absent" in a for a in second.actions)
    assert (p / SKILL).read_text() == "mine\n"
    assert not op.exists()
    assert (p / ".jarvis" / "OPERATION.md.offboarded").read_text().endswith("my notes\n")


def test_unbootstrap_operation_md_committed_or_edited(tmp_path, monkeypatch):
    """Offboard spec §4: OPERATION.md never lingers; only bytes git cannot restore survive"""
    import subprocess
    from jarvis.bootstrap import unbootstrap_project
    from jarvis.testing import make_git_project
    monkeypatch.setenv("JARVIS_CLAUDE_JSON", str(tmp_path / "claude.json"))
    p = make_git_project(tmp_path, "committed")
    bootstrap_project(spec(p))
    subprocess.run(["git", "-C", str(p), "add", "OPERATION.md"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-qm", "op"], check=True)
    op = p / "OPERATION.md"
    op.write_text("committed, then edited\n")
    subprocess.run(["git", "-C", str(p), "commit", "-qam", "edit"], check=True)

    report = unbootstrap_project(spec(p), purge=False, dry_run=False)
    assert "removed OPERATION.md" in report.actions and not op.exists()
    assert not (p / ".jarvis" / "OPERATION.md.offboarded").exists()
    assert any("left for you to commit" in w for w in report.warnings)

    q = make_git_project(tmp_path, "purged-edited")
    bootstrap_project(spec(q))
    (q / "OPERATION.md").write_text("mine\n")
    purge = unbootstrap_project(spec(q), purge=True, dry_run=False)
    assert (q / "OPERATION.md").read_text() == "mine\n"
    assert any(f"rm {q / 'OPERATION.md'}" in w for w in purge.warnings)


def test_unbootstrap_keeps_claude_dir_holding_an_empty_user_dir(tmp_path, monkeypatch):
    """Issue 1330: an empty .claude/worktrees/ the OS never wrote must not abort the strip"""
    from jarvis.bootstrap import unbootstrap_project, workspace_trusted
    from jarvis.testing import make_git_project
    p = make_git_project(tmp_path, "worktrees")
    monkeypatch.setenv("JARVIS_CLAUDE_JSON", str(tmp_path / "claude.json"))
    bootstrap_project(spec(p))
    (p / ".claude" / "worktrees").mkdir()

    report = unbootstrap_project(spec(p), purge=False, dry_run=False)
    assert [x.name for x in (p / ".claude").iterdir()] == ["worktrees"]
    assert "removed OPERATION.md" in report.actions
    assert not (p / "OPERATION.md").exists() and workspace_trusted(p) is False


def test_unbootstrap_purge_restores_backup_and_dry_run_writes_nothing(tmp_path, monkeypatch):
    from jarvis.bootstrap import unbootstrap_project
    from jarvis.testing import make_git_project
    p = make_git_project(tmp_path, "purged")
    monkeypatch.setenv("JARVIS_CLAUDE_JSON", str(tmp_path / "claude.json"))
    (p / ".gitignore").write_text("node_modules/\n")
    (p / ".claude").mkdir()
    (p / ".claude" / "settings.json").write_text('{"mine": true}\n')
    bootstrap_project(spec(p))

    before = _snapshot(p)
    dry = unbootstrap_project(spec(p), purge=True, dry_run=True)
    assert _snapshot(p) == before
    assert "would remove .claude/skills" in dry.actions
    assert "would remove .jarvis" in dry.actions

    unbootstrap_project(spec(p), purge=True, dry_run=False)
    assert (p / ".claude" / "settings.json").read_text() == '{"mine": true}\n'
    assert sorted(x.name for x in (p / ".claude").iterdir()) == ["settings.json"]
    assert not (p / "OPERATION.md").exists() and not (p / ".jarvis").exists()
    assert (p / ".gitignore").read_text() == "node_modules/\n"
