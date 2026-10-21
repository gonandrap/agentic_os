"""The validation eval must not be able to skip itself into silence"""

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

#: The one spelling
ENV_VAR = "JARVIS_EVALS_LLM"

BATTERIES = ("MUST_REJECT", "MUST_PASS", "MUST_PASS_WITH_FOLLOW_UPS",
             "MUST_REJECT_WITH_FOLLOW_UPS", "FEATURE_CASES")

#: How many cases each battery must hold
SIZES = {"MUST_REJECT": 4, "MUST_PASS": 3, "MUST_PASS_WITH_FOLLOW_UPS": 3,
         "MUST_REJECT_WITH_FOLLOW_UPS": 2, "FEATURE_CASES": 2}

#: Every battery's floor, by the name of the constant that holds it
FLOORS = {"MUST_REJECT": "MUST_REJECT_FLOOR", "MUST_PASS": "MUST_PASS_FLOOR",
          "MUST_PASS_WITH_FOLLOW_UPS": "MUST_PASS_WITH_FOLLOW_UPS_FLOOR",
          "MUST_REJECT_WITH_FOLLOW_UPS": "MUST_REJECT_WITH_FOLLOW_UPS_FLOOR"}

#: The floors and the cases the 2026-09-15 feature may not have bought itself (docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md)
PINNED_FLOORS = {"MUST_REJECT_FLOOR": 4, "MUST_PASS_FLOOR": 3}
PINNED_MUST_REJECT = ("untested-new-function", "todo-instead-of-backlog",
                      "evidence-contradicts-diff", "raw-sql-where-a-store-method-exists")

#: How many added lines a "small change" may carry
SMALL_CHANGE_ADDED_LINES = 130

#: Strings that would mean a case reaches outside the repo
PRODUCTION_SHAPES = ("/home/", "/Users/", "~/", "workspace/production", "production/",
                     ".jarvis", "JARVIS_HOME", "/var/", "/etc/", "scripts/shipit.sh",
                     "shipit", "jarvis")

#: The two halves of the integration defect's seam: the name one child introduces
OLD_NAME, NEW_NAME = "post_entry", "record_entry"

#: Where an invented diff is allowed to touch
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
    """The module-level assignment of `name`, or a failure saying it is not one"""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} is not assigned at module level in {EVAL_PATH.name}")


def _calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == attr]


def _text_of(case: tuple) -> str:
    """Every string in one case, nested children included"""
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


# -- the skip gate


def test_the_skip_gate_reads_exactly_the_documented_env_var(tree) -> None:
    """A typo here skips the eval for the lifetime of the repo and reports nothing"""
    mark = _module_assign(tree, "pytestmark")
    names = [c.args[0].value for c in _calls(mark, "get")
             if getattr(getattr(c.func, "value", None), "attr", None) == "environ"
             and c.args and isinstance(c.args[0], ast.Constant)]
    assert names == [ENV_VAR], (
        f"the eval's skip gate reads {names} — it must read exactly ['{ENV_VAR}']. "
        "Any other spelling is an eval that never runs and never says so.")


def test_the_skip_gate_says_how_to_turn_it_on(eval_module) -> None:
    """The reason string is the only place a human is told the eval exists"""
    reasons = [m.kwargs.get("reason", "") for m in eval_module.pytestmark]
    assert any(ENV_VAR in r for r in reasons), (
        f"no skip reason names {ENV_VAR}, so a skipped run tells nobody how to run it")


# -- the batteries


@pytest.mark.parametrize("battery", BATTERIES)
def test_a_battery_is_not_empty(eval_module, battery) -> None:
    """An empty battery makes every assertion over it vacuously true: `>= 4 of 0` fails loudly"""
    cases = getattr(eval_module, battery)
    assert len(cases) == SIZES[battery], (
        f"{battery} holds {len(cases)} cases; the eval's threshold is stated out of "
        f"{SIZES[battery]}")


@pytest.mark.parametrize("battery", BATTERIES)
def test_a_battery_has_unique_case_names(eval_module, battery) -> None:
    """Names are dict keys in the fixture, so a duplicate silently drops a case"""
    names = [case[0] for case in getattr(eval_module, battery)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"{battery} reuses case names {dupes}; each one loses a case"


def test_the_batteries_do_not_share_a_name(eval_module) -> None:
    """All three key into one dict together"""
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
    """A reviewer of this PUBLIC repo has to be able to read what is being graded off the page"""
    value = _module_assign(tree, battery)
    assert isinstance(value, (ast.List, ast.Tuple)), (
        f"{battery} is a {type(value).__name__}, not a literal list a reviewer can read")

    def literal(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str)
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(literal(e) for e in node.elts)
        # A run of adjacent string literals is one constant after parsing; anything else
        return False

    for entry in value.elts:
        assert isinstance(entry, ast.Tuple), f"{battery} holds a computed entry"
        assert literal(entry), f"{battery} holds an entry that is not literal strings"


def test_the_feature_battery_has_a_defect_that_spans_two_children(eval_module) -> None:
    """THE ASSERTION THAT MAKES THIS A FEATURE EVAL AT ALL"""
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
    """Every submission is INVENTED"""
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


# -- what actually gets called


def test_the_eval_calls_validation_decide(tree) -> None:
    """The subject"""
    decides = [c for c in _calls(tree, "decide")
               if isinstance(c.func, ast.Attribute)
               and isinstance(c.func.value, ast.Name) and c.func.value.id == "validation"]
    assert decides, "the eval never calls validation.decide(...) — it grades something else"


def test_the_eval_does_not_assemble_the_seats_itself(tree) -> None:
    """`decide` is more than a fan-out: it builds every prompt on one thread"""
    for forbidden in ("run_blind", "arbitrate", "_run_chair", "build_shared_prefix",
                      "build_seat_prompt", "build_chair_prompt"):
        assert not _calls(tree, forbidden), (
            f"the eval calls {forbidden} itself, so it is assembling the panel rather "
            "than grading validation.decide")


def test_the_meter_wrapping_the_model_still_calls_the_model(tree) -> None:
    """The eval wraps `claude_cli.run_headless_result` to count calls and take a seat down"""
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
    """`evals/conftest.py` builds the scorecard from the `scenario` marker alone"""
    unmarked = []
    for name in dir(eval_module):
        fn = getattr(eval_module, name)
        if not (name.startswith("test_") and callable(fn)):
            continue
        marks = getattr(fn, "pytestmark", [])
        if not any(m.name == "scenario" for m in marks):
            unmarked.append(name)
    assert not unmarked, f"these tests never reach the scorecard: {unmarked}"


# -- criteria the fixture has to be able to satisfy


def test_the_degraded_seat_is_a_veto_holder_and_not_the_chair(eval_module) -> None:
    """Degradation only means something if the seat that went down could have changed the outcome"""
    seat = eval_module.DEGRADED_SEAT
    assert seat in VALIDATOR_SEATS, f"{seat!r} is not a seat in the OS's vocabulary"
    assert seat in VETO_SEATS, (
        f"{seat!r} holds no veto, so taking it down cannot change any outcome and the "
        f"scenario measures nothing (veto seats: {VETO_SEATS})")
    assert seat != "chair", (
        "an unreachable chair is total failure by design, not degradation")


def test_the_degraded_case_is_one_of_the_defective_submissions(eval_module) -> None:
    """The degradation scenario also carries a hard limit"""
    assert eval_module.DEGRADED_CASE in {c[0] for c in eval_module.MUST_REJECT}, (
        f"{eval_module.DEGRADED_CASE!r} is not in MUST_REJECT, so 'a seat down does not "
        "pass the submission' is graded on work that was allowed to pass anyway")


def test_the_standing_instruction_the_todo_case_depends_on_is_seeded(
        eval_module, tree) -> None:
    """The reachability check, and it is the one that matters most here"""
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
    """A floor of 0 grades nothing and a floor above `n` can never be met"""
    for battery, const in FLOORS.items():
        floor = getattr(eval_module, const)
        n = len(getattr(eval_module, battery))
        assert 1 <= floor <= n, (
            f"{const}={floor} is outside 1..{n}: it is either unreachable or vacuous")


def test_the_floors_and_cases_the_feature_may_not_buy_itself_are_unchanged(
        eval_module) -> None:
    """A panel that blocks on nothing scores full marks on every battery added for the 2026-09-15 feature"""
    for const, expected in PINNED_FLOORS.items():
        assert getattr(eval_module, const) == expected, (
            f"{const} is {getattr(eval_module, const)} and must be {expected}: a floor "
            "lowered to fit a run is the measurement being written by the thing it "
            "measures")
    assert tuple(c[0] for c in eval_module.MUST_REJECT) == PINNED_MUST_REJECT, (
        "MUST_REJECT's cases moved. Every one of them is a defect nobody disputes, and "
        "dropping one is the same exit as lowering the floor")

    # And the two batteries the feature added carry NO headroom at all, by construction
    for battery in ("MUST_PASS_WITH_FOLLOW_UPS", "MUST_REJECT_WITH_FOLLOW_UPS"):
        floor, n = getattr(eval_module, FLOORS[battery]), len(getattr(eval_module,
                                                                     battery))
        assert floor == n, (
            f"{FLOORS[battery]} is {floor} of {n}. This battery is the feature's own "
            "claim and it does not get to be true of most of its cases")


def test_a_small_correct_submission_is_actually_small(eval_module) -> None:
    """The new battery's own premise, and nothing else checks it"""
    for case in eval_module.MUST_PASS_WITH_FOLLOW_UPS:
        added = [ln for ln in case[5].splitlines()
                 if ln.startswith("+") and not ln.startswith("+++")]
        assert len(added) <= SMALL_CHANGE_ADDED_LINES, (
            f"MUST_PASS_WITH_FOLLOW_UPS[{case[0]}] adds {len(added)} lines; this battery "
            f"grades a SMALL correct change and {SMALL_CHANGE_ADDED_LINES} is where that "
            "stops being the claim")


def test_the_veto_case_is_one_of_the_defective_submissions(eval_module) -> None:
    """`test_a_veto_rejects_and_the_chair_is_never_asked` grades one NAMED case"""
    assert eval_module.VETO_CASE in {c[0] for c in eval_module.MUST_REJECT}, (
        f"{eval_module.VETO_CASE!r} is not in MUST_REJECT, so 'a veto seat blocked' is "
        "graded on work no seat had reason to block")


# -- the baseline: the record that makes the next recalibration free


def test_the_baseline_records_the_run_the_thresholds_came_from() -> None:
    """A threshold with no measurement behind it is a guess"""
    assert BASELINE_PATH.is_file(), (
        f"{BASELINE_PATH.name} is missing: the thresholds in the eval have no measured "
        "run behind them")
    data = json.loads(BASELINE_PATH.read_text())
    assert data.get("generated"), "the baseline does not say when it was measured"
    assert data.get("model"), "the baseline does not say which model was asked"


def test_the_baseline_matches_the_batteries_it_measured(eval_module) -> None:
    """`n` is stored with every score because a score means nothing without it"""
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
    """A floor above the score the panel actually achieved is a test that fails the first time somebody..."""
    data = json.loads(BASELINE_PATH.read_text())
    for battery, const in FLOORS.items():
        floor = getattr(eval_module, const)
        recorded = data["thresholds"][battery]
        assert floor == recorded["floor"], (
            f"{battery}'s floor is {floor} in the eval and {recorded['floor']} in the "
            "baseline; one of them was changed without the other")
        assert floor <= recorded["scored"], (
            f"{battery}'s floor is {floor} and the measured run scored "
            f"{recorded['scored']}/{recorded['n']}")


def test_the_baseline_records_what_each_seat_said() -> None:
    """Per-seat verdicts, not just an outcome"""
    data = json.loads(BASELINE_PATH.read_text())
    for name, run in data["runs"].items():
        assert run["seats"], f"{name} recorded no seat opinions at all"
        # The severity split is the thing the 2026-09-15 feature turns on
        assert "severities" in run and "filed" in run, (
            f"{name} records no severity reading: {sorted(run)}")
        for row in run["seats"]:
            assert row["seat"] in VALIDATOR_SEATS, f"{name}: unknown seat {row['seat']!r}"
            assert set(row) >= {"seat", "status", "verdict", "blocking", "reply"}, (
                f"{name}/{row['seat']} lost a field: {sorted(row)}")


# -- the one thing that must never be graded


def test_no_cost_or_latency_number_is_asserted(tree) -> None:
    """There is no baseline for cost in this repo"""
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
