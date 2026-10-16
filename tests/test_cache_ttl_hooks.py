"""The foreground cap, the long-seat refusal and the polling rhythm follow the lead's
prompt-cache TTL. Under the five-minute write everything here is exactly what
`tests/test_background_refusal.py` pins; under the hour the cap stretches, a long seat
may be JOINED in the foreground (one blocking call, no request in flight), and the
rhythm the advice names stretches with it. Findings:
docs/superpowers/findings/2026-10-11-the-lead-buys-the-hour-the-crew-does-not.md
"""

from __future__ import annotations

from jarvis import claude_cli, hooks

HEADLESS = {"JARVIS_WO_ID": "wo-ttl01",
            claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_HEADLESS}
HOUR = {**HEADLESS, claude_cli.CACHE_TTL_ENV: "1h"}


def _fg(command: str, env: dict, **tool_input):
    return hooks.long_foreground_decision(
        {"tool_name": "Bash", "tool_input": {"command": command, **tool_input}}, env)


def _seat(subagent_type: str, env: dict):
    return hooks.long_foreground_decision(
        {"tool_name": "Agent",
         "tool_input": {"subagent_type": subagent_type, "prompt": "do it"}}, env)


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_foreground_cap_is_sized_from_the_ttl():
    assert hooks.cache_ttl_seconds(HEADLESS) == 300
    assert hooks.cache_ttl_seconds(HOUR) == 3600
    assert hooks.foreground_cap_seconds(HEADLESS) == 240
    assert hooks.foreground_cap_seconds(HOUR) == 2880


def test_an_unknown_ttl_value_falls_back_to_the_short_cap():
    """A worker dispatched before the key existed, or a garbled value, gets the
    conservative cap: the direction a mistake must fail in."""
    assert hooks.cache_ttl_seconds({**HEADLESS, claude_cli.CACHE_TTL_ENV: "2h"}) == 300


def test_under_the_hour_a_sleep_inside_the_cap_is_allowed_and_one_past_it_refused():
    assert _fg("sleep 1800", HOUR) is None
    assert _fg("timeout 2400 bash -c 'while :; do sleep 30; done'", HOUR) is None
    result = _fg("sleep 3000", HOUR)
    assert _decision(result) == "deny"
    assert "2880" in _reason(result)
    assert "60 minutes" in _reason(result)
    assert "3-4 minutes" not in _reason(result)


def test_under_the_five_minute_write_the_short_cap_still_holds():
    result = _fg("sleep 300", HEADLESS)
    assert _decision(result) == "deny"
    assert "240" in _reason(result)
    assert "5 minutes" in _reason(result)


def test_under_the_hour_a_long_seat_may_be_joined_in_the_foreground():
    for seat in hooks.LONG_SEATS:
        assert _seat(seat, HOUR) is None, seat
        assert _decision(_seat(seat, HEADLESS)) == "deny", seat


def test_the_rhythm_in_the_advice_follows_the_ttl():
    assert hooks.poll_rhythm(HEADLESS) == "3-4 minutes"
    assert hooks.poll_rhythm(HOUR) == "30-40 minutes"
    assert "30-40 minutes" in _reason(_fg("gh run watch", HOUR))
    assert "3-4 minutes" in _reason(_fg("gh run watch", HEADLESS))
