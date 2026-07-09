import json

import pytest

from jarvis.catalog import CatalogError, load_catalog, parse_catalog


def test_minimal_catalog(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"projects": [{"name": "a", "path": str(tmp_path)}]}))
    cat = load_catalog(f)
    assert cat.os.default_model == "sonnet"
    assert cat.projects[0].name == "a"
    assert cat.projects[0].worker.model == "sonnet"
    assert cat.projects[0].worker.permission_mode == "auto"
    assert cat.projects[0].max_concurrent == 5


def test_project_overrides_inherit():
    cat = parse_catalog({
        "os": {"defaults": {"model": "opus", "permission_mode": "auto"}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "model": "haiku",
             "worker": {"permission_mode": "plan"}},
        ],
    })
    assert cat.projects[0].worker.model == "opus"
    assert cat.projects[0].worker.permission_mode == "auto"
    assert cat.projects[1].worker.model == "haiku"
    assert cat.projects[1].worker.permission_mode == "plan"


def test_max_concurrent_config():
    cat = parse_catalog({
        "os": {"defaults": {"max_concurrent": 3}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},                     # inherits fleet default
            {"name": "b", "path": "/tmp/b", "max_concurrent": 8},  # per-project override
        ],
    })
    assert cat.os.default_max_concurrent == 3
    assert cat.projects[0].max_concurrent == 3
    assert cat.projects[1].max_concurrent == 8


@pytest.mark.parametrize("bad,msg", [
    ({}, "projects"),
    ({"projects": []}, "projects"),
    ({"projects": [{"path": "/x"}]}, "name"),
    ({"projects": [{"name": "a"}]}, "path"),
    ({"projects": [{"name": "a", "path": "/x"}, {"name": "a", "path": "/y"}]}, "duplicate"),
    ({"projects": [{"name": "a", "path": "/x", "worker": {"permission_mode": "yolo"}}]}, "permission_mode"),
    ({"projects": [{"name": "a", "path": "/x", "max_concurrent": 0}]}, "max_concurrent"),
])
def test_invalid_catalogs(bad, msg):
    with pytest.raises(CatalogError, match=msg):
        parse_catalog(bad)


def test_missing_file(tmp_path):
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "nope.json")


def test_unknown_project_lookup():
    cat = parse_catalog({"projects": [{"name": "a", "path": "/x"}]})
    with pytest.raises(CatalogError, match="unknown project"):
        cat.project("zzz")
