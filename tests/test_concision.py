"""The three things the OS ships so its sessions stop being verbose (docs/superpowers/specs/2026-08-22-agent-concision.md)"""

import json

import pytest

from jarvis.bootstrap import ASSETS, bootstrap_project, build_settings
from jarvis.catalog import ProjectSpec
from jarvis.dispatch import _write_worker_settings


def _spec(project, **kw) -> ProjectSpec:
    return ProjectSpec(name="proj_a", path=project, description="", **kw)


# -- the output style (SS3)

def test_both_audiences_get_the_concise_output_style(project, jarvis_home):
    """The point of putting it in `settings.base.json` rather than in either writer: ONE line has to cover..."""
    bootstrap_project(_spec(project))
    user_session = json.loads((project / ".claude" / "settings.json").read_text())
    assert user_session["outputStyle"] == "Concise"

    worker = json.loads(
        _write_worker_settings(_spec(project), {"id": "wo-conc01"}).read_text())
    assert worker["outputStyle"] == "Concise"


def test_a_project_can_override_the_style():
    """The escape hatch is the catalog, not an edit to the project"""
    assert build_settings({"outputStyle": "Explanatory"})["outputStyle"] == "Explanatory"


# -- the worker skill (SS4)

def test_the_skill_reaches_every_worker_and_can_be_model_invoked(project):
    """Upstream ships this as an output style with `disable-model-invocation: true`"""
    from jarvis import bootstrap

    roots = bootstrap.install_agent_assets(project, kind="worker")
    delivered = [r / ".claude" / "skills" / "i-have-adhd" / "SKILL.md" for r in roots]
    hit = [p for p in delivered if p.is_file()]
    assert hit, f"skill not delivered to any --add-dir root: {roots}"
    text = hit[0].read_text()
    assert "disable-model-invocation" not in text
    # The description is what decides whether the model ever loads it
    front = text.split("---")[1]
    for surface in ("work-order message", "PR body", "code comment"):
        assert surface in front, f"description does not mention {surface!r}"


def test_caveman_is_adapted_and_says_so(project):
    """It shipped verbatim until 2026-09-19 and no longer does"""
    from jarvis import bootstrap

    vendored = (ASSETS / "skills" / "caveman" / "SKILL.md").read_text()
    # Whitespace-collapsed: the description is a folded YAML scalar
    front = " ".join(vendored.split("---")[1].split())
    assert "disable-model-invocation" not in vendored
    # The upstream trigger — "user says caveman mode"
    for surface in ("finish summary", "PR bodies", "code comments"):
        assert surface in front, f"description does not name {surface!r}"

    # The self-exclusion
    body = vendored[vendored.index("## Boundaries"):]
    assert "no persisted-outside-chat exemption" in body
    assert "commit messages and PR bodies" in body

    # ...but the correctness rails are NOT collateral
    for rail in ("exact error strings", "not / never / no / only / except",
                 "security warnings", "failing test output"):
        assert rail in body, f"correctness rail dropped from caveman: {rail!r}"

    roots = bootstrap.install_agent_assets(project, kind="worker")
    hit = [r / ".claude" / "skills" / "caveman" / "SKILL.md" for r in roots]
    hit = [q for q in hit if q.is_file()]
    assert hit, f"caveman not delivered to any --add-dir root: {roots}"
    assert hit[0].read_text() == vendored


def test_the_readme_does_not_still_promise_a_verbatim_copy(project):
    """The README's whole value is that a reader can diff against upstream"""
    readme = (ASSETS / "skills" / "caveman" / "README.md").read_text()
    assert "no longer a verbatim copy" in readme
    assert "The diff against upstream" in readme


def test_both_skills_ship_their_licences(project):
    """Both copies are third-party MIT inside a GPL-3.0 repo"""
    from jarvis import bootstrap

    for name in ("i-have-adhd", "caveman"):
        assert "MIT License" in (ASSETS / "skills" / name / "LICENSE").read_text()
    roots = bootstrap.install_agent_assets(project, kind="worker")
    for name in ("i-have-adhd", "caveman"):
        assert any((r / ".claude" / "skills" / name / "LICENSE").is_file()
                   for r in roots), f"{name} licence did not reach the worker tree"


# -- the brief (SS6)

def test_the_core_carries_the_three_rules_without_a_fetch(project):
    """A worker that never runs `jarvis brief concision` still has to obey them"""
    from jarvis.dispatch import build_worker_prompt

    p = build_worker_prompt({"id": "wo-conc01", "title": "t", "description": "d"},
                            _spec(project))
    core = p[p.index("# Operating contract"):p.index("# Full briefings")]
    assert "docs/superpowers/specs/" in core          # comments cite a spec
    assert "A PR body hints" in core                  # the PR body hints
    assert "already on this record" in core           # say each thing once
    assert "jarvis brief concision" in core           # and the fetch is named


def test_the_section_names_the_knowledge_it_argues_from():
    """kn-f861a2f6 is the review this rule came out of"""
    from jarvis import worker_brief

    text = worker_brief.render_section("concision")
    assert "kn-f861a2f6" in text
    for heading in ("Code comments", "PR body", "once"):
        assert heading in text
    # Concision is never an excuse to drop evidence
    assert "correctness" in text


# -- the message cap's transport (§5 of the bounded-model-inputs spec)

def test_the_message_cap_travels_to_the_worker_by_env(project, jarvis_home):
    """Same transport as the summary cap and for the same reason: the hook runs on every Bash call and..."""
    from jarvis import concision
    from jarvis.catalog import ConcisionConfig

    written = json.loads(
        _write_worker_settings(_spec(project), {"id": "wo-conc04"}).read_text())
    assert written["env"][concision.MESSAGE_CAP_ENV] == str(
        concision.DEFAULT_MESSAGE_MAX_CHARS)

    tighter = json.loads(_write_worker_settings(
        _spec(project, concision=ConcisionConfig(message_max_chars=2500)),
        {"id": "wo-conc05"}).read_text())
    assert tighter["env"][concision.MESSAGE_CAP_ENV] == "2500"
    assert concision.message_cap(tighter["env"]) == 2500


def test_a_project_can_switch_the_message_cap_off_but_not_set_a_nonsense_one():
    """`0` is off, which a project is entitled to"""
    from jarvis.catalog import CatalogError, _parse_concision

    assert _parse_concision({"message_max_chars": 0}).message_max_chars == 0
    assert _parse_concision({"message_max_chars": 9000}).message_max_chars == 9000
    for bad in (500, -1):
        with pytest.raises(CatalogError):
            _parse_concision({"message_max_chars": bad})


# -- the assumption and question caps (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §1)

def test_the_two_new_caps_default_to_one_thousand_and_two_thousand():
    from jarvis import concision, sections
    from jarvis.catalog import _parse_concision

    assert concision.DEFAULT_ASSUME_MAX_CHARS == 1000
    assert concision.DEFAULT_QUESTION_MAX_CHARS == 2000
    # `concision` owns the number; `sections` re-exports it
    assert sections.QUESTION_MAX_CHARS == concision.DEFAULT_QUESTION_MAX_CHARS
    assert _parse_concision({}).assume_max_chars == 1000
    assert _parse_concision({}).question_max_chars == 2000


@pytest.mark.parametrize("key", ["assume_max_chars", "question_max_chars"])
def test_a_project_can_tune_or_switch_each_new_cap_off_but_not_go_negative(key):
    from jarvis.catalog import CatalogError, _parse_concision

    assert getattr(_parse_concision({key: 0}), key) == 0
    assert getattr(_parse_concision({key: 400}), key) == 400
    with pytest.raises(CatalogError, match=f"{key} must be 0"):
        _parse_concision({key: -1})


def test_both_new_caps_travel_to_the_worker_by_env(project, jarvis_home):
    """The hook refuses before the shell expands anything, so it carries them as env"""
    from jarvis import concision
    from jarvis.catalog import ConcisionConfig

    written = json.loads(
        _write_worker_settings(_spec(project), {"id": "wo-conc06"}).read_text())
    assert written["env"][concision.ASSUME_CAP_ENV] == "1000"
    assert written["env"][concision.QUESTION_CAP_ENV] == "2000"

    tighter = json.loads(_write_worker_settings(
        _spec(project, concision=ConcisionConfig(assume_max_chars=300,
                                                 question_max_chars=700)),
        {"id": "wo-conc07"}).read_text())
    assert concision.assume_cap(tighter["env"]) == 300
    assert concision.question_cap(tighter["env"]) == 700


@pytest.mark.parametrize("raw", [None, "", "nonsense", "-5"])
def test_an_unusable_env_value_falls_back_to_the_default(raw):
    from jarvis import concision

    env = {} if raw is None else {concision.ASSUME_CAP_ENV: raw,
                                  concision.QUESTION_CAP_ENV: raw}
    assert concision.assume_cap(env) == concision.DEFAULT_ASSUME_MAX_CHARS
    assert concision.question_cap(env) == concision.DEFAULT_QUESTION_MAX_CHARS


# -- the subagent transport (SS5.1)

def test_the_standing_prompt_travels_to_the_worker_by_env(project, jarvis_home):
    """The subagent injection's only moving part"""
    from jarvis import concision, hooks
    from jarvis.catalog import WorkerDefaults

    spec = _spec(project, worker=WorkerDefaults(append_system_prompt="catalog rule"))
    written = json.loads(_write_worker_settings(spec, {"id": "wo-conc02"}).read_text())
    env = written["env"]
    assert env[concision.STANDING_PROMPT_ENV] == "catalog rule"

    injected = hooks.handle_hook(
        {"hook_event_name": "SubagentStart", "agent_type": "general-purpose"}, env)
    assert "catalog rule" in injected["hookSpecificOutput"]["additionalContext"]

    override = json.loads(_write_worker_settings(
        spec, {"id": "wo-conc03", "append_system_prompt": "this order's rule"}).read_text())
    assert override["env"][concision.STANDING_PROMPT_ENV] == "this order's rule"


def test_the_subagent_hook_is_wired_in_the_baseline_settings():
    """It reaches a worker through the same file as every other hook"""
    entries = build_settings({})["hooks"]["SubagentStart"]

    assert len(entries) == 1
    assert "matcher" not in entries[0]  # every agent type
    assert entries[0]["hooks"][0]["command"] == \
        build_settings({})["hooks"]["SessionStart"][0]["hooks"][0]["command"]
