import sqlite3

from jarvis import invariants


def test_doctor_flags_a_deadlocking_libsqlite3(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.51.1")
    found = [v for v in invariants.check_os() if v.invariant == "INV-SQLITE-VERSION"]
    assert len(found) == 1 and found[0].level == "critical"
    assert "3.51.1" in found[0].detail and "3.51.2" in found[0].detail

    monkeypatch.setattr(sqlite3, "sqlite_version", "3.51.2")
    assert list(invariants.check_sqlite_version()) == []
