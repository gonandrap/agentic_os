"""What TTL Jarvis buys when it writes to the prompt cache, on every launch path.

A cache WRITE is 1.25x base input at the 5-minute TTL and 2x at the one-hour one; a READ
is 0.1x under either. The CLI decides the TTL per request from two environment variables,
one for the main thread and one for everything else (subagents, helpers), and
`FORCE_PROMPT_CACHING_5M` short-circuits both — so Jarvis has to pin all three to own the
decision. The lead and its crew get DIFFERENT answers on purpose: the lead idles while
the crew works and pays the hour only on its deltas, a subagent is never resumed and
writes its whole prefix cold. Background and the measurements:
`docs/superpowers/findings/2026-10-11-the-lead-buys-the-hour-the-crew-does-not.md`.

The guard here is over the SEAM rather than over each call site, because a per-call-site
test cannot fail for a call site nobody thought of — which is exactly how Neo, the panel
and the digest kept buying the hour for ten days after workers stopped (spec
2026-08-22-the-five-minute-write-everywhere.md).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from jarvis import claude_cli

FORCE = "FORCE_PROMPT_CACHING_5M"
LEAD = "CLAUDE_CODE_PROMPT_CACHE_TTL"
SUBAGENT = "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"

#: The two functions in `claude_cli` allowed to start a `claude` process, because they
#: are the two that apply `cache_env`. A third would buy whatever the CLI felt like.
LAUNCHERS = {"_run", "spawn_turn"}


def _cache_env(call: dict) -> dict:
    return call.get("cache_env") or {}


@pytest.fixture(autouse=True)
def _no_inherited_flag(monkeypatch):
    """Clear every TTL variable out of the ambient environment before each test here.

    NOT tidiness — without this the whole file is vacuous. These tests are usually run BY
    a Jarvis worker, whose own session carries the variables from its settings file;
    pytest inherits them and hands them to the fake `claude`, so every assertion below
    passes with the implementation deleted (verified by mutation; kn-95a32178).
    """
    for var in (FORCE, LEAD, SUBAGENT, "ENABLE_PROMPT_CACHING_1H"):
        monkeypatch.delenv(var, raising=False)


# -- the seam ------------------------------------------------------------------------


def test_only_the_two_launchers_start_a_claude_process() -> None:
    """No new launch path can appear without either pinning the TTL or failing here."""
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
        f"pin the prompt-cache TTL: {offenders}. Route them through `_run`, or apply "
        f"`claude_cli.cache_env()` and add them to LAUNCHERS.")


# -- what the environment says ---------------------------------------------------------


def test_the_default_is_the_five_minute_write_on_both_threads() -> None:
    """Jarvis's own one-shot calls (Neo, the panel, the digest) keep the cheap write."""
    assert claude_cli.cache_env() == {FORCE: "0", LEAD: "5m", SUBAGENT: "5m"}


def test_the_lead_and_its_crew_can_be_bought_different_ttls() -> None:
    assert claude_cli.cache_env(lead="1h", subagent="5m") == {
        FORCE: "0", LEAD: "1h", SUBAGENT: "5m"}


@pytest.mark.parametrize("bad", ["1H", "60m", "", "hour"])
def test_a_ttl_the_cli_would_not_parse_is_refused(bad: str) -> None:
    """The CLI reads exactly "5m" or "1h"; anything else falls through to ITS default,
    silently, which is the failure mode this whole file exists to prevent."""
    with pytest.raises(ValueError):
        claude_cli.cache_env(lead=bad)
    with pytest.raises(ValueError):
        claude_cli.cache_env(subagent=bad)


def test_the_force_flag_is_pinned_off_so_the_pair_decides() -> None:
    """`FORCE_PROMPT_CACHING_5M` beats the pair inside the CLI, and the user's own
    `~/.claude/settings.json` may well set it — so Jarvis has to say "0", a value the
    CLI reads as false (it accepts exactly 1/true/yes/on; kn-522c6103)."""
    env = claude_cli.cache_env(lead="1h")
    assert env[FORCE] == "0"


# -- precedence ------------------------------------------------------------------------


def test_ambient_environment_cannot_decide_the_rate(monkeypatch) -> None:
    """A stray variable in the daemon's own environment must not reach the CLI."""
    monkeypatch.setenv(FORCE, "1")
    monkeypatch.setenv(LEAD, "1h")
    monkeypatch.setenv("ENABLE_PROMPT_CACHING_1H", "1")
    assert claude_cli.cache_env() == {FORCE: "0", LEAD: "5m", SUBAGENT: "5m"}


def test_an_explicit_caller_can_still_buy_another_ttl() -> None:
    """`env_extra` sits above the default, so this decision stays A/B-measurable."""
    assert claude_cli.cache_env(explicit={LEAD: "1h"})[LEAD] == "1h"


# -- the paths, end to end -------------------------------------------------------------


def test_jarvis_own_headless_calls_buy_the_five_minute_write(fake_claude, tmp_path) -> None:
    claude_cli.run_headless("hi", cwd=tmp_path)
    assert _cache_env(fake_claude.calls[-1]) == {FORCE: "0", LEAD: "5m", SUBAGENT: "5m"}


def test_env_extra_does_not_drop_the_pins(fake_claude, tmp_path) -> None:
    """The tooled callers (the retrieval eval's subject) pass `env_extra` for PATH."""
    claude_cli.run_headless("hi", cwd=tmp_path, env_extra={"PATH": "/usr/bin"})
    assert _cache_env(fake_claude.calls[-1]) == {FORCE: "0", LEAD: "5m", SUBAGENT: "5m"}


def test_a_worker_turn_carries_its_ttls_in_its_environment(fake_claude, tmp_path) -> None:
    """Belt and braces beside `--settings`: asserted with NO settings file passed, so the
    property does not depend on one being written."""
    claude_cli.spawn_turn("hi", cwd=tmp_path, session_id="s-1",
                          outfile=tmp_path / "o.json", errfile=tmp_path / "o.err",
                          cache_ttl="1h", subagent_cache_ttl="5m")
    calls = fake_claude.wait_calls(lambda c: "-p" in c["argv"])
    assert calls, "the fake claude was never invoked"
    assert _cache_env(calls[-1]) == {FORCE: "0", LEAD: "1h", SUBAGENT: "5m"}


def test_a_worker_turn_without_a_stated_ttl_buys_the_five_minute_write(
        fake_claude, tmp_path) -> None:
    """A launch path that forgot to resolve the catalog gets the cheap write, never the
    CLI's own default — the direction a mistake must fail in."""
    claude_cli.spawn_turn("hi", cwd=tmp_path, session_id="s-1",
                          outfile=tmp_path / "o.json", errfile=tmp_path / "o.err")
    calls = fake_claude.wait_calls(lambda c: "-p" in c["argv"])
    assert _cache_env(calls[-1]) == {FORCE: "0", LEAD: "5m", SUBAGENT: "5m"}
