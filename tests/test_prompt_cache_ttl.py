"""What TTL Jarvis buys when it writes to the prompt cache, on every launch path.

A prompt-cache WRITE costs 1.25x base input at the 5-minute TTL and 2x at the one-hour
one; a READ is 0.1x under either. Claude Code picks the one-hour TTL by default for the
`querySource` every `claude -p` presents, so the expensive write is what Jarvis gets
unless it says otherwise — on every path, every time.

WHY THIS FILE EXISTS RATHER THAN ONE ASSERTION NEXT TO EACH CALL SITE. The flag first
shipped in `dispatch._write_worker_settings` (wo-5668a3f7) and was tested there
(`test_workers_buy_the_five_minute_cache_not_the_one_hour_one`, tests/test_worker_
session.py), which proved the property for worker turns and for nothing else. It stayed
false for Neo, the panel's seats and the dashboard digest for another ten days,
invisibly: measured on wo-b9563d2b, the worker's own turns wrote 362,028 tokens all at
5m while Jarvis's own calls on the same order wrote 28,804, ALL AT 1H. A per-call-site
test cannot fail for a call site nobody thought of, so the guard here is over the SEAM —
every subprocess in `claude_cli` must go through one of the two functions that force it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from jarvis import claude_cli

FLAG = "FORCE_PROMPT_CACHING_5M"

#: The two functions in `claude_cli` allowed to start a `claude` process, because they
#: are the two that apply `PROMPT_CACHE_5M_ENV`. A third would silently buy the 2x write.
LAUNCHERS = {"_run", "spawn_turn"}


def _cache_env(call: dict) -> dict:
    return call.get("cache_env") or {}


@pytest.fixture(autouse=True)
def _no_inherited_flag(monkeypatch):
    """Clear the flag out of the ambient environment before every test here.

    NOT tidiness — without this the whole file is vacuous, and it passes while proving
    nothing. These tests are usually run BY a Jarvis worker, and a worker's own session
    carries `FORCE_PROMPT_CACHING_5M=1` from its settings file. pytest inherits it, the
    fake `claude` inherits it from pytest, and every assertion below holds with the
    implementation deleted. Verified by mutation: reverting `_run` and `spawn_turn` left
    all seven green until this fixture existed (kn-95a32178).
    """
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv("ENABLE_PROMPT_CACHING_1H", raising=False)


# -- the seam ------------------------------------------------------------------------


def test_only_the_two_launchers_start_a_claude_process() -> None:
    """No new launch path can appear without either forcing the TTL or failing here.

    Asserted over the AST rather than by grep so a `subprocess.run` buried in a nested
    helper is still attributed to the function that contains it.
    """
    tree = ast.parse(Path(inspect.getfile(claude_cli)).read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            fn = getattr(inner, "func", None)
            if (isinstance(inner, ast.Call) and isinstance(fn, ast.Attribute)
                    and fn.attr in {"run", "Popen"}
                    and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"
                    and node.name not in LAUNCHERS):
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, (
        f"these functions start a subprocess outside {sorted(LAUNCHERS)} and so do not "
        f"force the 5-minute prompt cache: {offenders}. Route them through `_run`, or "
        f"apply `claude_cli.cache_env()` and add them to LAUNCHERS.")


def test_the_flag_value_is_one_the_cli_actually_parses() -> None:
    """Claude Code's boolean reader accepts exactly 1/true/yes/on (kn-522c6103).

    "TRUE" works and "y" does not, and the failure mode of getting it wrong is silence:
    nothing errors, the flag is simply ignored and the hour is bought again.
    """
    assert claude_cli.PROMPT_CACHE_5M_ENV == {FLAG: "1"}


# -- precedence ------------------------------------------------------------------------


def test_ambient_environment_cannot_re_buy_the_hour(monkeypatch) -> None:
    """A stray variable in the daemon's own environment must not decide the rate.

    Both directions matter. `ENABLE_PROMPT_CACHING_1H` loses because Claude Code checks
    the 5m flag first and short-circuits (`EEe`, 2.1.240); a `FORCE_PROMPT_CACHING_5M=0`
    inherited from a shell would win, so it is overwritten rather than merged.
    """
    monkeypatch.setenv(FLAG, "0")
    monkeypatch.setenv("ENABLE_PROMPT_CACHING_1H", "1")
    assert claude_cli.cache_env()[FLAG] == "1"


def test_an_explicit_caller_can_still_buy_another_ttl() -> None:
    """`env_extra` sits above the default, so this decision stays A/B-measurable."""
    assert claude_cli.cache_env({FLAG: "0"})[FLAG] == "0"


# -- the paths, end to end -------------------------------------------------------------


def test_jarvis_own_headless_calls_buy_the_five_minute_write(fake_claude, tmp_path) -> None:
    """The regression this file was written for.

    `run_headless_result` is the transport for Neo answering a worker's question, for
    each of the panel's seats and for the dashboard digest — every token Jarvis spends
    on itself. It passed no settings file and no env, so all of it wrote at 2x.
    """
    claude_cli.run_headless("hi", cwd=tmp_path)
    assert _cache_env(fake_claude.calls[-1]) == {FLAG: "1"}


def test_env_extra_does_not_drop_the_flag(fake_claude, tmp_path) -> None:
    """The tooled callers (the retrieval eval's subject) pass `env_extra` for PATH."""
    claude_cli.run_headless("hi", cwd=tmp_path, env_extra={"PATH": "/usr/bin"})
    assert _cache_env(fake_claude.calls[-1]) == {FLAG: "1"}


def test_a_worker_turn_carries_the_flag_in_its_environment_too(fake_claude, tmp_path) -> None:
    """Belt and braces beside `--settings`.

    A worker turn gets the flag twice over: in the settings file (what reaches a session
    the CLI reloads settings for) and in the spawn environment. This asserts the second,
    with NO settings file passed, so the property does not depend on the file existing —
    which is exactly the state a turn launched outside `worker_session.briefing_for`
    would be in.
    """
    claude_cli.spawn_turn("hi", cwd=tmp_path, session_id="s-1",
                          outfile=tmp_path / "o.json", errfile=tmp_path / "o.err")
    calls = fake_claude.wait_calls(lambda c: "-p" in c["argv"])
    assert calls, "the fake claude was never invoked"
    assert _cache_env(calls[-1]) == {FLAG: "1"}
