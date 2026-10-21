"""The plan-review eval must not be able to skip itself into silence"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_plan_review_judgment.py"

ENV_VAR = "JARVIS_EVALS_LLM"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def eval_module():
    return _load(EVAL_PATH, "_plan_review_judgment_under_test")


def test_the_skip_gate_is_spelled_exactly_right():
    tree = ast.parse(EVAL_PATH.read_text())
    gates = [n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and n.value == ENV_VAR]
    assert gates, f"the eval no longer gates on {ENV_VAR!r} — lifetime silent skip"


def test_the_battery_is_skeleton_shaped(eval_module):
    battery = eval_module.MUST_RELEASE + eval_module.MUST_BLOCK
    assert len(battery) == 7
    assert len({name for name, _ in battery}) == 7, "colliding scenario names"
    for name, question in battery:
        # Composed by the shipped skeleton renderer, briefs withheld
        assert "Release this plan?" in question, name
        assert eval_module.FAT_MARKER not in question, (
            f"{name}: a child brief leaked into the question")
        assert len(question) < 10_000, (
            f"{name}: {len(question)} chars — the diet this eval guards is gone")
    # Control in the same test: the briefs the questions withhold really are fat
    from jarvis.plans import MAX_DESCRIPTION_CHARS

    fat = eval_module._child("k", "t", "a")
    assert eval_module.FAT_MARKER in fat["description"]
    assert len(fat["description"]) > MAX_DESCRIPTION_CHARS * 0.7


def test_a_canned_model_drives_the_real_plumbing(eval_module, tmp_path, monkeypatch):
    """Everything but the judgment: store, kind='plan', persona, parse, collection"""
    from jarvis import claude_cli

    calls = []

    def canned(prompt, system_prompt=None, model=None, timeout=300, cwd=None,
               tools=None, **kwargs):
        calls.append({"prompt": prompt, "system": system_prompt or ""})
        return claude_cli.HeadlessResult(
            text='{"escalate": false, "verdict": "approve", "reason": "canned"}')

    monkeypatch.setattr(claude_cli, "run_headless_result", canned)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    verdicts = eval_module.collect_verdicts("canned-model", tmp_path)

    assert set(verdicts) == {n for n, _ in
                             eval_module.MUST_RELEASE + eval_module.MUST_BLOCK}
    assert all(v["verdict"] == "approved" for v in verdicts.values())
    # kind="plan" selected the plan-review persona, not the general answerer
    assert all("reviewing a PLAN" in c["system"] for c in calls)
    # And each call carried its scenario's skeleton question
    assert sum("Release this plan?" in c["prompt"] for c in calls) == len(calls)
