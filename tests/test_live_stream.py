"""The current turn's events, line by line — what the debug page's terminal modal tails"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis import live, usage


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(at: float, text: str) -> dict:
    return {"type": "user", "timestamp": stamp(at), "promptSource": "sdk",
            "message": {"content": text}}


def text_row(at: float, mid: str, text: str) -> dict:
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0, "output_tokens": 1},
                        "content": [{"type": "text", "text": text}]}}


def tool_use_row(at: float, tool_id: str, name: str, payload: dict | None = None) -> dict:
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": f"m-{tool_id}", "model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0, "output_tokens": 1},
                        "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                     "input": payload or {}}]}}


def tool_result_row(at: float, tool_id: str, content: str = "ok") -> dict:
    return {"type": "user", "timestamp": stamp(at),
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
                                     "content": content}]}}


@pytest.fixture()
def transcript(tmp_path) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("")
    return path


def write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def append(path: Path, rows: list[dict]) -> None:
    with path.open("a") as handle:
        handle.write("".join(json.dumps(r) + "\n" for r in rows))


def kinds(payload: dict) -> list[str]:
    return [line["kind"] for line in payload["lines"]]


# -- the window is ONE turn


def test_only_the_current_turns_events_are_returned_anchored_at_its_prompt(transcript):
    """"from the start of this turn" — the previous turn's lines are gone, not scrolled past"""
    write(transcript, [
        prompt_row(1000, "the first ask"),
        text_row(1005, "m1", "done with that"),
        prompt_row(1010, "the second ask"),
        *[tool_use_row(1020, "t1", "Bash", {"command": "pytest -q"})],
        tool_result_row(1025, "t1"),
    ])

    payload = live.turn_lines(transcript)

    assert payload["turn"] == {"seq": 2, "started": 1010.0}
    assert kinds(payload) == ["prompt", "tool", "result"]
    assert payload["lines"][0]["text"] == "the second ask"
    assert "the first ask" not in json.dumps(payload)


def test_a_tool_call_carries_its_name_its_detail_and_its_redacted_parameters(transcript):
    """The same redaction the live block states: `live.redact_params`, not a second path"""
    write(transcript, [
        prompt_row(1000, "go"),
        tool_use_row(1010, "t1", "Bash",
                     {"command": "deploy --now", "api_key": "sk-abcdef123456"}),
    ])

    (_prompt, call) = live.turn_lines(transcript)["lines"]

    assert call["kind"] == "tool"
    assert call["text"] == "Bash"
    assert call["detail"] == "deploy --now"
    assert call["params"]["api_key"] == live.REDACTED
    assert "sk-abcdef123456" not in json.dumps(call)


def test_a_result_line_names_the_tool_it_closed_and_quotes_its_head(transcript):
    write(transcript, [
        prompt_row(1000, "go"),
        tool_use_row(1010, "t1", "Bash", {"command": "pytest -q"}),
        tool_result_row(1015, "t1", "3 passed"),
    ])

    result = live.turn_lines(transcript)["lines"][-1]

    assert result["kind"] == "result"
    assert result["text"] == "Bash"
    assert result["detail"] == "3 passed"


def test_an_assistant_message_is_a_line_of_its_own(transcript):
    write(transcript, [prompt_row(1000, "go"), text_row(1010, "m1", "reading the code")])

    said = live.turn_lines(transcript)["lines"][-1]

    assert said["kind"] == "text"
    assert said["text"] == "reading the code"


def test_a_long_turn_keeps_the_last_lines_and_says_how_many_it_dropped(transcript):
    """A modal opened an hour into a turn must not ship the whole hour"""
    rows = [prompt_row(1000, "go")]
    for n in range(20):
        rows.append(text_row(1000 + n, f"m{n}", f"step {n}"))
    write(transcript, rows)

    payload = live.turn_lines(transcript, cap=5)

    assert len(payload["lines"]) == 5
    assert payload["dropped"] == 16  # the prompt and the first 15 steps
    assert payload["lines"][-1]["text"] == "step 19"
    assert payload["cursor"] == 21


def test_a_lines_number_does_not_shift_when_the_turn_grows(transcript):
    """The cursor the modal polls with: a seq that renumbered would replay or skip lines"""
    write(transcript, [prompt_row(1000, "go"), text_row(1010, "m1", "first")])
    before = live.turn_lines(transcript)["lines"]

    append(transcript, [text_row(1020, "m2", "second")])
    after = live.turn_lines(transcript)["lines"]

    assert [line["seq"] for line in before] == [1, 2]
    assert [line["seq"] for line in after] == [1, 2, 3]
    assert after[:2] == before


def test_a_transcript_that_is_not_there_is_an_empty_window_and_not_an_error(tmp_path):
    payload = live.turn_lines(tmp_path / "gone.jsonl")

    assert payload == {"turn": None, "lines": [], "cursor": 0, "dropped": 0}
    assert live.turn_lines(None) == payload


def test_a_session_with_no_transcript_resolves_to_no_path(tmp_path, monkeypatch):
    """The one resolver both the live frame and the stream bind through"""
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    assert live.transcript_of("nobody-here") is None
