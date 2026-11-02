"""The LLM gate evals' commands must still trip a gate, checked without a model (issue 780)"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from jarvis import gates

LLM = Path(__file__).parent / "llm"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_fixtures_{name}", LLM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _commands():
    for name in ("test_gate_review_judgment", "test_neo_panel_judgment"):
        mod = _load(name)
        for table in ("MUST_DISMISS", "MUST_NOT_DISMISS", "CONTESTS"):
            for row in getattr(mod, table, []):
                yield pytest.param(row[1], id=f"{name}:{table}:{row[0]}")


@pytest.mark.parametrize("command", list(_commands()))
def test_eval_command_still_trips_a_gate(command):
    cfg = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))
    assert gates.classify(command, cfg) is not None, (
        f"{command!r} no longer trips a gate; pick a command the recogniser still matches")
