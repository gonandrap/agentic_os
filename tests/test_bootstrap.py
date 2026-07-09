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


def test_operation_md_preserves_specifics(project):
    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    text = op.read_text().replace("template v1", "template v0")  # simulate old version
    text = text.replace("_None yet._", "Run `make test` before shipping.")
    op.write_text(text)
    bootstrap_project(spec(project))
    assert "Run `make test` before shipping." in op.read_text()
    assert "template v1" in op.read_text().split("\n", 1)[0]
