"""The cohort re-measurement script behind the 5m-vs-1h cache decision.

The script exists because the answer MOVES (0.3% of writes before the
`includeGitInstructions` fix, 20.4% in the trailing week after it) and an all-history
average hides that. So the tests that matter are the two ways a reader gets the decision
wrong: the window not actually selecting a cohort, and the trigger being read against
the wrong denominator.

Findings: docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from jarvis import usage

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cache_ttl_cohort.py"


@pytest.fixture()
def cohort(tmp_path, monkeypatch):
    """A transcript tree, plus the script loaded as a module against it."""
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    spec = importlib.util.spec_from_file_location("cache_ttl_cohort", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def session(session_id: str, rows: list[dict]):
        (root / "-proj" / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    return module, session


def row(mid: str, *, write: int = 0, read: int = 0, at: str) -> dict:
    return {"type": "assistant", "timestamp": at,
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 0,
                                  "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": read,
                                  "output_tokens": 0}}}


def _expired(prefix: str, day: str) -> list[dict]:
    """A session whose one boundary is the cache entry EXPIRING."""
    return [row(f"{prefix}1", write=60_000, read=0, at=f"{day}T00:00:00.000Z"),
            row(f"{prefix}2", write=100, read=60_000, at=f"{day}T00:00:02.000Z"),
            row(f"{prefix}3", write=60_000, read=50, at=f"{day}T00:30:00.000Z")]


def _prefix_moved(prefix: str, day: str) -> list[dict]:
    """A session whose one boundary is the PREFIX moving, twelve seconds apart."""
    return [row(f"{prefix}1", write=60_000, read=0, at=f"{day}T00:00:00.000Z"),
            row(f"{prefix}2", write=100, read=60_000, at=f"{day}T00:00:02.000Z"),
            row(f"{prefix}3", write=60_000, read=16_000, at=f"{day}T00:00:14.000Z")]


def test_the_window_selects_a_cohort_rather_than_the_whole_history(cohort, capsys):
    """The point of the script: an old session must not dilute a recent verdict.

    The two sessions differ ONLY in date, and the narrow window must see one of them.
    """
    module, session = cohort
    session("old", _expired("o", "2026-06-01"))
    session("new", _prefix_moved("n", "2026-08-29"))

    module.main(["--since", "2026-08-01", "--until", "2026-09-01"])
    narrow = capsys.readouterr().out
    assert "1 sessions" in narrow
    assert "no TTL helps this" in narrow

    module.main(["--since", "2026-01-01", "--until", "2026-09-01"])
    assert "2 sessions" in capsys.readouterr().out


def test_the_trigger_is_read_against_all_writes_not_against_the_tax(cohort, capsys):
    """The mistake this script was written to stop, asserted as two different numbers.

    The 1-hour premium is paid on every written token, so the deciding ratio is
    W_ttl/cache_write. The TTL's share OF THE TAX has a far smaller denominator and is
    always the larger number; printing only that one reads as "nearly worth switching".
    """
    module, session = cohort
    session("a", _expired("a", "2026-08-29"))
    session("b", _prefix_moved("b", "2026-08-29"))

    module.main(["--since", "2026-08-01", "--until", "2026-09-01"])
    out = capsys.readouterr().out
    assert "TTL share of all writes" in out and "TTL share of the tax" in out
    assert "NOT the trigger" in out
    # Same tokens on each side, so the tax splits 50/50 — while the trigger's ratio
    # cannot, because the non-boundary writes sit in its denominator alone.
    of_tax = float(out.split("TTL share of the tax")[1].split("%")[0])
    of_writes = float(out.split("TTL share of all writes")[1].split("%")[0])
    assert of_tax == 50.0
    assert of_writes == pytest.approx(25.0, abs=0.1)
    assert of_writes < of_tax
    assert "KEEP the 5-minute write" in out


def test_a_cohort_with_no_boundary_decides_nothing_rather_than_zero(cohort, capsys):
    """A quiet window must not read as evidence for the status quo."""
    module, session = cohort
    session("solo", [row("s1", write=500, at="2026-08-29T00:00:00.000Z"),
                     row("s2", write=400, read=500, at="2026-08-29T00:00:05.000Z")])
    module.main(["--since", "2026-08-01", "--until", "2026-09-01"])
    out = capsys.readouterr().out
    assert "nothing to decide on" in out
    assert "KEEP" not in out and "SWITCH" not in out


def test_the_trigger_is_derived_from_the_rate_table_not_typed_in(cohort):
    """If a price moves, the threshold must move with it."""
    module, _ = cohort
    assert module.TRIGGER == pytest.approx(0.75 / 1.90)
