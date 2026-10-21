"""Has the worker's cacheable prompt head started moving?"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from jarvis import claude_cli, dispatch
from jarvis.catalog import ProjectSpec
from jarvis.testing import make_git_project
from jarvis.worker_brief import git_briefing
from jarvis.worker_session import briefing_for

scenario = pytest.mark.scenario


# -- the precedence rule

AUTHORITY = "invariants.check_prefix_stable"

#: See `test_the_head_is_not_being_hollowed_out`
HEAD_SHARE_FLOOR = 0.5


# -- what gets rendered


@dataclass(frozen=True)
class Render:
    """One worker's rendered prefix, and the variable text it was entitled to carry"""

    label: str
    model: str
    prompt: str        # --append-system-prompt, verbatim
    standing: str      # the operator's own instructions for this worker, or ""
    settings: dict
    #: The project's resolved `worker.cache_ttl` arm
    cache_ttl: str = "5m"


def _fleet(tmp_path, jarvis_home) -> list[Render]:
    """A fleet whose renders differ in every way a real one does"""
    out: list[Render] = []
    # The cache-TTL arm varies across the fleet so the settings assertion is pinned to each project's own...
    cases = [
        ("bare worker", "proj_a", "claude-opus-5", "", "5m"),
        ("worker, project instructions", "proj_a", "claude-opus-5",
         "Always run the linter before you commit.", "5m"),
        ("worker, work-order instructions", "proj_b", "claude-opus-5",
         "Never touch the vendored tree.", "1h"),
        ("planner", "proj_b", "claude-opus-5",
         "Zero in on the smallest plan that ships.", "cli"),
        ("other model", "proj_a", "claude-haiku-4-5-20251001",
         "Only the trailer may differ.", "5m"),
    ]
    paths = {}
    for label, pname, model, standing, arm in cases:
        if pname not in paths:
            paths[pname] = make_git_project(tmp_path, pname)
        spec = ProjectSpec(name=pname, path=paths[pname], description="")
        spec.worker.cache_ttl = arm
        wo = {"id": f"wo-{label.replace(' ', '')[:8]}", "title": label, "model": model,
              "kind": "planner" if label == "planner" else "worker"}
        # whether the text arrives on the project or on the work order is deliberately mixed: both land in...
        if "project" in label:
            spec.worker.append_system_prompt = standing
        else:
            wo["append_system_prompt"] = standing
        brief = briefing_for(spec, wo)
        out.append(Render(
            label=label, model=model,
            prompt=brief["append_system_prompt"], standing=standing,
            settings=json.loads(brief["settings_file"].read_text()),
            cache_ttl=arm,
        ))
    return out


@pytest.fixture()
def fleet(tmp_path, jarvis_home):
    return _fleet(tmp_path, jarvis_home)


# -- the detector, as a function so it can be run in both directions


def _common_prefix(values: list[str]) -> str:
    first, last = min(values), max(values)
    for i, ch in enumerate(first):
        if i >= len(last) or last[i] != ch:
            return first[:i]
    return first


def head_findings(renders: list[Render]) -> list[str]:
    """What moved, and which side it moved to"""
    findings: list[str] = []
    by_model: dict[str, list[Render]] = {}
    for r in renders:
        by_model.setdefault(r.model, []).append(r)

    for model, group in sorted(by_model.items()):
        head = git_briefing(model)
        for r in group:
            if not r.prompt.startswith(head):
                at = len(_common_prefix([r.prompt, head]))
                findings.append(
                    f"{r.label}: the shared head is not at the start of the prompt — "
                    f"they diverge at byte {at}, where the head has "
                    f"{head[at:at + 60]!r} and the render has {r.prompt[at:at + 60]!r}. "
                    f"Per-order text has leaked INTO the head, or the composition order "
                    f"in `worker_session._append_system_prompt` has changed.")
                continue
            tail = r.prompt[len(head):]
            expected = f"\n\n{r.standing}" if r.standing else ""
            if tail != expected:
                findings.append(
                    f"{r.label}: {len(tail)} bytes follow the shared head that are not "
                    f"this worker's own standing instructions: {tail[:120]!r}. Static "
                    f"text placed after the variable region is paid for by every order "
                    f"in the fleet instead of being shared.")
        if len(group) > 1:
            shared = _common_prefix([r.prompt for r in group])
            if shared != head:
                where = "longer than" if len(shared) > len(head) else "shorter than"
                findings.append(
                    f"model {model}: the fleet's longest common prefix is {len(shared)} "
                    f"bytes, {where} the {len(head)}-byte head `git_briefing` declares. "
                    f"The cacheable region is not the one the code says it is.")
    return findings


# -- the battery


@scenario("prefix-drift", "the fleet's renders differ from each other")
def test_the_fixture_fleet_is_not_secretly_uniform(fleet):
    """kn-45bae078 (1): a battery that asserts a structure goes green when it stops rendering anything"""
    assert len(fleet) >= 4
    assert len({r.prompt for r in fleet}) == len(fleet), "every render is distinct"
    tails = [r.standing for r in fleet if r.standing]
    assert len({t[0] for t in tails}) == len(tails), "tails share no first byte"


@scenario("prefix-drift", "the head is the same bytes for every work order")
def test_the_shared_head_is_the_longest_common_prefix(fleet):
    assert head_findings(fleet) == [], (
        f"the worker prompt's cacheable head has moved. {AUTHORITY} is the authority on "
        f"whether the cache actually suffered; this is the early signal that something "
        f"changed.")


@scenario("prefix-drift", "the detector fires when the head really moves")
@pytest.mark.parametrize("drift", ["leak-in", "drift-out", "reorder"])
def test_a_planted_drift_is_caught(fleet, drift):
    """The negative control (kn-5a5d47fa (5))"""
    head = git_briefing(fleet[0].model)
    victim = fleet[0]
    if drift == "leak-in":                      # per-order text inside the head
        moved = head.replace("# Git", f"# Git ({victim.label})", 1)
        broken = moved + victim.prompt[len(head):]
    elif drift == "drift-out":                  # static text after the variable region
        broken = victim.prompt + "\n\nRun the tests before you finish."
    else:                                       # the head no longer comes first
        broken = f"{victim.standing or 'x'}\n\n{head}"

    found = head_findings([Render(victim.label, victim.model, broken, victim.standing,
                                  victim.settings)]
                          + [r for r in fleet if r is not victim])
    assert found, f"a planted {drift} drift went undetected"


@scenario("prefix-drift", "the head does not move when the world does")
def test_the_head_survives_everything_that_is_not_an_input(tmp_path, jarvis_home,
                                                           monkeypatch):
    """The prefix is a function of (project, work order, model) and of nothing else"""
    path = make_git_project(tmp_path, "proj_churn")
    spec = ProjectSpec(name="proj_churn", path=path, description="")
    wo = {"id": "wo-churn", "title": "t", "model": "claude-opus-5"}
    before = briefing_for(spec, wo)["append_system_prompt"]

    (path / "worker-edited-this.py").write_text("x = 1\n")
    (path / "README.md").write_text("# changed by the worker\n")
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CLAUDE_CODE_SOMETHING_NEW", "1")
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")

    assert briefing_for(spec, wo)["append_system_prompt"] == before


@scenario("prefix-drift", "the settings still switch off the CLI's own dynamic blocks")
def test_the_levers_that_suppress_cli_side_churn_are_still_set(fleet):
    """Jarvis owns only half the prompt (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md §1)"""
    for r in fleet:
        assert r.settings["includeGitInstructions"] is False, r.label
        env = r.settings["env"]
        assert {k: env[k] for k in claude_cli.CACHE_TTL_KEYS if k in env} \
            == dispatch.cache_ttl_env(r.cache_ttl), r.label


@scenario("prefix-drift", "the shared head is the bulk of what every worker carries")
def test_the_head_is_not_being_hollowed_out(fleet):
    """A loose floor, not a pinned size (Neo question 362)"""
    total = sum(len(r.prompt) for r in fleet)
    shared = sum(len(git_briefing(r.model)) for r in fleet)
    assert shared / total >= HEAD_SHARE_FLOOR, (
        f"the shared head is {shared / total:.0%} of what the fleet's workers carry, "
        f"under the {HEAD_SHARE_FLOOR:.0%} floor")


@scenario("prefix-drift", "the skills tree handed to a worker is stable across turns")
def test_rebuilding_the_agent_assets_changes_nothing(tmp_path, jarvis_home):
    """`briefing_for` rebuilds this tree on EVERY turn"""
    path = make_git_project(tmp_path, "proj_assets")
    spec = ProjectSpec(name="proj_assets", path=path, description="")
    wo = {"id": "wo-assets", "title": "t", "model": "claude-opus-5"}

    def tree() -> dict[str, bytes]:
        dirs = briefing_for(spec, wo)["add_dirs"]
        return {str(p.relative_to(d)): p.read_bytes()
                for d in dirs for p in sorted(d.rglob("*")) if p.is_file()}

    first = tree()
    assert first, "a worker is pointed at an empty skills tree"
    assert tree() == first


#: A cohort that trips the prefix ceiling, so `check_prefix_stable` renders its message
_FIRING_COHORT = dict(orders=25, cache_write=1_000_000, ttl_write=100_000,
                      prefix_write=500_000, boundaries=100, ttl_boundaries=20)


@scenario("prefix-drift", "the authority this battery defers to still resolves")
def test_the_signal_that_outranks_this_one_still_exists():
    """`AUTHORITY` is the whole precedence rule in one string"""
    from jarvis import invariants

    module, _, name = AUTHORITY.rpartition(".")
    assert module == invariants.__name__.rpartition(".")[2]
    assert callable(getattr(invariants, name, None)), f"{AUTHORITY} no longer exists"


@scenario("prefix-drift", "the authority's violation sends its reader to this battery")
def test_the_violation_a_triaging_reader_sees_points_here(monkeypatch):
    """The OTHER half of the reference"""
    from jarvis import bill, catalog, invariants

    writes = bill.CacheWrites(**_FIRING_COHORT)
    monkeypatch.setattr(invariants, "_cache_health",
                        lambda: (catalog.OsConfig(), writes))

    found = list(invariants.check_prefix_stable())
    assert len(found) == 1, "the synthetic cohort no longer trips the ceiling"
    assert "evals/test_prefix_drift.py" in found[0].detail, (
        "INV-PREFIX-DRIFT no longer points a triaging reader at this battery")
