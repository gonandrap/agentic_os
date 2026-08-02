import json

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
    """The user's own sessions only ever load <project>/.claude/skills/."""
    report = bootstrap_project(spec(project))
    body = (project / SKILL).read_text()
    assert "name: jarvis-inject-session" in body
    assert any("installed .claude/skills/" in a for a in report.actions)
    # second pass: unchanged content is left alone, not rewritten
    report2 = bootstrap_project(spec(project))
    assert ".claude/skills/ already up to date" in report2.actions


def test_inject_skill_uses_the_session_id_claude_exports(project):
    """The id comes from the environment Claude Code sets, never from a guess, and a
    missing one has to fail out loud rather than inject an empty string."""
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
    """A worker session is already a work order; self-injection would file a second
    record against the same session id."""
    from jarvis.bootstrap import install_agent_skills
    root = install_agent_skills(project)
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
