"""The shared clock fixture (docs/superpowers/specs/2026-10-15-no-test-reads-the-real-clock.md §1)"""

from __future__ import annotations

import pytest

from jarvis import db, testing
from jarvis.project_store import ProjectStore

# The real `db.now`, captured before any fixture can patch it
_REAL_NOW = db.now


def test_the_epoch_is_a_constant() -> None:
    assert testing.TEST_EPOCH == 1760000000.0


def test_frozen_reads_never_move(clock) -> None:
    a = db.now()
    b = db.now()
    assert a == b == 1760000000.0


def test_frozen_reaches_the_store_call_sites(clock, project) -> None:
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("stamped by the fake clock")
        assert float(wo["created_at"]) == 1760000000.0
    finally:
        store.close()


def test_advance_takes_seconds(clock) -> None:
    clock.advance(90)
    assert db.now() == 1760000090.0


def test_advance_refuses_no_argument(clock) -> None:
    with pytest.raises(TypeError):
        clock.advance()


def test_advance_refuses_minutes(clock) -> None:
    with pytest.raises(TypeError):
        clock.advance(minutes=61)


def test_ticking_reads_are_strictly_later(clock) -> None:
    clock.ticking()
    assert db.now() < db.now() < db.now()


def test_ticking_orders_rows_in_the_store(clock, project) -> None:
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("two turns in order")
        clock.ticking()
        first = store.create_turn(wo["id"], "dispatch", "do the thing")
        second = store.create_turn(wo["id"], "message", "and the next thing")
        assert float(second["started_at"]) > float(first["started_at"])
    finally:
        store.close()


def test_moved_is_scoped(clock) -> None:
    with clock.moved(3600):
        assert db.now() == 1760000000.0 + 3600
    assert db.now() == 1760000000.0


def test_moved_restores_the_clock_when_the_block_raises(clock) -> None:
    with pytest.raises(RuntimeError):
        with clock.moved(3600):
            raise RuntimeError("the block blew up")
    assert db.now() == 1760000000.0


def test_the_fixture_is_not_autouse() -> None:
    assert db.now is _REAL_NOW


def test_requesting_the_fixture_patches_db_now(clock) -> None:
    assert db.now is not _REAL_NOW
