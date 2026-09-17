"""Has the worker's cacheable prompt head started moving?

`tests/test_stable_prefix.py` checks that the two halves of the prefix fix ship together
— the setting and the briefing it replaces. This battery checks the property they exist
to buy, on the surface dispatch actually renders: the head of the worker's system prompt
is the SAME BYTES for every work order in the fleet and for every turn of each one, and
the only thing after it is the operator's own standing instructions.

WHAT MOVES A PREFIX, AND WHY THIS IS CHEAP INSURANCE. Prefix invalidation is the larger
half of the re-write tax (finding 4 of docs/superpowers/findings/
2026-08-30-where-the-800-dollars-went.md). The causes are ordinary edits, not exotic
faults: a CLI upgrade, an MCP server added mid-session, a line of per-order context
tucked into `worker_brief.git_briefing` because that is where the text was handy, a new
key in `dispatch._write_worker_settings`. Each of those lands in a pull request, and
nothing in the tree failed when it did.

NOT A COMMITTED BYTE SNAPSHOT (Neo question 362). A pinned render of the prompt fails on
every legitimate wording edit, gets refreshed reflexively within a month, and then
detects nothing. What is pinned here is the STRUCTURE — which region each piece of text
lives in — so rewording the briefing is free and moving it is not.

Deterministic: no model call, no opt-in. A drift detector that only runs when somebody
sets `JARVIS_EVALS_LLM=1` is not a detector.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from jarvis import claude_cli
from jarvis.catalog import ProjectSpec
from jarvis.testing import make_git_project
from jarvis.worker_brief import git_briefing
from jarvis.worker_session import briefing_for

scenario = pytest.mark.scenario


# -- the precedence rule ------------------------------------------------------------
#
# THIS BATTERY IS A PROXY AND `invariants.check_prefix_stable` IS THE AUTHORITY.
# Everything below reads a prompt Jarvis rendered and infers that the cache would have
# held; INV-PREFIX-DRIFT reads the cache accounting the API ITSELF reported, through
# `usage._usage_of`'s boundary classification, frozen onto each order's bill. A verdict
# here is a statement about what the prompt LOOKS like, and it can be wrong in both
# directions: green while a CLI-side block nobody modelled moves the real prefix, red on
# a restructuring that costs the cache nothing. It is worth having anyway because it is
# the EARLY one — it fails in CI in the pull request that causes the drift, where
# INV-PREFIX-DRIFT can only report it days later out of transcripts already paid for.
# When the two disagree, INV-PREFIX-DRIFT is right. (kn-fafe92b7, kn-376c88eb.)

AUTHORITY = "invariants.check_prefix_stable"

#: See `test_the_head_is_not_being_hollowed_out`. Loose on purpose.
HEAD_SHARE_FLOOR = 0.5


# -- what gets rendered -------------------------------------------------------------


@dataclass(frozen=True)
class Render:
    """One worker's rendered prefix, and the variable text it was entitled to carry."""

    label: str
    model: str
    prompt: str        # --append-system-prompt, verbatim
    standing: str      # the operator's own instructions for this worker, or ""
    settings: dict


def _fleet(tmp_path, jarvis_home) -> list[Render]:
    """A fleet whose renders differ in every way a real one does.

    The standing instructions are chosen to share no first character, so the longest
    common prefix of the fleet is forced to stop at the end of the shared head. A fixture
    set whose tails happened to agree would make the central assertion vacuous.
    """
    out: list[Render] = []
    cases = [
        ("bare worker", "proj_a", "claude-opus-5", ""),
        ("worker, project instructions", "proj_a", "claude-opus-5",
         "Always run the linter before you commit."),
        ("worker, work-order instructions", "proj_b", "claude-opus-5",
         "Never touch the vendored tree."),
        ("planner", "proj_b", "claude-opus-5",
         "Zero in on the smallest plan that ships."),
        ("other model", "proj_a", "claude-haiku-4-5-20251001",
         "Only the trailer may differ."),
    ]
    paths = {}
    for label, pname, model, standing in cases:
        if pname not in paths:
            paths[pname] = make_git_project(tmp_path, pname)
        spec = ProjectSpec(name=pname, path=paths[pname], description="")
        wo = {"id": f"wo-{label.replace(' ', '')[:8]}", "title": label, "model": model,
              "kind": "planner" if label == "planner" else "worker"}
        # whether the text arrives on the project or on the work order is deliberately
        # mixed: both land in the same region, and a regression that treated them
        # differently would put one of them in the head.
        if "project" in label:
            spec.worker.append_system_prompt = standing
        else:
            wo["append_system_prompt"] = standing
        brief = briefing_for(spec, wo)
        out.append(Render(
            label=label, model=model,
            prompt=brief["append_system_prompt"], standing=standing,
            settings=json.loads(brief["settings_file"].read_text()),
        ))
    return out


@pytest.fixture()
def fleet(tmp_path, jarvis_home):
    return _fleet(tmp_path, jarvis_home)


# -- the detector, as a function so it can be run in both directions ----------------


def _common_prefix(values: list[str]) -> str:
    first, last = min(values), max(values)
    for i, ch in enumerate(first):
        if i >= len(last) or last[i] != ch:
            return first[:i]
    return first


def head_findings(renders: list[Render]) -> list[str]:
    """What moved, and which side it moved to. Empty means the structure held.

    A finding names the region, because the two failures need opposite fixes: text that
    leaked INTO the head must come out of `git_briefing`, and text that drifted OUT of it
    must go back in.
    """
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


# -- the battery --------------------------------------------------------------------


@scenario("prefix-drift", "the fleet's renders differ from each other")
def test_the_fixture_fleet_is_not_secretly_uniform(fleet):
    """kn-45bae078 (1): a battery that asserts a structure goes green when it stops
    rendering anything. Everything below is vacuous without this."""
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
    """The negative control (kn-5a5d47fa (5)). One predicate decides both this and the
    scenario above, so it is exercised in both directions or the green above is
    indistinguishable from a predicate stuck at "no findings"."""
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
    """The prefix is a function of (project, work order, model) and of nothing else.

    `tests/test_stable_prefix.py` perturbs the working tree. A worker turn also runs at a
    different time, from a different directory and under a different environment from the
    turn before it, and each of those is a way for a clock, a `cwd` or a `$PWD`-derived
    path to reach the prompt.
    """
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
    """Jarvis owns only half the prompt. The other half is the CLI's, and these two keys
    are the whole of Jarvis's control over whether it moves between turns: the git-status
    snapshot the briefing replaces, and the cache TTL the writes are billed at."""
    for r in fleet:
        assert r.settings["includeGitInstructions"] is False, r.label
        for key, value in claude_cli.PROMPT_CACHE_5M_ENV.items():
            assert r.settings["env"][key] == value, r.label


@scenario("prefix-drift", "the shared head is the bulk of what every worker carries")
def test_the_head_is_not_being_hollowed_out(fleet):
    """A loose floor, not a pinned size (Neo question 362).

    Rewording the briefing moves this by a few percent and must not fail. What does fail
    is the head being gutted, or per-order text growing until it dominates — either way
    the region the whole fleet shares has shrunk, and a smaller shared region is a
    smaller cache read on every dispatch. The number is empirical: the fleet sits near
    0.9 today, so a breach means something structural happened, not that somebody edited
    a sentence.
    """
    total = sum(len(r.prompt) for r in fleet)
    shared = sum(len(git_briefing(r.model)) for r in fleet)
    assert shared / total >= HEAD_SHARE_FLOOR, (
        f"the shared head is {shared / total:.0%} of what the fleet's workers carry, "
        f"under the {HEAD_SHARE_FLOOR:.0%} floor")


@scenario("prefix-drift", "the skills tree handed to a worker is stable across turns")
def test_rebuilding_the_agent_assets_changes_nothing(tmp_path, jarvis_home):
    """`briefing_for` rebuilds this tree on EVERY turn, and the CLI renders the skills it
    finds there into the system prompt. A generated skill file carrying a timestamp, or a
    set that varied with the work order, would move the prefix from a direction nothing
    else in this battery looks at."""
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


@scenario("prefix-drift", "the precedence over the authoritative measurement is stated")
def test_this_battery_names_the_signal_that_outranks_it():
    """Two prefix-drift signals in one tree with no stated precedence is kn-376c88eb: a
    later reader trusts whichever they found first. So the rule lives in the code that
    computes each verdict — this module's own failure message on one side, the
    invariant's docstring on the other — and both halves are held here, because a
    reference that silently stops resolving is how the rule quietly ceases to exist.
    """
    import inspect

    from jarvis import invariants

    module, _, name = AUTHORITY.rpartition(".")
    assert module == invariants.__name__.rpartition(".")[2]
    check = getattr(invariants, name, None)
    assert callable(check), f"{AUTHORITY} no longer exists"

    source = inspect.getsource(check)
    assert "AUTHORITATIVE MEASUREMENT" in (check.__doc__ or ""), "the authority stopped claiming it"
    # and the other direction: INV-PREFIX-DRIFT's own violation text sends the triaging
    # reader here, because green here with that red rules this tree out and leaves the
    # CLI and the MCP servers — which is the first thing worth knowing.
    assert "evals/test_prefix_drift.py" in source, (
        "the authoritative invariant no longer points at this battery")
