"""A turn is one-shot, and the record says so when a worker forgets.

wo-2df8828c backgrounded a 15-minute paid eval, ended its turn on "I'll be re-invoked
when it finishes", and landed in `needs_review`. Nothing re-invoked it and nothing
survived — a turn is one `claude -p` process. The user read the status as a mislabelled
`running`, which is the second half of the bug: the flag said the turn had ended without
saying what that cost.

Two halves ship together here, and either alone leaves the fleet wrong:

  * the CONTRACT that stops a worker believing in a wake-up (`worker_brief` for the
    dispatched prompt, OPERATION.md for the worker that goes looking), and
  * the ATTENTION LINE that tells the user what actually happened when one does —
    one constant, so the flag and its re-derivation cannot tell two stories.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.bootstrap import TEMPLATE_VERSION, bootstrap_project
from jarvis.catalog import ProjectSpec
from jarvis.invariants import IDLE_NO_FINISH_BLOCKER, true_blockers
from jarvis.project_store import ProjectStore


def spec(path, **kw):
    return ProjectSpec(name="proj_a", path=path, **kw)


def _operation_md(path) -> str:
    bootstrap_project(spec(path))
    return (path / "OPERATION.md").read_text()


def _worker_prompt(path) -> str:
    from jarvis.dispatch import build_worker_prompt

    store = ProjectStore(path)
    try:
        wo = store.create_work_order("ship the thing")
    finally:
        store.close()
    return build_worker_prompt(wo, spec(path))


# -- 1. the contract ------------------------------------------------------------------


def test_the_rule_is_in_the_core_not_behind_a_fetch(project):
    """The core's admission test (worker_brief.CORE_BUDGET_CHARS) asks whether a worker
    lacking the bullet does damage before it would think to fetch the section. This one
    does: by the time the turn has ended the work and the money are already gone, and
    there is no next turn in which to look anything up."""
    from jarvis import worker_brief

    prompt = _worker_prompt(project)
    core = prompt[prompt.index("# Operating contract"):prompt.index("# Full briefings")]

    assert "one-shot" in core
    assert "re-invoked" in core
    assert len(core) < worker_brief.CORE_BUDGET_CHARS


def test_both_worker_texts_teach_that_nothing_wakes_a_worker(project):
    """OPERATION.md is what a worker reads if it goes LOOKING; the dispatched prompt is
    what it reads without looking. A worker that has only one of them still ends turns,
    so both carry the rule — the same pairing the `--evidence` flag needed."""
    for where, text in (("OPERATION.md", _operation_md(project)),
                        ("the worker prompt", _worker_prompt(project))):
        assert "one-shot" in text or "single `claude -p`" in text, (
            f"{where} never says a turn is one process")
        assert "re-invoked" in text, f"{where} never names the false belief"


def test_the_record_section_says_what_to_do_instead(project):
    """A prohibition with no alternative is advice a worker cannot follow. The full
    section has to name both the mechanism and the three things that DO start a turn,
    or 'never background it' reads as 'never do long work'."""
    from jarvis import worker_brief

    section = worker_brief.render_section("record", wo_id="wo-abc123", project="p1")

    assert "one-shot" in section
    assert "claude -p" in section
    for resumer in ("jarvis wo ask", "gate", "message"):
        assert resumer in section, f"the section never names {resumer!r} as a resumer"
    assert "FOREGROUND" in section
    assert "split it" in section


def test_the_template_version_was_bumped(project):
    """Prose is the entire mechanism, and prose only reaches an already-bootstrapped
    repo through the bump. Pinned to the value this work order shipped so a later edit
    that forgets it fails here rather than in a fleet that never sees the paragraph."""
    assert TEMPLATE_VERSION >= 10
    assert f"template v{TEMPLATE_VERSION}" in _operation_md(project)


def test_an_already_bootstrapped_project_is_regenerated_by_the_bump(project):
    op = project / "OPERATION.md"
    bootstrap_project(spec(project))
    stale = op.read_text().replace(f"template v{TEMPLATE_VERSION}", "template v9")
    op.write_text(stale.replace("one-shot", "endlessly resumable"))
    assert "one-shot" not in op.read_text()

    bootstrap_project(spec(project))

    assert "one-shot" in op.read_text()


# -- 2. the attention line ------------------------------------------------------------


def test_the_flag_says_what_happened_not_that_a_turn_ended(project):
    """The two facts the user cannot see for themselves, and guesses wrong without.

    'worker idle without `jarvis wo finish`' said only the second one, and wo-2df8828c
    read as a status bug — the work order LOOKED like it was still running an eval,
    because its last message said so.
    """
    assert "mid-task" in IDLE_NO_FINISH_BLOCKER
    assert "nothing it started is still running" in IDLE_NO_FINISH_BLOCKER
    assert "jarvis wo finish" in IDLE_NO_FINISH_BLOCKER


@pytest.mark.parametrize("status", ["waiting_input", "validating"])
def test_a_worker_that_was_told_to_wait_is_not_accused_of_stopping(project, status):
    """The branches above the default in `settle_work_order`: a work order parked on
    Neo, on a gate or on the round machine is idle BY INSTRUCTION. Accusing it of
    stopping mid-task is the false positive GitHub issue 100 was."""
    store = ProjectStore(project)
    wo = store.create_work_order("task")
    store.set_status(wo["id"], status)

    assert IDLE_NO_FINISH_BLOCKER not in true_blockers(store, store.get_work_order(
        wo["id"]))


# -- 3. the paid A/B stays honest -----------------------------------------------------

EVAL_PATH = Path(__file__).resolve().parents[1] / "evals" / "llm" / "test_one_shot_turn_ab.py"


def test_the_ab_eval_exists_and_is_opt_in():
    text = EVAL_PATH.read_text()
    assert "JARVIS_EVALS_LLM" in text
    assert "JARVIS_EVALS_LLM=1 pytest" in text, "the skip gate never says how to turn it on"


def test_the_ab_markers_still_match_the_shipped_prose(project):
    """The A/B's arms differ by a substring cut. Reword the rule without updating the
    marker and `_strip_rule` silently removes NOTHING — arm WITHOUT becomes arm WITH,
    the eval passes at 100%, and it has measured an A/A."""
    import importlib.util

    spec_ = importlib.util.spec_from_file_location("_one_shot_ab", EVAL_PATH)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)

    from jarvis import worker_brief

    prompt = _worker_prompt(project)
    record = worker_brief.render_section("record", wo_id="wo-abc123", project="p1")

    assert any(ln.startswith(mod.CORE_BULLET_MARKER) for ln in prompt.split("\n")), (
        "the eval's core marker no longer matches a line of the shipped prompt")
    assert mod.RECORD_BLOCK_MARKER in record

    assert mod.CORE_BULLET_MARKER not in mod._strip_rule(prompt)
    assert mod.RECORD_BLOCK_MARKER not in mod._strip_rule(record)


def test_the_ab_cut_removes_the_rule_and_nothing_else(project):
    """The arms must be byte-identical everywhere but the rule, or the A/B measures
    whatever else drifted."""
    import importlib.util

    spec_ = importlib.util.spec_from_file_location("_one_shot_ab", EVAL_PATH)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)

    prompt = _worker_prompt(project)
    kept = mod._strip_rule(prompt).split("\n")
    dropped = [ln for ln in prompt.split("\n") if ln not in kept]

    assert dropped, "the cut removed nothing"
    assert all(ln.startswith(mod.CORE_BULLET_MARKER) for ln in dropped), (
        f"the cut removed lines that are not the rule: {dropped}")


def test_the_ab_asserts_no_cost_or_latency_number():
    """Same ban the other paid evals carry: a test that failed on tokens or seconds
    would spend real money to be flaky."""
    import ast

    tree = ast.parse(EVAL_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            src = ast.dump(node).lower()
            for word in ("seconds", "latency", "cost_usd", "elapsed"):
                assert word not in src, f"the eval asserts on {word}"
