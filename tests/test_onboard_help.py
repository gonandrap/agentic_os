"""The onboarding help session: `ops.onboard_help` and `POST /onboard/{name}/help` (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §4)"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import bootstrap, claude_cli, interview, ops, paths, seats, wiring  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.testing import make_git_project  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402

NAME = "newproj"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture(autouse=True)
def no_inventory(monkeypatch):
    """`wiring.discover` execs `claude`; the view reads one fixed lever instead"""
    item = wiring.Item(kind="skill", name="s", source="user", lever=f"{wiring.LEVER_SKILL}s")
    monkeypatch.setattr(interview, "wiring_choices",
                        lambda draft, *, document: {"items": [item]})


@pytest.fixture()
def draft_file(tmp_path) -> Path:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"os": {}, "projects": []}))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    interview.new_draft(make_git_project(tmp_path / "work", NAME), NAME)
    return interview.draft_file(NAME)


def _two_sections() -> tuple[dict, dict]:
    view = ops.onboard_view(NAME)
    a, b = [s for s in view["sections"]
            if s["key"] not in ("identity", "wiring", "review") and len(s["knobs"]) >= 2][:2]
    return a, b


def _knob_lines(prompt: str) -> list[str]:
    return [line for line in prompt.splitlines() if line.startswith("  ")]


def _flag(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


def test_prompt_puts_the_focused_knob_first_and_keeps_to_its_section(draft_file, fake_claude):
    a, b = _two_sections()
    focus, sibling = a["knobs"][-1]["path"], a["knobs"][0]["path"]
    other = b["knobs"][0]["path"]

    prompt = ops.onboard_help(NAME, a["key"], focus)["prompt"]

    first = _knob_lines(prompt)[0]
    assert first.startswith(f"  {focus} = ") and "the user is asking about this one" in first
    assert any(line.startswith(f"  {sibling} = ") for line in _knob_lines(prompt))
    assert f"{other} = " not in prompt
    # Control: the other section's knob IS in its own section's prompt
    assert f"{other} = " in ops.onboard_help(NAME, b["key"])["prompt"]
    # Deterministic for an unchanged draft
    assert ops.onboard_help(NAME, a["key"], focus)["prompt"] == prompt


def test_spawn_runs_the_helper_persona_from_jarvis_home(draft_file, fake_claude):
    section = _two_sections()[0]["key"]
    res = ops.onboard_help(NAME, section)

    call = next(c for c in fake_claude.calls if res["session_name"] in c["argv"])
    assert _flag(call["argv"], "--agent") == [ops.ONBOARD_HELPER]
    root = Path(_flag(call["argv"], "--add-dir")[0])
    assert (root / ".claude" / "agents" / "onboard_helper.md").is_file()
    assert Path(call["cwd"]).resolve() == paths.jarvis_home().resolve()
    assert res["argv"][1:] == call["argv"]


def test_doctor_does_not_report_the_help_session_as_an_orphan(draft_file, fake_claude,
                                                               tmp_path):
    res = ops.onboard_help(NAME, _two_sections()[0]["key"])
    claude_cli.spawn_background("x", cwd=tmp_path, name="[WO wo-gone] x")

    names = [o["name"] for o in ops.run_doctor().get("orphaned_sessions", [])]
    assert "[WO wo-gone] x" in names  # control: the check does run
    assert res["session_name"] not in names


def test_route_shows_the_session_and_how_to_attach(draft_file, fake_claude):
    section, focus = _two_sections()[0]["key"], _two_sections()[0]["knobs"][0]["path"]
    resp = TestClient(create_app()).post(f"/onboard/{NAME}/help",
                                         data={"section": section, "focus": focus})

    assert resp.status_code == 200
    job = next(s["id"] for s in fake_claude.sessions if s["name"].startswith("[onboard]"))
    page = html.unescape(resp.text)
    assert f"[onboard] {NAME} · {section}" in page
    assert f"claude attach {job}" in page


def test_a_failed_spawn_shows_the_error_and_the_prompt(draft_file, fake_claude,
                                                       monkeypatch, tmp_path):
    section = _two_sections()[0]["key"]
    monkeypatch.setenv(claude_cli.CLAUDE_BIN_ENV, str(tmp_path / "no-claude"))
    prompt = ops.onboard_help(NAME, section)["prompt"]

    resp = TestClient(create_app()).post(f"/onboard/{NAME}/help", data={"section": section})

    assert resp.status_code == 502
    page = html.unescape(resp.text)
    assert "not found on PATH" in page
    assert prompt in page


def test_persona_is_read_only():
    meta, body = seats.parse_definition(
        (bootstrap.ASSETS / "onboard-helper" / "onboard_helper.md").read_text())
    tools = {t.strip() for t in meta["tools"].split(",")}
    assert meta["name"] == ops.ONBOARD_HELPER
    assert meta["description"]
    assert tools == {"Bash", "Read"}
    assert body
