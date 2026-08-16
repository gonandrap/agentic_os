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

THE RETRY APPENDS TO THE USER PROMPT ONLY. `system_prompt` is passed through byte-identical
on every attempt, because that is what keeps consecutive calls inside the Anthropic
prompt cache — a retry that rewrote the system prompt would cost a full cache miss, and
prefix stability is the reason Neo's prompt is built the way it is (see `neo`'s docstring).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from . import claude_cli

#: One JSON object out of possibly-fenced, possibly-chatty output. GREEDY on purpose:
#: it spans from the first `{` to the LAST `}`, so a nested object survives and a
#: trailing ``` fence does not. The cost is that two separate objects in one reply match
#: as one unparseable span — a reply that said two things is not a reply that said one,
#: so refusing it is the right answer rather than silently taking the first.
#: `tests/test_structured.py` pins this input by input.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

#: Prefix for the complaint appended to the user prompt on a retry.
RETRY_NOTE = "Your previous reply could not be used: "


class InvalidOutput(ValueError):
    """The model's reply was not the JSON object the caller asked for.

    Raised by `coerce`/`request` when nothing parses. A `validate` callback is welcome to
    raise it too, but is not required to — anything it raises is caught the same way.
    """


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """Pull one JSON object out of `raw`, or return `None`.

    No policy: `None` means "there was nothing to parse", not "an empty answer". A caller
    that wants an exception or a fallback builds it on top — see `coerce`.
    """
    m = _JSON_RE.search(raw or "")
    if m is None:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
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
            call: Callable[..., Any] = claude_cli.run_headless_result,
            on_usage: Callable[[Any], None] | None = None) -> Any:
    """Ask a model for strict JSON and return the validated value.

    Makes at most `attempts` calls. Each failed attempt appends the validator's complaint
    to the USER prompt (never the system prompt — see the module docstring) and asks
    again from scratch. After the last attempt the reply goes through `coerce`, so
    `on_invalid` decides between a fallback value and a raised exception.

    `call` is the model transport, defaulting to `claude_cli.run_headless_result`; it is
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
