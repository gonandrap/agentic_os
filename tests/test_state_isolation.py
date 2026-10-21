"""The test suite must never touch the user's live OS state"""

from __future__ import annotations

from pathlib import Path

from jarvis import paths
from jarvis.central_store import CentralStore
from jarvis.neo_store import NeoStore


def test_jarvis_home_is_scratch_even_without_the_fixture(tmp_path_factory):
    home = paths.jarvis_home()
    assert home != Path("~/.jarvis").expanduser()
    assert home.is_relative_to(tmp_path_factory.getbasetemp())


def test_stores_default_into_the_scratch_home():
    """The stores resolve their paths at construction time"""
    for store_cls in (CentralStore, NeoStore):
        store = store_cls()
        try:
            assert store.db_path.is_relative_to(paths.jarvis_home())
        finally:
            store.close()


def test_the_per_test_fixture_still_narrows_the_home(jarvis_home):
    assert paths.jarvis_home() == jarvis_home
