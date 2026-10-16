"""Every sentence that tells a worker how long its cache lives, how long one call may
block and how often to collect a background task is derived from the TTL its lead
bought — the brief, the standing section, OPERATION.md and the hooks all read one
arithmetic (`claude_cli.cache_rhythm`), so none of them can quote the five-minute
numbers to a worker that bought the hour. Findings:
docs/superpowers/findings/2026-10-11-the-lead-buys-the-hour-the-crew-does-not.md
"""

from __future__ import annotations

from pathlib import Path

from jarvis import claude_cli, hooks, worker_brief
from jarvis.bootstrap import render_operation_md
from jarvis.catalog import ProjectSpec, WorkerDefaults


def test_the_rhythm_arithmetic_for_both_ttls():
    five = claude_cli.cache_rhythm("5m")
    assert (five.seconds, five.minutes, five.cap_seconds, five.poll_every,
            five.write_rate) == (300, 5, 240, "3-4 minutes", "1.25x")
    hour = claude_cli.cache_rhythm("1h")
    assert (hour.seconds, hour.minutes, hour.cap_seconds, hour.poll_every,
            hour.write_rate) == (3600, 60, 2880, "30-40 minutes", "2x")


def test_an_unknown_ttl_reads_as_the_short_one():
    assert claude_cli.cache_rhythm(None) == claude_cli.cache_rhythm("5m")
    assert claude_cli.cache_rhythm("2h") == claude_cli.cache_rhythm("5m")


def test_the_hooks_copy_of_the_arithmetic_agrees_with_claude_cli():
    """`hooks` spells the arithmetic rather than importing `claude_cli` (import cost on
    every Bash call); this is what stops the two drifting."""
    assert hooks.CACHE_TTL_ENV == claude_cli.CACHE_TTL_ENV
    for ttl in ("5m", "1h"):
        env = {claude_cli.CACHE_TTL_ENV: ttl}
        rhythm = claude_cli.cache_rhythm(ttl)
        assert hooks.cache_ttl_seconds(env) == rhythm.seconds
        assert hooks.foreground_cap_seconds(env) == rhythm.cap_seconds
        assert hooks.poll_rhythm(env) == rhythm.poll_every


def _turn_bullet(cache_ttl: str) -> str:
    core = worker_brief.core_contract("wo-ttl01", "t", "p1", has_knowledge=False,
                                      cache_ttl=cache_ttl)
    bullets = [line for line in core if line.startswith("- **A turn is one-shot")]
    assert len(bullets) == 1
    return bullets[0]


def test_the_core_contract_quotes_the_numbers_of_the_ttl_the_lead_bought():
    hour = _turn_bullet("1h")
    assert "30-40 minutes" in hour and "2880 seconds" in hour and "60 minutes" in hour
    assert "3-4 minutes" not in hour and "240" not in hour
    five = _turn_bullet("5m")
    assert "3-4 minutes" in five and "240 seconds" in five and "5 minutes" in five


def test_the_standing_section_quotes_the_same_numbers():
    hour = worker_brief.render_section("record", wo_id="wo-ttl01", project="p1",
                                       cache_ttl="1h")
    assert "30-40 minutes" in hour and "2880 seconds" in hour
    assert "3600 seconds" in hour and "2x write rate" in hour
    assert "3-4 minutes" not in hour
    five = worker_brief.render_section("record", wo_id="wo-ttl01", project="p1")
    assert "3-4 minutes" in five and "240 seconds" in five and "1.25x" in five


def test_operation_md_is_rendered_for_the_projects_own_ttl(tmp_path: Path):
    hour = render_operation_md(ProjectSpec(name="p", path=tmp_path,
                                           worker=WorkerDefaults(cache_ttl="1h")))
    assert "30-40 minutes" in hour and "2880 seconds" in hour and "60 minutes" in hour
    assert "3-4 minutes" not in hour
    five = render_operation_md(ProjectSpec(name="p", path=tmp_path,
                                           worker=WorkerDefaults(cache_ttl="5m")))
    assert "3-4 minutes" in five and "240 seconds" in five and "5 minutes" in five
