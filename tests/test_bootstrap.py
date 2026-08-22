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


# -- the worker contract: `jarvis wo finish --evidence` -------------------------------


def _rendered_operation(project) -> str:
    bootstrap_project(spec(project))
    return (project / "OPERATION.md").read_text()


def _evidence_prose(text: str, window: int = 900) -> str:
    """The prose around `--evidence`, which is what the no-leaking rule governs.

    The rest of either document is not in scope and must not be: OPERATION.md's Serena
    section says "architecture" repeatedly, and a whole-document search would fail on
    that rather than on anything about the reviewer.
    """
    at = text.index("--evidence")
    return text[max(0, at - window):at + window]


def _worker_prompt(project) -> str:
    """What a running worker actually reads — the same call the daemon makes."""
    from jarvis.dispatch import build_worker_prompt
    from jarvis.project_store import ProjectStore

    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the thing")
    finally:
        store.close()
    return build_worker_prompt(wo, spec(project))


def test_both_worker_texts_teach_the_evidence_flag(project):
    """OPERATION.md is what a worker reads if it goes LOOKING; the dispatched prompt is
    what it reads without looking. They answer different questions and both have to
    carry the flag, or the half that does not silently trains the behaviour we are
    trying to end."""
    op, prompt = _rendered_operation(project), _worker_prompt(project)

    for text, where in ((op, "OPERATION.md"), (prompt, "the worker prompt")):
        assert "--evidence" in text, f"{where} never mentions the flag"
        # near the finishing paragraph, not filed off in a corner of its own
        finish_at = text.index("jarvis wo finish")
        assert 0 < text.index("--evidence") - finish_at < 1200, (
            f"{where} mentions --evidence nowhere near `jarvis wo finish`")


def test_neither_worker_text_reveals_who_reads_the_evidence(project):
    """The implementor and the reviewer do not know about each other — a design rule,
    not a style note. From the worker's side this is review feedback and nothing more.

    PAIRED with the positive assertion on purpose: "the words panel, seat and validator
    are absent" is satisfied perfectly by a paragraph that was never written, so the
    absence proves nothing on its own.

    Scoped to the evidence prose rather than the whole document, and on WORD boundaries:
    OPERATION.md talks about a project's "architecture" all over its Serena section, and
    an unanchored substring search would fail on that instead of on anything this rule
    is about.
    """
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


def test_the_template_version_was_bumped_for_the_new_contract(project):
    """Without the bump the paragraph never reaches an already-bootstrapped project —
    the version comments in bootstrap.py are three separate records of exactly that
    mistake. Pinned to the value this work order shipped, so a later edit to the
    template that forgets the bump fails here rather than in production."""
    from jarvis.bootstrap import TEMPLATE_VERSION

    assert TEMPLATE_VERSION >= 9
    assert f"template v{TEMPLATE_VERSION}" in _rendered_operation(project)


def test_an_already_bootstrapped_project_is_regenerated_by_the_bump(project):
    """The bump's whole job. A repo carrying the previous version's OPERATION.md is
    rewritten on the next bootstrap and comes away with the new paragraph."""
    bootstrap_project(spec(project))
    op = project / "OPERATION.md"
    from jarvis.bootstrap import TEMPLATE_VERSION
    stale = op.read_text().replace(f"template v{TEMPLATE_VERSION}", "template v8")
    op.write_text(stale.replace("--evidence", "--nothing-of-the-sort"))
    assert "--evidence" not in op.read_text()

    bootstrap_project(spec(project))

    assert "--evidence" in op.read_text()
