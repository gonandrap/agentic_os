"""The cohort re-measurement script behind the 5m-vs-1h cache decision (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from jarvis import usage

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cache_ttl_cohort.py"


@pytest.fixture()
def cohort(tmp_path, monkeypatch, jarvis_home):
    """A transcript tree and a registered catalog, plus the script as a module"""
    from jarvis.central_store import CentralStore

    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"os": {"cold_prefix_floor": 5_000},
                                   "projects": []}))
    central = CentralStore()
    try:
        central.set_state("catalog_path", str(catalog))
        central.conn.commit()
    finally:
        central.close()

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
    """A session whose one boundary is the cache entry EXPIRING"""
    return [row(f"{prefix}1", write=60_000, read=0, at=f"{day}T00:00:00.000Z"),
            row(f"{prefix}2", write=100, read=60_000, at=f"{day}T00:00:02.000Z"),
            row(f"{prefix}3", write=60_000, read=50, at=f"{day}T00:30:00.000Z")]


def _prefix_moved(prefix: str, day: str) -> list[dict]:
    """A session whose one boundary is the PREFIX moving, twelve seconds apart"""
    return [row(f"{prefix}1", write=60_000, read=0, at=f"{day}T00:00:00.000Z"),
            row(f"{prefix}2", write=100, read=60_000, at=f"{day}T00:00:02.000Z"),
            row(f"{prefix}3", write=60_000, read=16_000, at=f"{day}T00:00:14.000Z")]


def test_the_window_selects_a_cohort_rather_than_the_whole_history(cohort, capsys):
    """The point of the script: an old session must not dilute a recent verdict"""
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
    """The mistake this script was written to stop, asserted as two different numbers"""
    module, session = cohort
    session("a", _expired("a", "2026-08-29"))
    session("b", _prefix_moved("b", "2026-08-29"))

    module.main(["--since", "2026-08-01", "--until", "2026-09-01"])
    out = capsys.readouterr().out
    assert "TTL share of all writes" in out and "TTL share of the tax" in out
    assert "NOT the trigger" in out
    # Same tokens on each side, so the tax splits 50/50
    of_tax = float(out.split("TTL share of the tax")[1].split("%")[0])
    of_writes = float(out.split("TTL share of all writes")[1].split("%")[0])
    assert of_tax == 50.0
    assert of_writes == pytest.approx(25.0, abs=0.1)
    assert of_writes < of_tax
    assert "KEEP the 5-minute write" in out


def test_a_cohort_with_no_boundary_decides_nothing_rather_than_zero(cohort, capsys):
    """A quiet window must not read as evidence for the status quo"""
    module, session = cohort
    session("solo", [row("s1", write=500, at="2026-08-29T00:00:00.000Z"),
                     row("s2", write=400, read=500, at="2026-08-29T00:00:05.000Z")])
    module.main(["--since", "2026-08-01", "--until", "2026-09-01"])
    out = capsys.readouterr().out
    assert "nothing to decide on" in out
    assert "KEEP" not in out and "SWITCH" not in out


def test_the_trigger_is_derived_from_the_rate_table_not_typed_in(cohort):
    """If a price moves, the threshold must move with it"""
    module, _ = cohort
    assert module.TRIGGER == pytest.approx(0.75 / 1.90)


def test_no_catalog_is_an_error_not_a_default(cohort, capsys):
    """With the catalog unregistered the script must stop, not classify against 5,000"""
    from jarvis.central_store import CentralStore
    from jarvis.ops import OpsError

    module, session = cohort
    session("s", _expired("s", "2026-08-29"))

    central = CentralStore()
    try:
        central.set_state("catalog_path", "")
        central.conn.commit()
    finally:
        central.close()

    with pytest.raises(OpsError, match="no catalog registered"):
        module.main(["--since", "2026-08-01", "--until", "2026-09-01"])
