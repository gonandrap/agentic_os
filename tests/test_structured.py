"""The strict-structured-output helper: pull one JSON object out of chatty model
output, validate it, and either fall back or retry.

Two callers with opposite failure policies share this module — Neo's live answering path
(`attempts=1` + a fallback, never a retry) and the panel chair (`attempts=2`, no
fallback) — so the tests here are mostly about *how many calls happen* and *what the
second one says*, not about the parsing alone.
"""

from __future__ import annotations

import json

import pytest

from jarvis import claude_cli, structured


def _needs_ok(data):
    """A validator: `{"ok": …}` is the shape, anything else is not."""
    if "ok" not in data:
        raise structured.InvalidOutput("no `ok` field")
    return data["ok"]


def _headless_calls(fake_claude):
    """Every one-shot `claude -p` invocation the fake recorded, oldest first.

    `run_headless` is synchronous — the call file is written before the process exits and
    `_run` waits for that — so an exact count here is deterministic, unlike the detached
    worker turns that need `wait_calls`.
    """
    return [c for c in fake_claude.calls
            if "-p" in c["argv"] and "--resume" not in c["argv"]]


# --- parse_json_object: tolerance, pinned input by input --------------------------

def test_bare_json_parses():
    assert structured.parse_json_object('{"ok": 1}') == {"ok": 1}


def test_fenced_json_parses():
    raw = '```json\n{"ok": "yes", "why": "r"}\n```'
    assert structured.parse_json_object(raw) == {"ok": "yes", "why": "r"}


def test_chatty_json_parses():
    raw = 'Sure! Here is the verdict:\n{"ok": true}\nLet me know if that helps.'
    assert structured.parse_json_object(raw) == {"ok": True}


def test_nested_objects_survive_the_greedy_match():
    # The greedy `{.*}` is what makes this work: a lazy match would stop at the first
    # closing brace and hand `json.loads` a truncated object.
    raw = 'prose {"ok": {"inner": [1, 2]}} more prose'
    assert structured.parse_json_object(raw) == {"ok": {"inner": [1, 2]}}


@pytest.mark.parametrize("raw", [
    "",
    "total nonsense",
    "no braces here at all",
    "{not json}",
    '{"unterminated": ',
])
def test_garbage_returns_none(raw):
    assert structured.parse_json_object(raw) is None


def test_two_objects_in_one_reply_parse_as_nothing():
    # PINNED, not described: the greedy expression spans from the FIRST `{` to the LAST
    # `}`, so this whole string is the match and it is not valid JSON. A reply that said
    # two things is refused rather than silently reduced to the first of them. Change the
    # regex and this test tells you which inputs moved.
    assert structured.parse_json_object('{"a": 1} and then {"b": 2}') is None


def test_two_objects_on_separate_lines_parse_as_nothing_either():
    # Same span rule, stated from the other side: text BEFORE the first `{` and AFTER the
    # last `}` is dropped, but anything between two objects is not.
    assert structured.parse_json_object('{"a": 1}\n{"b": 2}') is None


def test_prose_after_the_only_object_is_dropped():
    assert structured.parse_json_object('{"ok": 1} — hope that helps') == {"ok": 1}


def test_nothing_to_parse_and_an_empty_object_are_different_answers():
    # The distinction the callers depend on: "nothing to parse" is None, never `{}`, or a
    # validator sees an empty answer where there was in fact no answer at all.
    assert structured.parse_json_object("nope") is None
    assert structured.parse_json_object("{}") == {}


# --- coerce: the attempts=1 policy, on a reply already in hand ---------------------

def test_coerce_returns_the_validated_value():
    assert structured.coerce('{"ok": "yes"}', _needs_ok) == "yes"


def test_coerce_hands_the_raw_string_to_on_invalid_and_returns_its_result():
    seen = []

    def fallback(raw):
        seen.append(raw)
        return {"fell": "back"}

    assert structured.coerce("not json", _needs_ok, on_invalid=fallback) == {"fell": "back"}
    assert seen == ["not json"]  # the RAW string, not the parsed anything


def test_coerce_routes_a_validator_failure_to_on_invalid_too():
    # Parsed fine, wrong shape: the same fallback, because the caller cannot use either.
    assert structured.coerce('{"nope": 1}', _needs_ok, on_invalid=lambda raw: raw[:4]) == '{"no'


def test_coerce_without_on_invalid_raises():
    with pytest.raises(structured.InvalidOutput):
        structured.coerce("not json", _needs_ok)
    with pytest.raises(structured.InvalidOutput):
        structured.coerce('{"nope": 1}', _needs_ok)


# --- request: how many calls, and what the second one says ------------------------

def test_request_returns_the_validated_value():
    calls = []

    def call(prompt, **kw):
        calls.append((prompt, kw))
        return '```json\n{"ok": "done"}\n```'

    assert structured.request("q", validate=_needs_ok, call=call) == "done"
    assert len(calls) == 1
    assert calls[0][0] == "q"  # no complaint appended to a first attempt


def test_attempts_one_makes_exactly_one_call_even_on_garbage(fake_claude, tmp_path):
    # Through the REAL run_headless (default `call`), so a signature change to it breaks
    # this test instead of quietly breaking the caller.
    out = structured.request(
        "FORCE_GARBAGE please decide", validate=_needs_ok,
        system_prompt="you are a test", model="sonnet", cwd=tmp_path,
        attempts=1, on_invalid=lambda raw: {"escalated": raw})
    assert out == {"escalated": "I think you should maybe do the thing?"}

    calls = _headless_calls(fake_claude)
    assert len(calls) == 1, "attempts=1 must never ask twice"
    argv = calls[0]["argv"]
    assert argv[argv.index("-p") + 1] == "FORCE_GARBAGE please decide"
    assert "--append-system-prompt" in argv and "--model" in argv


def test_attempts_two_makes_exactly_two_calls_on_garbage(fake_claude, tmp_path):
    out = structured.request(
        "FORCE_GARBAGE please decide", validate=_needs_ok,
        system_prompt="you are a test", cwd=tmp_path,
        attempts=2, on_invalid=lambda raw: "gave up")
    assert out == "gave up"
    assert len(_headless_calls(fake_claude)) == 2, "attempts=2 must ask exactly twice"


def test_the_retry_appends_the_complaint_to_the_user_prompt_only(fake_claude, tmp_path):
    system = "PERSONA\nlearning one\nlearning two"
    structured.request(
        "FORCE_GARBAGE decide this", validate=_needs_ok,
        system_prompt=system, cwd=tmp_path, attempts=2, on_invalid=lambda raw: None)

    first, second = (c["argv"] for c in _headless_calls(fake_claude))

    def opt(argv, name):
        return argv[argv.index(name) + 1]

    # The system prompt is BYTE-identical across attempts. A retry that rewrote it would
    # cost a full Anthropic prompt-cache miss, which is the whole reason Neo's prompt is
    # built the way it is.
    assert opt(first, "--append-system-prompt") == system
    assert opt(second, "--append-system-prompt") == system

    # The complaint rides in the user prompt, after the original question.
    assert opt(first, "-p") == "FORCE_GARBAGE decide this"
    assert opt(second, "-p").startswith("FORCE_GARBAGE decide this\n\n")
    assert structured.RETRY_NOTE in opt(second, "-p")
    assert "no JSON object in the reply" in opt(second, "-p")


def test_a_retry_that_succeeds_stops_there():
    replies = iter(["nonsense", '{"ok": "second time"}', '{"ok": "never reached"}'])
    seen = []

    def call(prompt, **kw):
        seen.append(prompt)
        return next(replies)

    assert structured.request("q", validate=_needs_ok, attempts=3, call=call) == "second time"
    assert len(seen) == 2


def test_request_without_on_invalid_raises_after_the_last_attempt():
    with pytest.raises(structured.InvalidOutput, match="no JSON object"):
        structured.request("q", validate=_needs_ok, attempts=2,
                           call=lambda prompt, **kw: "nonsense")


def test_request_passes_the_transport_arguments_through(tmp_path):
    seen = {}

    def call(prompt, **kw):
        seen.update(kw)
        return '{"ok": 1}'

    structured.request("q", validate=_needs_ok, system_prompt="sys", model="opus",
                       timeout=17, cwd=tmp_path, call=call)
    assert seen == {"system_prompt": "sys", "model": "opus",
                    "timeout": 17, "cwd": tmp_path}


def test_a_transport_failure_is_not_invalid_output():
    # `on_invalid` is for a reply that arrived and could not be used. A call that never
    # happened propagates, because the caller (see `neo.drain_queue`) tells them apart.
    def call(prompt, **kw):
        raise claude_cli.ClaudeCliError("model call failed")

    with pytest.raises(claude_cli.ClaudeCliError):
        structured.request("q", validate=_needs_ok, attempts=2,
                           on_invalid=lambda raw: "fallback", call=call)


def test_zero_attempts_is_a_programming_error():
    with pytest.raises(ValueError, match="at least 1"):
        structured.request("q", validate=_needs_ok, attempts=0,
                           call=lambda prompt, **kw: '{"ok": 1}')


def test_the_last_attempt_is_exactly_coerce():
    # The property the module docstring claims, asserted rather than described: whatever
    # `coerce` does with a reply in hand is what `request` does with its final one.
    raw = '{"nope": 1}'
    fallback = object()
    assert (structured.request("q", validate=_needs_ok, attempts=1,
                               on_invalid=lambda r: fallback,
                               call=lambda prompt, **kw: raw)
            is structured.coerce(raw, _needs_ok, on_invalid=lambda r: fallback))


# --- the first real consumer: Neo's fail-safe, unchanged --------------------------

def test_neo_parse_verdict_is_the_attempts_one_fallback_configuration():
    from jarvis import neo

    good = json.dumps({"escalate": False, "answer": "go", "reason": "r"})
    assert neo.parse_verdict(good) == structured.coerce(
        good, neo._validate_verdict, on_invalid=neo._unparseable_verdict)
    assert neo.parse_verdict("garbage")["escalate"] is True
