"""A boundary is a TTL expiry only if the entry the previous call wrote had actually
lapsed — and that entry lived an HOUR when the call bought the hour. Judged against a
flat five minutes, every warm-but-moved prefix in a 1h session eleven minutes after its
last call would be booked as an expiry, and `INV-CACHE-TTL-TRIGGER` would read a fleet
that bought the hour as one that needs to. Mirrors `inspection.classify_writes`, which
already keys the TTL on the previous call's 1h split. Findings:
docs/superpowers/findings/2026-10-11-the-lead-buys-the-hour-the-crew-does-not.md
"""

from __future__ import annotations

import json

import pytest

from jarvis import usage

FLOOR = 5_000


def _row(mid: str, *, write: int, read: int, at: str, ttl_1h: int = 0) -> dict:
    usage_obj: dict = {"input_tokens": 0, "cache_creation_input_tokens": write,
                       "cache_read_input_tokens": read, "output_tokens": 1}
    if ttl_1h:
        usage_obj["cache_creation"] = {"ephemeral_1h_input_tokens": ttl_1h,
                                       "ephemeral_5m_input_tokens": 0}
    return {"type": "assistant", "timestamp": at,
            "message": {"id": mid, "model": "claude-opus-5", "usage": usage_obj,
                        "stop_reason": "end_turn"}}


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict]):
        project_dir = root / "-proj"
        project_dir.mkdir(exist_ok=True)
        (project_dir / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    return write


def test_eleven_minutes_after_a_one_hour_write_is_the_prefix_moving(transcripts):
    transcripts("s1", [
        _row("m1", write=9_000, read=0, at="2026-10-11T00:00:00.000Z", ttl_1h=9_000),
        _row("m2", write=100, read=9_000, at="2026-10-11T00:00:02.000Z", ttl_1h=100),
        _row("m3", write=9_000, read=10, at="2026-10-11T00:11:00.000Z", ttl_1h=9_000),
    ])
    total = usage.read_session("s1", FLOOR).total
    assert total.boundaries_ttl == 0
    assert total.rewrite_prefix_write == 9_000


def test_an_hour_and_a_minute_after_a_one_hour_write_is_an_expiry(transcripts):
    transcripts("s2", [
        _row("m1", write=9_000, read=0, at="2026-10-11T00:00:00.000Z", ttl_1h=9_000),
        _row("m2", write=100, read=9_000, at="2026-10-11T00:00:02.000Z", ttl_1h=100),
        _row("m3", write=9_000, read=10, at="2026-10-11T01:01:00.000Z", ttl_1h=9_000),
    ])
    total = usage.read_session("s2", FLOOR).total
    assert total.boundaries_ttl == 1
    assert total.rewrite_ttl_write == 9_000


def test_a_five_minute_write_still_expires_at_five_minutes(transcripts):
    transcripts("s3", [
        _row("m1", write=9_000, read=0, at="2026-10-11T00:00:00.000Z"),
        _row("m2", write=100, read=9_000, at="2026-10-11T00:00:02.000Z"),
        _row("m3", write=9_000, read=10, at="2026-10-11T00:11:00.000Z"),
    ])
    assert usage.read_session("s3", FLOOR).total.boundaries_ttl == 1
