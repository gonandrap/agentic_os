"""Ask a model for strict JSON, validate it, and optionally retry.

Models asked for "reply with JSON and nothing else" mostly comply, and the rest of the
time they fence it, apologise before it, or explain it afterwards. Every caller that
wants a machine-readable answer therefore needs the same three steps — pull one object
out of chatty text, check its shape, decide what to do when it is wrong — and the third
step is the one worth sharing, because the two sensible answers to it are opposites:

* **Fail safe.** Neo's live answering path (`neo.parse_verdict`) turns an unusable reply
  into a synthetic escalation, so a garbled answer reaches the user instead of a worker.
  That is `on_invalid=<a fallback>` with `attempts=1`: never retry, never raise, always
  return something the caller can act on.
* **Retry.** A caller that can afford a second round trip asks again, with the
  validator's complaint appended, and lets the failure propagate if that fails too.
  That is `attempts=2, on_invalid=None`.

`coerce` is exactly `request`'s `attempts=1` body applied to a reply already in hand —
`request`'s final attempt calls it, so the two cannot drift apart. Nothing here holds
policy of its own: `parse_json_object` returns `None` rather than raising or inventing an
empty dict, and `validate` belongs to the caller.

ONE REPAIR IS ATTEMPTED, AND ONLY ONE. A reply that is complete except for the closing
brackets is closed and re-parsed (`close_unterminated`); anything cut off mid-value is
not, because the two failures look nothing alike from the caller's side. The first costs
a byte and, for Neo, an unnecessary interruption of the user. The second would cost a
worker a truncated answer it could not tell from a whole one.

THE RETRY APPENDS TO THE USER PROMPT ONLY. `system_prompt` is passed through byte-identical
on every attempt, because that is what keeps consecutive calls inside the Anthropic
prompt cache — a retry that rewrote the system prompt would cost a full cache miss, and
prefix stability is the reason Neo's prompt is built the way it is (see `neo`'s docstring).
"""

from __future__ import annotations

import json
import re
from functools import partial
from pathlib import Path
from typing import Any, Callable

from . import claude_cli

#: The default transport. `attribute=False` because every caller of `request` that pays
#: for its calls binds them itself through `on_usage`, and the transport's own
#: attribution would then write a second row for the same tokens. A caller that wants
#: the transport to account for it should pass `call=claude_cli.run_headless_result`.
DEFAULT_CALL = partial(claude_cli.run_headless_result, attribute=False)

#: One JSON object out of possibly-fenced, possibly-chatty output. GREEDY on purpose:
#: it spans from the first `{` to the LAST `}`, so a nested object survives and a
#: trailing ``` fence does not. The cost is that two separate objects in one reply match
#: as one unparseable span — a reply that said two things is not a reply that said one,
#: so refusing it is the right answer rather than silently taking the first. That case
#: survives the `close_unterminated` repair untouched, because two whole objects are
#: already bracket-balanced and the repair only ever APPENDS closers.
#: `tests/test_structured.py` pins this input by input.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

#: Prefix for the complaint appended to the user prompt on a retry.
RETRY_NOTE = "Your previous reply could not be used: "


class InvalidOutput(ValueError):
    """The model's reply was not the JSON object the caller asked for.

    Raised by `coerce`/`request` when nothing parses. A `validate` callback is welcome to
    raise it too, but is not required to — anything it raises is caught the same way.
    """


def _load_object(text: str) -> dict[str, Any] | None:
    """`json.loads(text)` when it yields an object, `None` on anything else."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def close_unterminated(text: str) -> str | None:
    """`text` plus the `}`/`]` its still-open containers need, or `None`.

    CLOSERS ONLY, and that restraint is the whole safety argument. An unterminated
    string is not closed, a dangling comma is not trimmed, a missing value is not
    invented — those are the shapes a reply cut off MID-VALUE takes, and a repair that
    guessed at them would hand a caller half an answer wearing a whole answer's shape.
    What this repairs is the one failure it costs nothing to be sure about: a model that
    finished its last value and then forgot the brackets around it. Everything else still
    fails to parse, and for Neo still fails toward the user (see `neo.parse_verdict`).

    `None` means "not repairable this way": already balanced (so the caller's parse
    failed for some other reason, and appending nothing cannot change that), ended inside
    a string, ended on a bare number, or closed a bracket it never opened. Note the scan
    is STRING-AWARE — a naive count calls `{"a": "}"}` unbalanced.

    THE NUMBER IS THE SUBTLE ONE. `{"n": 12` closes to `{"n": 12}` and parses, so nothing
    downstream would ever know the reply might have been cut out of the middle of 123456;
    a digit is the only JSON token whose prefix is itself a valid token. Strings are
    refused because they need a quote invented, and `true`/`false`/`null` are
    self-delimiting, so numbers are the one case where closers alone silently change a
    value rather than failing. Refused for that reason, at the cost of an escalation in
    the case where the model merely forgot the brace after a number.
    """
    stack: list[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None  # crossed brackets: not a truncation, so do not guess
            stack.pop()
    if in_string or not stack or text.rstrip()[-1:].isdigit():
        return None
    return text + "".join(reversed(stack))


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """Pull one JSON object out of `raw`, or return `None`.

    No policy: `None` means "there was nothing to parse", not "an empty answer". A caller
    that wants an exception or a fallback builds it on top — see `coerce`.

    When nothing decodes, ONE repair is attempted: `close_unterminated` appends the
    closing brackets an otherwise-complete object is missing. That is not politeness to a
    sloppy model, it is a measured cost — Neo answered question 145 with 3161 characters
    of perfect JSON and no final `}` (`stop_reason: end_turn`, so not even a truncation),
    the greedy span stopped at the nested `dispatch` block's brace, and one absent byte
    escalated a settled answer to the user. Three spans are tried, longest shot last: the
    greedy match, which drops trailing prose; everything from the first `{`, which is all
    there is when the closing brace never arrived at all and `_JSON_RE` therefore never
    matched; and that same tail with a trailing ``` fence off it. Trimming the fence is
    not a rewrite of the model's JSON — it is the same thing the greedy expression
    already does to its right edge, done for a reply whose last brace is missing and
    whose fence therefore has nothing to hide behind.
    """
    raw = raw or ""
    start = raw.find("{")
    if start < 0:
        return None
    m = _JSON_RE.search(raw)
    if m is not None:
        data = _load_object(m.group(0))
        if data is not None:
            return data
    tail = raw[start:]
    for span in dict.fromkeys([m.group(0) if m else "", tail, tail.rstrip("` \t\r\n")]):
        repaired = close_unterminated(span) if span else None
        if repaired is not None:
            data = _load_object(repaired)
            if data is not None:
                return data
    return None


def coerce(raw: str, validate: Callable[[dict[str, Any]], Any],
           on_invalid: Callable[[str], Any] | None = None) -> Any:
    """Parse + validate a reply already in hand. `request`'s `attempts=1` body.

    Returns `validate(parsed)`. If nothing parses, or `validate` raises, hands the RAW
    string to `on_invalid` and returns whatever that returns; with `on_invalid=None` the
    failure propagates instead.

    Any exception from `validate` is caught, not just `InvalidOutput`: `on_invalid` is a
    fail-safe, and a fail-safe that only covers the failures someone anticipated is not
    one. Pass `on_invalid=None` when you would rather see the traceback.
    """
    try:
        return _parse_and_validate(raw, validate)
    except Exception:
        if on_invalid is None:
            raise
        return on_invalid(raw)


def request(prompt: str, *, validate: Callable[[dict[str, Any]], Any],
            system_prompt: str | None = None, model: str | None = None,
            attempts: int = 1, on_invalid: Callable[[str], Any] | None = None,
            timeout: int = 300, cwd: Path | None = None,
            call: Callable[..., Any] = DEFAULT_CALL,
            on_usage: Callable[[Any], None] | None = None) -> Any:
    """Ask a model for strict JSON and return the validated value.

    Makes at most `attempts` calls. Each failed attempt appends the validator's complaint
    to the USER prompt (never the system prompt — see the module docstring) and asks
    again from scratch. After the last attempt the reply goes through `coerce`, so
    `on_invalid` decides between a fallback value and a raised exception.

    `call` is the model transport, defaulting to `DEFAULT_CALL`; it is
    called as `call(prompt, system_prompt=…, model=…, timeout=…, cwd=…)`. Override it to
    strip the callee's tools (`functools.partial(claude_cli.run_headless_result,
    tools="")`) or, in a test, to record what was asked. A transport that returns a bare
    string still works — see `claude_cli.unpack_headless`. Transport failures —
    `claude_cli.ClaudeCliError` — propagate untouched: a call that never happened is not
    invalid output, and callers already tell those two apart (see `neo.drain_queue`).

    `on_usage(envelope)` is called ONCE PER ATTEMPT, before the reply is validated. A
    retry is a second call the OS paid for, and an accounting that only recorded the
    attempt that happened to parse would quietly under-report exactly the calls that went
    worst. See `agent_usage`.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")
    complaint = ""
    for attempt in range(1, attempts + 1):
        ask = f"{prompt}\n\n{RETRY_NOTE}{complaint}" if complaint else prompt
        raw, usage = claude_cli.unpack_headless(
            call(ask, system_prompt=system_prompt, model=model, timeout=timeout, cwd=cwd))
        if on_usage is not None and usage is not None:
            on_usage(usage)
        if attempt == attempts:
            return coerce(raw, validate, on_invalid=on_invalid)
        try:
            return _parse_and_validate(raw, validate)
        except Exception as exc:
            complaint = str(exc)
    raise AssertionError("unreachable: attempts >= 1 always returns or raises above")


def _parse_and_validate(raw: str, validate: Callable[[dict[str, Any]], Any]) -> Any:
    data = parse_json_object(raw)
    if data is None:
        raise InvalidOutput(f"no JSON object in the reply: {(raw or '')[:120]}")
    return validate(data)
