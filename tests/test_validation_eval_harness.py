"""The validation eval must not be able to skip itself into silence.

`evals/llm/test_validation_judgment.py` costs real money, so it is opt-in behind an
environment variable and nothing in CI ever runs it. THAT IS THE TRAP THIS FILE EXISTS FOR:
a typo in the variable name — `JARVIS_EVAL_LLM`, `JARVIS_EVALS_LM` — skips the eval for
ever, the scorecard reports nothing, CI stays green, and validation gets enabled on the
strength of a measurement that never ran. Every assertion here is free and runs on every
`pytest tests/`.

Three more shapes of the same failure are covered:

* **Grading nothing.** A battery that is empty, or whose entries collide on name, or a
  module that quietly calls something other than `validation.decide`, produces a full
  green scorecard having measured no panel at all. The sharpest case is the feature
  battery: a "feature" case whose defect sits inside ONE child is a work-order case with a
  different label, and it would report that feature-level validation works while measuring
  nothing that only the integrated diff can see.
* **Unreachable criteria.** A criterion the fixture can never satisfy — a standing
  instruction the run never seeds, a floor higher than anything the panel has ever scored
  — fails or passes for reasons that have nothing to do with the panel.
* **A measurement that stopped costing anything.** The meter wrapping the model is the one
  place a stub could make the whole eval free, green and meaningless.

The AST is walked rather than the source grepped. `tests/test_eval_harness.py` records a
substring check that stayed green after the argument it was guarding had been deleted,
because the word survived in a docstring explaining it; mutation testing caught it. Prose
cannot fool `ast`.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from jarvis.project_store import VALIDATOR_SEATS
from jarvis.validation import VETO_SEATS

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_validation_judgment.py"
BASELINE_PATH = REPO_ROOT / "evals" / "llm" / "validation_baseline.json"

#: The one spelling. Written out here as a literal so that changing it in the eval without
#: changing it here is a test failure rather than a silent lifetime skip.
ENV_VAR = "JARVIS_EVALS_LLM"

BATTERIES = ("MUST_REJECT", "MUST_PASS", "FEATURE_CASES")

#: How many cases each battery must hold. The eval's thresholds are stated out of these
#: numbers, so a battery that shrank would quietly make its floor easier to clear.
SIZES = {"MUST_REJECT": 4, "MUST_PASS": 3, "FEATURE_CASES": 2}

#: Strings that would mean a case reaches outside the repo. Every submission is INVENTED:
#: a real path is how a synthetic eval turns into one that depends on a machine, and this
#: repository is public. `jarvis` is in the list because the graded submissions are about
#: an invented project — a case naming this OS is a case naming a real catalog project.
PRODUCTION_SHAPES = ("/home/", "/Users/", "~/", "workspace/production", "production/",
                     ".jarvis", "JARVIS_HOME", "/var/", "/etc/", "scripts/shipit.sh",
                     "shipit", "jarvis")

#: The two halves of the integration defect's seam: the name one child introduces, and the
#: name the other child is still calling. Spelled out here because what makes the feature
#: battery a FEATURE battery is that those two names appear in one integrated diff and in
#: neither child's own — see `test_the_feature_battery_has_a_defect_that_spans_two_children`.
OLD_NAME, NEW_NAME = "post_entry", "record_entry"

#: Where an invented diff is allowed to touch. Narrower than "not a production path" and
#: it has to be: the ban list above can only forbid the shapes somebody thought of, while
#: this says what the fixture project is, so anything else is a case that wandered.
ALLOWED_ROOTS = ("src/ledger/", "tests/", "docs/", "README.md")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def eval_module():
    return _load(EVAL_PATH, "_validation_judgment_under_test")


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


def _text_of(case: tuple) -> str:
    """Every string in one case, nested children included."""
    out = []
    for field in case:
        if isinstance(field, str):
            out.append(field)
        else:
            out.extend(str(s) for entry in field for s in entry)
    return "\n".join(out)


def _paths_in(diff: str) -> list[str]:
    return [line[len("+++ b/"):].strip()
            for line in diff.splitlines() if line.startswith("+++ b/")]


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
    """An empty battery makes every assertion over it vacuously true: `>= 4 of 0` fails
    loudly, but a `not leaked` over nothing is a green that measured no panel."""
    cases = getattr(eval_module, battery)
    assert len(cases) == SIZES[battery], (
        f"{battery} holds {len(cases)} cases; the eval's threshold is stated out of "
        f"{SIZES[battery]}")


@pytest.mark.parametrize("battery", BATTERIES)
def test_a_battery_has_unique_case_names(eval_module, battery) -> None:
    """Names are dict keys in the fixture, so a duplicate silently drops a case — and it
    drops it from the DENOMINATOR too, so `3 of 4` becomes `3 of 3` and passes."""
    names = [case[0] for case in getattr(eval_module, battery)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"{battery} reuses case names {dupes}; each one loses a case"


def test_the_batteries_do_not_share_a_name(eval_module) -> None:
    """All three key into one dict together. A collision silently grades one battery's
    submission under another's criterion — and the must-pass and must-reject criteria are
    exact opposites."""
    seen: dict[str, str] = {}
    clashes = []
    for battery in BATTERIES:
        for case in getattr(eval_module, battery):
            if case[0] in seen:
                clashes.append(f"{case[0]} in {seen[case[0]]} and {battery}")
            seen[case[0]] = battery
    assert not clashes, f"the batteries share case names: {clashes}"


@pytest.mark.parametrize("battery", BATTERIES)
def test_a_battery_is_a_module_level_literal(tree, battery) -> None:
    """A reviewer of this PUBLIC repo has to be able to read what is being graded off the
    page — not reconstruct it from a helper, a fixture or a file read at import time."""
    value = _module_assign(tree, battery)
    assert isinstance(value, (ast.List, ast.Tuple)), (
        f"{battery} is a {type(value).__name__}, not a literal list a reviewer can read")

    def literal(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str)
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(literal(e) for e in node.elts)
        # A run of adjacent string literals is one constant after parsing; anything else
        # — a name, a call, an f-string — is a case built rather than written down.
        return False

    for entry in value.elts:
        assert isinstance(entry, ast.Tuple), f"{battery} holds a computed entry"
        assert literal(entry), f"{battery} holds an entry that is not literal strings"


def test_the_feature_battery_has_a_defect_that_spans_two_children(eval_module) -> None:
    """THE ASSERTION THAT MAKES THIS A FEATURE EVAL AT ALL.

    A feature-level panel exists for one defect: two children each correct on their own
    diff and jointly wrong. A "feature" case whose defect sits inside a single child is a
    work-order case wearing a different `unit`, and an eval built out of those would
    report that feature validation works while measuring nothing only the integrated diff
    can see.

    Pinned on the DEFECT CASE ITSELF, and that is the whole subtlety: an earlier version
    of this test asked whether SOME feature case had two children and a wide enough diff,
    and the clean case satisfied it — so the defect case could lose a child and this stayed
    green. Mutation testing is what surfaced that; each assertion below names the case it
    is about.

    What is pinned: every case that is not the clean control has two or more children, and
    its integrated diff both DEFINES the renamed symbol and CALLS the name it replaced.
    Two added lines disagreeing with each other is what "the defect is in the seam" means
    when the seam is code — and neither child's own diff contains both."""
    names = {c[0] for c in eval_module.FEATURE_CASES}
    assert eval_module.CLEAN_FEATURE_CASE in names, (
        "the feature battery has no clean case, so a panel that rejected every feature "
        "diff on sight would score full marks")
    defects = [c for c in eval_module.FEATURE_CASES
               if c[0] != eval_module.CLEAN_FEATURE_CASE]
    assert defects, "the feature battery grades no defect at all"

    for case in defects:
        name, diff, children = case[0], case[5], case[6]
        assert len(children) >= 2, (
            f"FEATURE_CASES[{name}] has {len(children)} child; a defect one child could "
            "have seen on its own diff is a work-order case with a different label")
        added = [line[1:] for line in diff.splitlines()
                 if line.startswith("+") and not line.startswith("+++")]
        defines = [line for line in added if f"def {NEW_NAME}" in line]
        stale = [line for line in added if f"{OLD_NAME}(" in line]
        assert defines and stale, (
            f"FEATURE_CASES[{name}]'s diff no longer both defines {NEW_NAME!r} and calls "
            f"{OLD_NAME!r}, so nothing in it shows the two children disagreeing: "
            f"defines={defines}, stale={stale}")
        assert any(NEW_NAME in "\n".join(child) for child in children), (
            f"no child of {name} claims the rename, so the stale caller reads as one "
            "child's own bug rather than as an integration defect")


@pytest.mark.parametrize("battery", BATTERIES)
def test_no_case_names_a_production_path(eval_module, battery) -> None:
    """Every submission is INVENTED. The corpus of real validations is deferred precisely
    because publishing production state is a user decision, and a real path — or a real
    project name — creeping into a synthetic battery is how that decision gets made by
    accident."""
    for case in getattr(eval_module, battery):
        blob = _text_of(case).lower()
        found = [s for s in PRODUCTION_SHAPES if s.lower() in blob]
        assert not found, f"{battery}[{case[0]}] names {found}"
        for path in _paths_in(case[5]):
            assert not path.startswith("/"), (
                f"{battery}[{case[0]}] patches an absolute path: {path!r}")
            assert path.startswith(ALLOWED_ROOTS), (
                f"{battery}[{case[0]}] patches {path!r}, which is outside the invented "
                f"project ({ALLOWED_ROOTS})")


# -- what actually gets called --------------------------------------------------------------


def test_the_eval_calls_validation_decide(tree) -> None:
    """The subject. An eval that reached the seats any other way would still return
    outcomes, still fill a scorecard, and grade something that is not the entry point the
    round machine calls.

    AST, not a substring: this module's docstring says `validation.decide` several times
    over, and so does the eval's."""
    decides = [c for c in _calls(tree, "decide")
               if isinstance(c.func, ast.Attribute)
               and isinstance(c.func.value, ast.Name) and c.func.value.id == "validation"]
    assert decides, "the eval never calls validation.decide(...) — it grades something else"


def test_the_eval_does_not_assemble_the_seats_itself(tree) -> None:
    """`decide` is more than a fan-out: it builds every prompt on one thread, records each
    opinion, applies the veto table, and only then runs the chair. An eval that called
    `run_blind` and `arbitrate` itself would grade the seats' prose while leaving the order
    of those steps — where the safety rule lives — completely unmeasured."""
    for forbidden in ("run_blind", "arbitrate", "_run_chair", "build_seat_system_prompt",
                      "build_chair_prompt"):
        assert not _calls(tree, forbidden), (
            f"the eval calls {forbidden} itself, so it is assembling the panel rather "
            "than grading validation.decide")


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
    decision to enable validation is made from."""
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


def test_the_degraded_seat_is_a_veto_holder_and_not_the_chair(eval_module) -> None:
    """Degradation only means something if the seat that went down could have changed the
    outcome. `architect` and `maintainer` force nothing however they reply, so taking one
    down grades a panel that was never going to notice; and an unreachable chair is TOTAL
    failure by design — `_run_chair` re-raises `ClaudeCliError` so the round machine can
    retry without the submitter paying for it — not degradation at all."""
    seat = eval_module.DEGRADED_SEAT
    assert seat in VALIDATOR_SEATS, f"{seat!r} is not a seat in the OS's vocabulary"
    assert seat in VETO_SEATS, (
        f"{seat!r} holds no veto, so taking it down cannot change any outcome and the "
        f"scenario measures nothing (veto seats: {VETO_SEATS})")
    assert seat != "chair", (
        "an unreachable chair is total failure by design, not degradation")


def test_the_degraded_case_is_one_of_the_defective_submissions(eval_module) -> None:
    """The degradation scenario also carries a hard limit — the panel must not PASS with a
    veto seat down — and that assertion is vacuous unless the submission it judges is one
    that should be refused."""
    assert eval_module.DEGRADED_CASE in {c[0] for c in eval_module.MUST_REJECT}, (
        f"{eval_module.DEGRADED_CASE!r} is not in MUST_REJECT, so 'a seat down does not "
        "pass the submission' is graded on work that was allowed to pass anyway")


def test_the_standing_instruction_the_todo_case_depends_on_is_seeded(
        eval_module, tree) -> None:
    """The reachability check, and it is the one that matters most here.

    `todo-instead-of-backlog` ships a tested, non-vacuous change whose only defect is a
    deferral comment. That is a defect ONLY because the project's standing instructions
    say so, and the seats read those out of the knowledge base the fixture seeds. If the
    seeding goes, no panel can ever reject that case and the battery fails for ever for a
    reason that is not the panel's."""
    assert "TODO" in eval_module.TODO_INSTRUCTION, (
        "the instruction does not mention the thing the case does, so no seat can connect "
        "the two")
    assert "backlog" in eval_module.TODO_INSTRUCTION.lower()
    case = next(c for c in eval_module.MUST_REJECT if c[0] == "todo-instead-of-backlog")
    assert "TODO" in case[5], "the case's diff no longer carries a TODO"

    seeded = [c for c in _calls(tree, "add_knowledge")
              if any(isinstance(a, ast.Name) and a.id == "TODO_INSTRUCTION"
                     for a in c.args)]
    assert seeded, (
        "the fixture never seeds TODO_INSTRUCTION into the knowledge base, so the seats "
        "are judged against a rule they were never shown")


def test_the_thresholds_are_reachable_and_not_vacuous(eval_module) -> None:
    """A floor of 0 grades nothing and a floor above `n` can never be met. Both are ways
    for a paid scorecard to say something that is not about the panel."""
    for name, floor, battery in (("MUST_REJECT_FLOOR", eval_module.MUST_REJECT_FLOOR,
                                  eval_module.MUST_REJECT),
                                 ("MUST_PASS_FLOOR", eval_module.MUST_PASS_FLOOR,
                                  eval_module.MUST_PASS)):
        assert 1 <= floor <= len(battery), (
            f"{name}={floor} is outside 1..{len(battery)}: it is either unreachable or "
            "vacuous")


# -- the baseline: the record that makes the next recalibration free -------------------------


def test_the_baseline_records_the_run_the_thresholds_came_from() -> None:
    """A threshold with no measurement behind it is a guess, and a guess is what this eval
    exists to replace. The paid run writes this file; it is checked in so the next worker
    can move a floor — or see which seat moved — without spending the money again."""
    assert BASELINE_PATH.is_file(), (
        f"{BASELINE_PATH.name} is missing: the thresholds in the eval have no measured "
        "run behind them")
    data = json.loads(BASELINE_PATH.read_text())
    assert data.get("generated"), "the baseline does not say when it was measured"
    assert data.get("model"), "the baseline does not say which model was asked"


def test_the_baseline_matches_the_batteries_it_measured(eval_module) -> None:
    """`n` is stored with every score because a score means nothing without it. A battery
    that grew or shrank since the run is a battery whose floor was calibrated against a
    different denominator — and a case added since is a case no measurement has ever seen.

    Regenerate with a paid run, or update the file by hand and say in the PR that you
    did: it is a record, not a lock."""
    data = json.loads(BASELINE_PATH.read_text())
    for battery in BATTERIES:
        recorded = data["thresholds"][battery]
        assert recorded["n"] == len(getattr(eval_module, battery)), (
            f"{battery} now holds {len(getattr(eval_module, battery))} cases and the "
            f"baseline was measured over {recorded['n']}")
    measured = set(data["runs"])
    cases = {case[0] for battery in BATTERIES
             for case in getattr(eval_module, battery)} | {"degraded"}
    assert cases - measured == set(), f"never measured: {sorted(cases - measured)}"
    assert measured - cases == set(), f"measured but gone: {sorted(measured - cases)}"


def test_no_threshold_is_higher_than_what_was_measured(eval_module) -> None:
    """A floor above the score the panel actually achieved is a test that fails the first
    time somebody pays for it — the most expensive possible way to learn that a number
    was optimistic."""
    data = json.loads(BASELINE_PATH.read_text())
    for battery, floor in (("MUST_REJECT", eval_module.MUST_REJECT_FLOOR),
                           ("MUST_PASS", eval_module.MUST_PASS_FLOOR)):
        recorded = data["thresholds"][battery]
        assert floor == recorded["floor"], (
            f"{battery}'s floor is {floor} in the eval and {recorded['floor']} in the "
            "baseline; one of them was changed without the other")
        assert floor <= recorded["scored"], (
            f"{battery}'s floor is {floor} and the measured run scored "
            f"{recorded['scored']}/{recorded['n']}")


def test_the_baseline_records_what_each_seat_said() -> None:
    """Per-seat verdicts, not just an outcome. A battery that starts failing is a
    completely different investigation depending on whether the veto seats stopped
    blocking or the chair started passing, and without the rows nobody can tell which
    without paying for another run."""
    data = json.loads(BASELINE_PATH.read_text())
    for name, run in data["runs"].items():
        assert run["seats"], f"{name} recorded no seat opinions at all"
        for row in run["seats"]:
            assert row["seat"] in VALIDATOR_SEATS, f"{name}: unknown seat {row['seat']!r}"
            assert set(row) >= {"seat", "status", "verdict", "blocking", "reply"}, (
                f"{name}/{row['seat']} lost a field: {sorted(row)}")


# -- the one thing that must never be graded -------------------------------------------------


def test_no_cost_or_latency_number_is_asserted(tree) -> None:
    """There is no baseline for cost in this repo, and the design calls the cost claim a
    claim to measure, not to assert. The numbers are printed for a human; an assertion on
    them is a test that spends real money to be flaky.

    Walked as an AST over every assert in every test, so the ban is enforced rather than
    remembered."""
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assert):
                continue
            src = ast.dump(node.test)
            for forbidden in ("'seconds'", "'calls'", "'diff_chars'"):
                assert forbidden not in src, (
                    f"{fn.name} asserts on {forbidden} — cost, latency and diff size are "
                    "reported, never graded")
