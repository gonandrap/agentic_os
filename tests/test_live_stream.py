"""The worker conversation, line by line and a page at a time"""

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


def steps(path: Path, count: int, *, start: float = 1000.0) -> None:
    write(path, [prompt_row(start, "go"),
                 *[text_row(start + n, f"m{n}", f"step {n}") for n in range(count)]])


def texts(payload: dict) -> list[str]:
    return [line["text"] for line in payload["lines"]]


def kinds(payload: dict) -> list[str]:
    return [line["kind"] for line in payload["lines"]]


# -- one numbering, over the whole conversation


def test_every_event_is_numbered_forward_and_names_the_turn_it_belongs_to(transcript):
    """Paging back crosses turn boundaries, so the seq cannot restart at each one"""
    write(transcript, [
        prompt_row(1000, "the first ask"),
        text_row(1005, "m1", "done with that"),
        prompt_row(1010, "the second ask"),
        tool_use_row(1020, "t1", "Bash", {"command": "pytest -q"}),
        tool_result_row(1025, "t1"),
    ])

    payload = live.conversation_lines(transcript, limit=10)

    assert [line["seq"] for line in payload["lines"]] == [1, 2, 3, 4, 5]
    assert [line["turn"] for line in payload["lines"]] == [1, 1, 2, 2, 2]
    assert kinds(payload) == ["prompt", "text", "prompt", "tool", "result"]
    assert payload["turn"] == {"seq": 2, "started": 1010.0}
    assert payload["total"] == 5


def test_the_default_window_is_the_tail_and_says_there_is_more_before_it(transcript):
    """A modal opened an hour in shows the end of the conversation, not its start"""
    steps(transcript, 20)

    payload = live.conversation_lines(transcript, limit=5)

    assert texts(payload) == [f"step {n}" for n in range(15, 20)]
    assert payload["first"] == 17 and payload["cursor"] == 21
    assert payload["more_before"] is True
    assert payload["total"] == 21


def test_a_window_that_reaches_the_first_line_says_there_is_nothing_before_it(transcript):
    steps(transcript, 3)

    payload = live.conversation_lines(transcript, limit=50)

    assert payload["first"] == 1 and payload["more_before"] is False
    assert len(payload["lines"]) == payload["total"] == 4


def test_before_returns_the_page_immediately_preceding_that_line(transcript):
    """The "load more" button: one page older, on demand, never the whole conversation"""
    steps(transcript, 20)
    tail = live.conversation_lines(transcript, limit=5)

    older = live.conversation_lines(transcript, limit=5, before=tail["first"])

    assert texts(older) == [f"step {n}" for n in range(10, 15)]
    assert older["cursor"] == tail["first"] - 1
    assert older["more_before"] is True
    # …and the page before THAT one reaches the prompt the conversation opened with
    oldest = live.conversation_lines(transcript, limit=50, before=older["first"])
    assert oldest["lines"][0]["kind"] == "prompt"
    assert oldest["more_before"] is False


def test_after_returns_only_what_arrived_since_the_cursor(transcript):
    """The live poll: the modal appends rather than re-rendering what it already holds"""
    steps(transcript, 3)
    seen = live.conversation_lines(transcript, limit=50)
    append(transcript, [text_row(1100, "m-new", "step 3")])

    fresh = live.conversation_lines(transcript, limit=50, after=seen["cursor"])

    assert texts(fresh) == ["step 3"]
    assert fresh["first"] == fresh["cursor"] == seen["cursor"] + 1


def test_a_poll_that_fell_behind_takes_one_page_at_a_time(transcript):
    """`after` is capped like every other window: nothing returns the whole conversation"""
    steps(transcript, 20)

    caught_up = live.conversation_lines(transcript, limit=5, after=1)

    assert texts(caught_up) == [f"step {n}" for n in range(0, 5)]
    assert caught_up["cursor"] == 6
    assert caught_up["total"] == 21  # and the payload says how far behind it still is


def test_a_lines_number_does_not_shift_when_the_conversation_grows(transcript):
    write(transcript, [prompt_row(1000, "go"), text_row(1010, "m1", "first")])
    before = live.conversation_lines(transcript, limit=50)["lines"]

    append(transcript, [text_row(1020, "m2", "second")])
    after = live.conversation_lines(transcript, limit=50)["lines"]

    assert [line["seq"] for line in before] == [1, 2]
    assert [line["seq"] for line in after] == [1, 2, 3]
    assert after[:2] == before


# -- what one line carries


def test_a_tool_call_carries_its_name_its_detail_and_its_redacted_parameters(transcript):
    """The same redaction the live block states: `live.redact_params`, not a second path"""
    write(transcript, [
        prompt_row(1000, "go"),
        tool_use_row(1010, "t1", "Bash",
                     {"command": "deploy --now", "api_key": "sk-abcdef123456"}),
    ])

    (_prompt, call) = live.conversation_lines(transcript, limit=10)["lines"]

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

    result = live.conversation_lines(transcript, limit=10)["lines"][-1]

    assert result["kind"] == "result"
    assert result["text"] == "Bash"
    assert result["detail"] == "3 passed"


def test_an_assistant_message_is_a_line_of_its_own(transcript):
    write(transcript, [prompt_row(1000, "go"), text_row(1010, "m1", "reading the code")])

    said = live.conversation_lines(transcript, limit=10)["lines"][-1]

    assert said["kind"] == "text"
    assert said["text"] == "reading the code"


def test_a_transcript_that_is_not_there_is_an_empty_window_and_not_an_error(tmp_path):
    empty = {"turn": None, "lines": [], "first": 0, "cursor": 0, "total": 0,
             "more_before": False}

    assert live.conversation_lines(tmp_path / "gone.jsonl", limit=10) == empty
    assert live.conversation_lines(None, limit=10) == empty


def test_a_session_with_no_transcript_resolves_to_no_path(tmp_path, monkeypatch):
    """The one resolver both the live frame and the stream bind through"""
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    assert live.transcript_of("nobody-here") is None


# -- nothing reaches the browser unscrubbed


def test_a_tool_result_that_printed_a_credential_names_it_instead_of_quoting_it(transcript):
    """kn-e97f6b51: a result head is where a secret surfaces — a read of `.env`, a printed token"""
    write(transcript, [
        prompt_row(1000, "go"),
        tool_use_row(1010, "t1", "Read", {"file_path": ".env"}),
        tool_result_row(1015, "t1", "api_key=sk-abcdef123456 and nothing else"),
    ])

    payload = live.conversation_lines(transcript, limit=10)

    assert "sk-abcdef123456" not in json.dumps(payload)
    assert "<redacted:" in payload["lines"][-1]["detail"]


def test_an_assistant_line_that_quoted_a_credential_names_it_instead(transcript):
    write(transcript, [
        prompt_row(1000, "go"),
        text_row(1010, "m1", "the token is Authorization: Bearer sk-abcdef123456"),
    ])

    payload = live.conversation_lines(transcript, limit=10)

    assert "sk-abcdef123456" not in json.dumps(payload)


def test_a_prompt_line_that_carried_a_credential_names_it_instead(transcript):
    """The user's own message is streamed too, and it is where a pasted token lands"""
    write(transcript, [prompt_row(1000, "deploy with api_key=sk-abcdef123456")])

    payload = live.conversation_lines(transcript, limit=10)

    assert "sk-abcdef123456" not in json.dumps(payload)
    assert "<redacted:" in payload["lines"][0]["text"]


# -- the page size is the catalog's


def test_the_page_size_is_a_per_project_catalog_setting(tmp_path):
    from jarvis.catalog import InspectConfig, load_catalog

    assert InspectConfig().stream_lines == live.STREAM_LINES

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "os": {"inspect": {"stream_lines": 50}},
        "projects": [{"name": "proj_a", "path": str(tmp_path),
                      "inspect": {"stream_lines": 25}},
                     {"name": "proj_b", "path": str(tmp_path)}],
    }))

    catalog = load_catalog(path)

    assert catalog.os.inspect.stream_lines == 50
    assert {p.name: p.inspect.stream_lines for p in catalog.projects} == {
        "proj_a": 25, "proj_b": 50}


def test_a_page_size_below_one_is_refused(tmp_path):
    """`_parse_inspect`'s floor: a window of zero lines is a modal that never fills"""
    from jarvis.catalog import CatalogError, load_catalog

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "os": {"inspect": {"stream_lines": 0}},
        "projects": [{"name": "proj_a", "path": str(tmp_path)}],
    }))

    with pytest.raises(CatalogError, match="stream_lines"):
        load_catalog(path)
