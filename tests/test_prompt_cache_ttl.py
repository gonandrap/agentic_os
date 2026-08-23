"""What TTL Jarvis buys when it writes to the prompt cache, on every launch path.

A cache WRITE is 1.25x base input at the 5-minute TTL and 2x at the one-hour one, and the
CLI picks the hour by default for the `querySource` every `claude -p` presents.

The guard here is over the SEAM rather than over each call site, because a per-call-site
test cannot fail for a call site nobody thought of — which is exactly how Neo, the panel
and the digest kept buying the hour for ten days after workers stopped. Background and
the reversal criteria:
`docs/superpowers/specs/2026-08-22-the-five-minute-write-everywhere.md`.
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

    NOT tidiness — without this the whole file is vacuous. These tests are usually run BY
    a Jarvis worker, whose own session carries `FORCE_PROMPT_CACHING_5M=1`; pytest
    inherits it and hands it to the fake `claude`, so every assertion below passes with
    the implementation deleted (verified by mutation; kn-95a32178).
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
    """The CLI accepts exactly 1/true/yes/on (kn-522c6103), and "y" is silently ignored."""
    assert claude_cli.PROMPT_CACHE_5M_ENV == {FLAG: "1"}


# -- precedence ------------------------------------------------------------------------


def test_ambient_environment_cannot_re_buy_the_hour(monkeypatch) -> None:
    """A stray variable in the daemon's own environment must not decide the rate.

    Both directions: the 1h flag loses to the CLI's own short-circuit, and an inherited
    `FORCE_PROMPT_CACHING_5M=0` would win unless it is overwritten rather than merged.
    """
    monkeypatch.setenv(FLAG, "0")
    monkeypatch.setenv("ENABLE_PROMPT_CACHING_1H", "1")
    assert claude_cli.cache_env()[FLAG] == "1"


def test_an_explicit_caller_can_still_buy_another_ttl() -> None:
    """`env_extra` sits above the default, so this decision stays A/B-measurable."""
    assert claude_cli.cache_env({FLAG: "0"})[FLAG] == "0"


# -- the paths, end to end -------------------------------------------------------------


def test_jarvis_own_headless_calls_buy_the_five_minute_write(fake_claude, tmp_path) -> None:
    """The regression this file was written for: `run_headless_result` is the transport
    for Neo, the panel's seats and the digest, and it passed no settings file and no env.
    """
    claude_cli.run_headless("hi", cwd=tmp_path)
    assert _cache_env(fake_claude.calls[-1]) == {FLAG: "1"}


def test_env_extra_does_not_drop_the_flag(fake_claude, tmp_path) -> None:
    """The tooled callers (the retrieval eval's subject) pass `env_extra` for PATH."""
    claude_cli.run_headless("hi", cwd=tmp_path, env_extra={"PATH": "/usr/bin"})
    assert _cache_env(fake_claude.calls[-1]) == {FLAG: "1"}


def test_a_worker_turn_carries_the_flag_in_its_environment_too(fake_claude, tmp_path) -> None:
    """Belt and braces beside `--settings`: asserted with NO settings file passed, so the
    property does not depend on one being written.
    """
    claude_cli.spawn_turn("hi", cwd=tmp_path, session_id="s-1",
                          outfile=tmp_path / "o.json", errfile=tmp_path / "o.err")
    calls = fake_claude.wait_calls(lambda c: "-p" in c["argv"])
    assert calls, "the fake claude was never invoked"
    assert _cache_env(calls[-1]) == {FLAG: "1"}
