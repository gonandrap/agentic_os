"""The panel eval must not be able to skip itself into silence.

`evals/llm/test_neo_panel_judgment.py` costs real money, so it is opt-in behind an
environment variable and nothing in CI ever runs it. THAT IS THE TRAP THIS FILE EXISTS FOR:
a typo in the variable name — `JARVIS_EVAL_LLM`, `JARVIS_EVALS_LM` — skips the eval for
ever, the scorecard reports nothing, CI stays green, and the panel gets enabled on the
strength of a measurement that never ran. Every assertion here is free and runs on every
`pytest tests/`.

Two more shapes of the same failure are covered:

* **Grading nothing.** A battery that is empty, or whose entries collide on name, or a
  module that quietly calls something other than `panel.decide`, produces a full green
  scorecard having measured no panel at all.
* **Unreachable criteria.** A criterion the fixture can never satisfy — a mandated
  sentence that is not in the learning that mandates it, a word budget stricter than the
  prompt being graded, a "degraded" seat whose outage is really the documented fallback —
  fails or passes for reasons that have nothing to do with the panel.

The AST is walked rather than the source grepped. `tests/test_eval_harness.py` records a
substring check that stayed green after the argument it was guarding had been deleted,
because the word survived in a docstring explaining it; mutation testing caught it. Prose
cannot fool `ast`.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from jarvis.bootstrap import ASSETS
from jarvis.neo_store import SEATS

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_neo_panel_judgment.py"
SINGLE_AGENT_EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_gate_review_judgment.py"

#: The one spelling. Written out here as a literal so that changing it in the eval without
#: changing it here is a test failure rather than a silent lifetime skip.
ENV_VAR = "JARVIS_EVALS_LLM"

BATTERIES = ("MUST_DISMISS", "MUST_NOT_DISMISS")

#: Strings that would mean the battery reaches outside the repo. The eval is graded on
#: INVENTED commands: a real path is how a synthetic eval turns into one that depends on a
#: machine, and this repository is public.
PRODUCTION_SHAPES = ("/home/", "/Users/", "~/", "workspace/production", "production/jarvis",
                     ".jarvis/", "JARVIS_HOME", "/var/", "/etc/")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def eval_module():
    return _load(EVAL_PATH, "_neo_panel_judgment_under_test")


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(EVAL_PATH.read_text())


def _module_assign(tree: ast.Module, name: str) -> ast.expr:
    """The module-level assignment of `name`, or a failure saying it is not one.

    "Module-level" is the assertion, not an implementation detail: a battery built inside
    a function or from a file read at import time is one a reviewer of this public repo
    cannot read off the page.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} is not assigned at module level in {EVAL_PATH.name}")


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == attr]


def test_the_eval_file_exists() -> None:
    assert EVAL_PATH.is_file(), f"{EVAL_PATH} — the eval this file guards is missing"


# -- the skip gate ----------------------------------------------------------------------


def test_the_skip_gate_reads_exactly_the_documented_env_var(tree) -> None:
    """A typo here skips the eval for the lifetime of the repo and reports nothing.

    Read off the AST of the `pytestmark` assignment. Importing the module cannot answer
    this: by then `skipif`'s condition has already collapsed to a bool and the name that
    produced it is gone.
    """
    mark = _module_assign(tree, "pytestmark")
    names = [c.args[0].value for c in _calls(mark, "get")
             if getattr(getattr(c.func, "value", None), "attr", None) == "environ"
             and c.args and isinstance(c.args[0], ast.Constant)]
    assert names == [ENV_VAR], (
        f"the eval's skip gate reads {names} — it must read exactly ['{ENV_VAR}']. "
        "Any other spelling is an eval that never runs and never says so.")


def test_the_skip_gate_says_how_to_turn_it_on(eval_module) -> None:
    """The reason string is the only place a human is told the eval exists."""
    reasons = [m.kwargs.get("reason", "") for m in eval_module.pytestmark]
    assert any(ENV_VAR in r for r in reasons), (
        f"no skip reason names {ENV_VAR}, so a skipped run tells nobody how to run it")


# -- the batteries ----------------------------------------------------------------------


@pytest.mark.parametrize("battery", BATTERIES)
def test_a_battery_is_not_empty(eval_module, battery) -> None:
    """An empty battery makes every assertion over it vacuously true: `>= 5 of 0` fails
    loudly, but `not leaked` over nothing is a green that measured no panel."""
    cases = getattr(eval_module, battery)
    assert len(cases) == 6, f"{battery} has {len(cases)} cases; the eval grades out of 6"


@pytest.mark.parametrize("battery", BATTERIES)
def test_a_battery_has_unique_case_names(eval_module, battery) -> None:
    """Names are dict keys in the fixture, so a duplicate silently drops a case — and it
    drops it from the DENOMINATOR too, so `5 of 6` becomes `5 of 5` and passes."""
    names = [name for name, _, _ in getattr(eval_module, battery)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"{battery} reuses case names {dupes}; each one loses a case"


def test_the_two_batteries_do_not_share_a_name(eval_module) -> None:
    """They key into one dict together. A collision across them silently grades one
    battery's command under the other's criterion."""
    dismiss = {n for n, _, _ in eval_module.MUST_DISMISS}
    keep = {n for n, _, _ in eval_module.MUST_NOT_DISMISS}
    assert not dismiss & keep, f"the batteries share case names: {sorted(dismiss & keep)}"


@pytest.mark.parametrize("battery", BATTERIES)
def test_a_battery_is_a_module_level_literal(tree, battery) -> None:
    """A reviewer of this PUBLIC repo has to be able to read what is being graded off the
    page — not reconstruct it from a helper, a fixture or a file read at import time."""
    value = _module_assign(tree, battery)
    assert isinstance(value, (ast.List, ast.Tuple)), (
        f"{battery} is a {type(value).__name__}, not a literal list a reviewer can read")
    for entry in value.elts:
        assert isinstance(entry, ast.Tuple), f"{battery} holds a computed entry"
        assert all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                   for e in entry.elts), f"{battery} holds a non-literal string"


@pytest.mark.parametrize("battery", BATTERIES)
def test_no_battery_command_names_a_production_path(eval_module, battery) -> None:
    """Every command is INVENTED. The corpus of real decisions is deferred to bl-cc5df0bd
    precisely because publishing production state is a user decision, and a real path
    creeping into a synthetic battery is how that decision gets made by accident."""
    for name, command, why in getattr(eval_module, battery):
        blob = f"{command} {why}"
        found = [s for s in PRODUCTION_SHAPES if s in blob]
        assert not found, f"{battery}[{name}] names {found}: {command!r}"
        assert not command.startswith("/"), (
            f"{battery}[{name}] is an absolute path: {command!r}")


def test_the_batteries_match_the_single_agent_eval(eval_module) -> None:
    """The panel's whole claim is that it beats the single agent, and a comparison of two
    scorecards is only a comparison if both graded the same inputs.

    `evals/llm/test_gate_review_judgment.py` measures `neo.answer_question` on these exact
    twelve commands. Adding a case to one file and not the other leaves two scorecards that
    still look comparable and are not."""
    single = _load(SINGLE_AGENT_EVAL_PATH, "_gate_review_judgment_reference")
    for battery in BATTERIES:
        ours = {(n, c) for n, c, _ in getattr(eval_module, battery)}
        theirs = {(n, c) for n, c, _ in getattr(single, battery)}
        assert ours == theirs, (
            f"{battery} has drifted from {SINGLE_AGENT_EVAL_PATH.name}: "
            f"only here {sorted(ours - theirs)}, only there {sorted(theirs - ours)}. "
            "Add the case to both, or the panel's scorecard stops being comparable to "
            "the single agent's.")


# -- what actually gets called --------------------------------------------------------------


def test_the_eval_calls_panel_decide(tree) -> None:
    """The subject. An eval that reached Neo any other way would still return verdicts,
    still fill a scorecard, and grade the single agent this feature is replacing.

    AST, not a substring: this module's docstring says `panel.decide` several times over,
    and so does the eval's."""
    decides = [c for c in _calls(tree, "decide")
               if isinstance(c.func, ast.Attribute)
               and isinstance(c.func.value, ast.Name) and c.func.value.id == "panel"]
    assert decides, "the eval never calls panel.decide(...) — it is grading something else"


def test_the_eval_does_not_reach_the_single_agent_directly(tree) -> None:
    """`decide` falls back to `neo.answer_question` when the premise seat is silent, and
    that is the panel's own behaviour. The EVAL calling it would be the panel's subject
    quietly swapped for its predecessor."""
    assert not _calls(tree, "answer_question"), (
        "the eval calls neo.answer_question itself; the fallback belongs to panel.decide")


def test_the_meter_wrapping_the_model_still_calls_the_model(tree) -> None:
    """The eval wraps `claude_cli.run_headless_result` to count calls and take a seat down.
    A wrapper that stopped delegating would turn every paid assertion into a measurement
    of the wrapper — the most expensive way possible to test nothing."""
    meter = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.ClassDef) and n.name == "Meter"), None)
    assert meter is not None, "the Meter that wraps run_headless_result is gone"
    call = next((n for n in meter.body
                 if isinstance(n, ast.FunctionDef) and n.name == "__call__"), None)
    assert call is not None, "Meter no longer implements __call__"
    delegates = [c for c in ast.walk(call)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                 and c.func.attr == "_real"]
    assert delegates, "Meter.__call__ never delegates to the real run_headless_result"


def test_every_test_carries_a_scenario_marker(eval_module) -> None:
    """`evals/conftest.py` builds the scorecard from the `scenario` marker alone. An
    unmarked test still runs and still costs money, and then vanishes from the report the
    decision to enable the panel is made from."""
    unmarked = []
    for name in dir(eval_module):
        fn = getattr(eval_module, name)
        if not (name.startswith("test_") and callable(fn)):
            continue
        marks = getattr(fn, "pytestmark", [])
        if not any(m.name == "scenario" for m in marks):
            unmarked.append(name)
    assert not unmarked, f"these tests never reach the scorecard: {unmarked}"


# -- criteria the fixture has to be able to satisfy -------------------------------------------


def test_the_mandated_sentence_is_inside_the_learning_that_mandates_it(eval_module) -> None:
    """The reachability check, and it is the one that matters most here.

    The brevity assertion subtracts `VERBATIM_SENTENCE` from the chair's answer before
    counting words, and a companion assertion requires the chair to have quoted it. If the
    learning seeded into the store does not actually demand that exact sentence, no chair
    can ever produce it and the pair fails for ever for a reason that is not the panel's.
    """
    assert eval_module.VERBATIM_SENTENCE in eval_module.VERBATIM_LEARNING, (
        "the learning does not quote the sentence the eval requires verbatim, so the "
        "exemption it grades is unreachable")


def test_the_degraded_seat_is_a_real_seat_and_not_the_fallback_path(eval_module) -> None:
    """`premise` is the seat that routes. `panel.decide` treats its silence as a reason to
    fall back to the single agent entirely — documented behaviour with its own free test —
    so a degradation scenario that took `premise` down would grade the fallback and report
    it as the panel surviving."""
    seat = eval_module.DEGRADED_SEAT
    assert seat in SEATS, f"{seat!r} is not a seat in the OS's vocabulary"
    assert seat not in ("premise", "chair"), (
        f"taking {seat!r} down is not degradation: premise silence is the single-agent "
        "fallback, and an unreachable chair is total failure by design")


def test_the_answer_budget_is_not_stricter_than_the_mandate_it_grades(eval_module) -> None:
    """The chair's own markdown caps an override at 50 words of explanation and exempts
    the decision itself. Grading below that would fail chairs that obeyed their prompt.

    Read off the SHIPPED markdown rather than a constant: the file the runtime hands the
    model is the thing the eval is measuring compliance with."""
    mandate = (ASSETS / "neo-seats" / "chair.md").read_text()
    assert "50 WORDS" in mandate, (
        "the chair mandate no longer states a 50-word budget; the eval's "
        f"ANSWER_BUDGET_WORDS={eval_module.ANSWER_BUDGET_WORDS} is now grading nothing "
        "the prompt asks for")
    assert eval_module.ANSWER_BUDGET_WORDS >= 50
    assert eval_module.REASON_BUDGET_WORDS >= 1


def test_the_full_roster_is_the_whole_vocabulary(eval_module) -> None:
    """The eval must grade the panel the design describes, not the two-seat roster that
    shipped first. `catalog.DEFAULT_ROSTER` is still `("premise", "chair")` — an eval that
    used the default would report on a panel with no record, blast or taste seat."""
    assert set(eval_module.FULL_ROSTER) == set(SEATS), (
        f"the eval's roster {eval_module.FULL_ROSTER} is not the seat vocabulary {SEATS}")


def test_no_cost_or_latency_number_is_asserted(tree) -> None:
    """The design calls the cost claim "a claim to measure, not to assert", and there is no
    baseline in this repo. The numbers are printed for a human; an assertion on them is a
    test that spends real money to be flaky."""
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            src = ast.dump(node.test)
            for forbidden in ("'seconds'", "'calls'"):
                assert forbidden not in src, (
                    f"{fn.name} asserts on {forbidden} — cost and latency are reported, "
                    "never graded")
