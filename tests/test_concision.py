"""The three things the OS ships so its sessions stop being verbose.

Design: docs/superpowers/specs/2026-08-22-agent-concision.md. One test per surface —
the output style (SS3), the worker skill (SS4), the brief (SS6) — because they reach
sessions by three different transports and any one of them can break alone.
"""

import json

from jarvis.bootstrap import ASSETS, bootstrap_project, build_settings
from jarvis.catalog import ProjectSpec
from jarvis.dispatch import _write_worker_settings


def _spec(project, **kw) -> ProjectSpec:
    return ProjectSpec(name="proj_a", path=project, description="", **kw)


# -- the output style (SS3) -------------------------------------------------------------

def test_both_audiences_get_the_concise_output_style(project, jarvis_home):
    """The point of putting it in `settings.base.json` rather than in either writer:
    ONE line has to cover the sessions the user opens by hand AND the ones Jarvis
    spawns, and those are two different files written by two different modules.

    Asserting the literal string, not a constant. `Concise` is one of the four names
    the CLI recognises (`Proactive`, `Explanatory`, `Learning` are the others); a typo
    is silently ignored by Claude Code, so nothing else would catch it.
    """
    bootstrap_project(_spec(project))
    user_session = json.loads((project / ".claude" / "settings.json").read_text())
    assert user_session["outputStyle"] == "Concise"

    worker = json.loads(
        _write_worker_settings(_spec(project), {"id": "wo-conc01"}).read_text())
    assert worker["outputStyle"] == "Concise"


def test_a_project_can_override_the_style():
    """The escape hatch is the catalog, not an edit to the project — same rule as every
    other baseline setting."""
    assert build_settings({"outputStyle": "Explanatory"})["outputStyle"] == "Explanatory"


# -- the worker skill (SS4) -------------------------------------------------------------

def test_the_skill_reaches_every_worker_and_can_be_model_invoked(project):
    """Upstream ships this as an output style with `disable-model-invocation: true`,
    reachable only by typing `/i-have-adhd`. A headless `-p` worker cannot type a slash
    command, so a copy carrying that flag would be inert — the adaptation IS the flag's
    absence, and that is what this pins.
    """
    from jarvis import bootstrap

    roots = bootstrap.install_agent_assets(project, kind="worker")
    delivered = [r / ".claude" / "skills" / "i-have-adhd" / "SKILL.md" for r in roots]
    hit = [p for p in delivered if p.is_file()]
    assert hit, f"skill not delivered to any --add-dir root: {roots}"
    text = hit[0].read_text()
    assert "disable-model-invocation" not in text
    # The description is what decides whether the model ever loads it, so it has to
    # name the surfaces this work order was filed about.
    front = text.split("---")[1]
    for surface in ("work-order message", "PR body", "code comment"):
        assert surface in front, f"description does not mention {surface!r}"


def test_caveman_is_adapted_and_says_so(project):
    """It shipped verbatim until 2026-09-19 and no longer does.

    The verbatim copy was invoked 0 times in 142 worker sessions and would have
    compressed nothing if it had loaded, because its upstream `## Boundaries` section
    excluded every surface a Jarvis worker writes (spec 2026-09-19 SS2). What this pins
    is the two properties that made the old copy inert, so neither can come back: the
    trigger must name Jarvis surfaces, and the self-exclusion must be gone.
    """
    from jarvis import bootstrap

    vendored = (ASSETS / "skills" / "caveman" / "SKILL.md").read_text()
    # Whitespace-collapsed: the description is a folded YAML scalar, so a phrase the
    # reader sees as one line may be split across two in the file.
    front = " ".join(vendored.split("---")[1].split())
    assert "disable-model-invocation" not in vendored
    # The upstream trigger — "user says caveman mode" — describes a conversation a
    # headless worker cannot have. The description has to name what it actually governs.
    for surface in ("finish summary", "PR bodies", "code comments"):
        assert surface in front, f"description does not name {surface!r}"

    # The self-exclusion. Upstream sends "code, comments, commits, docs, issue/PR/MR
    # text" back to normal prose; under that rule the skill has no job here at all.
    body = vendored[vendored.index("## Boundaries"):]
    assert "no persisted-outside-chat exemption" in body
    assert "commit messages and PR bodies" in body

    # ...but the correctness rails are NOT collateral. SS4.2: these guard accuracy, not
    # length, and a future trim for brevity must fail here rather than in production.
    for rail in ("exact error strings", "not / never / no / only / except",
                 "security warnings", "failing test output"):
        assert rail in body, f"correctness rail dropped from caveman: {rail!r}"

    roots = bootstrap.install_agent_assets(project, kind="worker")
    hit = [r / ".claude" / "skills" / "caveman" / "SKILL.md" for r in roots]
    hit = [q for q in hit if q.is_file()]
    assert hit, f"caveman not delivered to any --add-dir root: {roots}"
    assert hit[0].read_text() == vendored


def test_the_readme_does_not_still_promise_a_verbatim_copy(project):
    """The README's whole value is that a reader can diff against upstream. It said
    "verbatim" and "do not edit it in place"; once the file is adapted, leaving that
    claim there is worse than having no README."""
    readme = (ASSETS / "skills" / "caveman" / "README.md").read_text()
    assert "no longer a verbatim copy" in readme
    assert "The diff against upstream" in readme


def test_both_skills_ship_their_licences(project):
    """Both copies are third-party MIT inside a GPL-3.0 repo; attribution travels with
    them to every worker, not just to this checkout."""
    from jarvis import bootstrap

    for name in ("i-have-adhd", "caveman"):
        assert "MIT License" in (ASSETS / "skills" / name / "LICENSE").read_text()
    roots = bootstrap.install_agent_assets(project, kind="worker")
    for name in ("i-have-adhd", "caveman"):
        assert any((r / ".claude" / "skills" / name / "LICENSE").is_file()
                   for r in roots), f"{name} licence did not reach the worker tree"


# -- the brief (SS6) --------------------------------------------------------------------

def test_the_core_carries_the_three_rules_without_a_fetch(project):
    """A worker that never runs `jarvis brief concision` still has to obey them, so the
    actionable form is in the opening prompt and only the reasoning is behind the fetch.
    """
    from jarvis.dispatch import build_worker_prompt

    p = build_worker_prompt({"id": "wo-conc01", "title": "t", "description": "d"},
                            _spec(project))
    core = p[p.index("# Operating contract"):p.index("# Full briefings")]
    assert "docs/superpowers/specs/" in core          # comments cite a spec
    assert "A PR body hints" in core                  # the PR body hints
    assert "already on this record" in core           # say each thing once
    assert "jarvis brief concision" in core           # and the fetch is named


def test_the_section_names_the_knowledge_it_argues_from():
    """kn-f861a2f6 is the review this rule came out of. Citing the id lets a worker
    fetch the full story instead of the section restating it — which would be the
    section breaking its own rule."""
    from jarvis import worker_brief

    text = worker_brief.render_section("concision")
    assert "kn-f861a2f6" in text
    for heading in ("Code comments", "PR body", "once"):
        assert heading in text
    # Concision is never an excuse to drop evidence.
    assert "correctness" in text
