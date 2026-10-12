"""The `cache-1h-dispatched` alarm and `INV-CACHE-TTL-TRIGGER` both presume the fleet
buys the five-minute write — the first calls a 1h write a BREACH, the second says the
hour has started paying. A fleet whose leads buy the hour on purpose must hear neither.
The foreign alarm is untouched: a person's own `claude` is still their own. Findings:
docs/superpowers/findings/2026-10-11-the-lead-buys-the-hour-the-crew-does-not.md
"""

from __future__ import annotations

from jarvis import inspection, invariants
from jarvis.catalog import InspectConfig, parse_catalog

DAY = 86_400.0
NOW = 1_789_000_000.0


def _found(*, dispatched=0, foreign=0):
    return [inspection.HourWrites(session_id="s-mixed", directory="-somewhere",
                                  dispatched=dispatched, foreign=foreign,
                                  first_ts=NOW - DAY, last_ts=NOW - DAY)]


def test_a_fleet_that_bought_the_hour_is_not_told_it_breached_it():
    found = _found(dispatched=9_000_000, foreign=10_000_000)
    kinds = {a.kind for a in inspection.hour_alarms(found, InspectConfig(), days=7,
                                                    hour_bought=True)}
    assert inspection.CACHE_1H_DISPATCHED_ALARM not in kinds
    assert inspection.CACHE_1H_FOREIGN_ALARM in kinds


def test_a_fleet_on_the_five_minute_write_still_is():
    found = _found(dispatched=9_000_000)
    kinds = {a.kind for a in inspection.hour_alarms(found, InspectConfig(), days=7)}
    assert inspection.CACHE_1H_DISPATCHED_ALARM in kinds


def test_whether_the_fleet_buys_the_hour_is_read_off_the_catalog():
    assert invariants.fleet_buys_the_hour(parse_catalog(
        {"projects": [{"name": "a", "path": "/x"}]}))
    assert not invariants.fleet_buys_the_hour(parse_catalog(
        {"os": {"defaults": {"cache_ttl": "5m"}},
         "projects": [{"name": "a", "path": "/x"}]}))
    assert invariants.fleet_buys_the_hour(parse_catalog(
        {"os": {"defaults": {"cache_ttl": "5m"}},
         "projects": [{"name": "a", "path": "/x"},
                      {"name": "b", "path": "/y", "worker": {"cache_ttl": "1h"}}]}))


def test_the_ttl_trigger_is_silent_when_the_hour_is_already_bought(monkeypatch):
    """The whole check is a question about switching TO the hour."""
    asked = []
    monkeypatch.setattr(invariants, "_cache_health",
                        lambda: asked.append(True) or None)
    monkeypatch.setattr(invariants, "_fleet_catalog",
                        lambda: parse_catalog({"projects": [{"name": "a", "path": "/x"}]}))
    assert list(invariants.check_cache_ttl_trigger()) == []
    assert not asked
