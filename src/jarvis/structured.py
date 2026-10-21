"""Ask a model for strict JSON, validate it, and optionally retry"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from . import claude_cli

#: The default transport
DEFAULT_CALL = claude_cli.run_headless_result

#: One JSON object out of possibly-fenced, possibly-chatty output
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

#: Prefix for the complaint appended to the user prompt on a retry
RETRY_NOTE = "Your previous reply could not be used: "


class InvalidOutput(ValueError):
    """The model's reply was not the JSON object the caller asked for"""


def _load_object(text: str) -> dict[str, Any] | None:
    """`json.loads(text)` when it yields an object, `None` on anything else"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def close_unterminated(text: str) -> str | None:
    """`text` plus the `}`/`]` its still-open containers need, or `None`"""
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
    """Pull one JSON object out of `raw`, or return `None`"""
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


def parse_json_objects(raw: str) -> list[dict[str, Any]]:
    """Every successive top-level JSON OBJECT in `raw`, prose between them skipped (#1098)"""
    raw = raw or ""
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    i = raw.find("{")
    while i >= 0:
        try:
            data, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            i = raw.find("{", i + 1)
            continue
        if isinstance(data, dict):
            out.append(data)
        i = raw.find("{", max(end, i + 1))
    return out


def coerce(raw: str, validate: Callable[[dict[str, Any]], Any],
           on_invalid: Callable[[str], Any] | None = None) -> Any:
    """Parse + validate a reply already in hand"""
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
            on_usage: Callable[[Any], None] | None = None,
            records_itself: str = "") -> Any:
    """Ask a model for strict JSON and return the validated value (#749)"""
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")
    if records_itself and on_usage is None:
        # Before the first attempt, so a refused call spends nothing (spec §2)
        raise claude_cli.AttributionRefused(
            f"records_itself={records_itself!r} says this caller records the call itself "
            "and no on_usage was given: nothing would record it. Pass an "
            "`agent_usage.recorder`, or drop the declaration and let the transport "
            "attribute the call.")
    complaint = ""
    for attempt in range(1, attempts + 1):
        ask = f"{prompt}\n\n{RETRY_NOTE}{complaint}" if complaint else prompt
        declared = {"records_itself": records_itself} if records_itself else {}
        raw, usage = claude_cli.unpack_headless(
            call(ask, system_prompt=system_prompt, model=model, timeout=timeout, cwd=cwd,
                 **declared))
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
