"""A worker's `AskUserQuestion` is filed as Neo questions"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import concision, hooks, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

#: The shape wo-0d358979's worker sent, trimmed
QUESTIONS = [
    {"question": "How do you want the additive cache sequenced?", "header": "Sequencing",
     "multiSelect": False,
     "options": [{"label": "Follow-up WO (Recommended)", "description": "ship #1275 now"},
                 {"label": "Extend this PR", "description": "one bigger PR"}]},
    {"question": "What should the cold computing page show?", "header": "Computing page",
     "multiSelect": True,
     "options": [{"label": "CSS animation", "description": "no JavaScript"},
                 {"label": "File count", "description": ""}]},
]


def ask_payload(questions=QUESTIONS) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
            "tool_input": {"questions": questions}}


@pytest.fixture()
def worker(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "build the exporter")
    Daemon(load_catalog(catalog_file)).tick()
    return wo


def neo_questions(wo_id: str) -> list[dict]:
    neo = NeoStore()
    try:
        return [q for q in neo.list_questions() if q["wo_id"] == wo_id]
    finally:
        neo.close()


def test_every_entry_is_filed_for_neo_and_the_dialog_denied(worker):
    decision = hooks.preflight_decision(ask_payload(), {"JARVIS_WO_ID": worker["id"]})

    assert decision is not None
    out = decision["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    asked = neo_questions(worker["id"])
    assert len(asked) == 2  # one atomic question per entry
    for q in asked:
        assert f"Q{q['id']}" in out["permissionDecisionReason"]
    assert "End your turn" in out["permissionDecisionReason"]
    assert "NOT the user declining" in out["permissionDecisionReason"]


def test_the_record_carries_the_question_text_and_the_order_waits(worker):
    hooks.preflight_decision(ask_payload(), {"JARVIS_WO_ID": worker["id"]})

    _, path, _ = ops.find_work_order(worker["id"])
    store = ProjectStore(path)
    try:
        texts = [json.loads(e["payload"])["question"]
                 for e in store.events_of_kind(worker["id"], "question_asked")]
        assert store.get_work_order(worker["id"])["status"] == "waiting_input"
    finally:
        store.close()
    assert any("[Sequencing]" in t and "Follow-up WO (Recommended) — ship #1275 now" in t
               for t in texts)
    assert any("pick any of" in t for t in texts)


def test_a_session_that_is_not_a_worker_keeps_its_dialog():
    """The user's own session, or an injected one, has a human at the terminal"""
    assert hooks.preflight_decision(ask_payload(), {}) is None


def test_a_question_that_cannot_be_filed_still_denies_and_names_the_channel(jarvis_home):
    decision = hooks.preflight_decision(ask_payload(), {"JARVIS_WO_ID": "wo-nosuch"})

    assert decision is not None
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "jarvis wo ask wo-nosuch" in reason
    assert "Sequencing" in reason


def test_an_overlong_entry_drops_its_descriptions_to_fit_the_cap():
    entry = {"question": "Which?", "header": "H",
             "options": [{"label": "A", "description": "x" * concision.DEFAULT_QUESTION_MAX_CHARS},
                         {"label": "B", "description": "y"}]}
    assert len(hooks.native_question_text(entry)) > concision.DEFAULT_QUESTION_MAX_CHARS
    assert hooks.native_question_text(entry, with_descriptions=False) == \
        "[H] Which? Options, pick one of: (1) A; (2) B."


def test_settings_matcher_names_ask_user_question():
    """Without it the hook arm is dead code in production"""
    settings = json.loads(
        (Path(hooks.__file__).parent / "assets" / "settings.base.json").read_text())
    matcher = settings["hooks"]["PreToolUse"][0]["matcher"]

    assert hooks.NATIVE_QUESTION_TOOL in matcher.split("|")
