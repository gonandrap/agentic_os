"""Which instance is this? — the production/development signal behind the UI badge.

The whole point of the badge is that nobody mistakes the production console for the dev
one, so the interesting cases here are the ones where detection is *wrong* in the unsafe
direction: dev dressed up as prod, or a broken check taking the header down with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import paths


@pytest.fixture(autouse=True)
def undeclared(monkeypatch, tmp_path):
    """Decide both variables per test.

    A worker session spawned by the production daemon inherits `PRODUCTION_CODE` (and,
    once the units are redeployed, `JARVIS_ENV`), so a test that leaves them ambient
    would assert against whatever launched pytest.
    """
    monkeypatch.delenv("JARVIS_ENV", raising=False)
    monkeypatch.setenv("PRODUCTION_CODE", str(tmp_path / "nowhere"))


def test_declared_production_wins(monkeypatch):
    """The explicit knob the production units export."""
    monkeypatch.setenv("JARVIS_ENV", "production")
    env, why = paths.deployment_env()
    assert env == paths.PRODUCTION
    assert "JARVIS_ENV" in why


def test_any_other_declared_value_is_not_production(monkeypatch):
    monkeypatch.setenv("JARVIS_ENV", "staging")
    env, why = paths.deployment_env()
    assert env == paths.DEVELOPMENT
    assert "staging" in why


def test_code_inside_the_production_checkout_is_production(monkeypatch, tmp_path):
    """No `JARVIS_ENV` → the code's own location decides, as docs/DEPLOYMENT.md defines
    it. This is what keeps an already-deployed production instance labelled right
    without redeploying it."""
    repo = Path(paths.__file__).resolve().parents[2]
    (tmp_path / "jarvis_os").symlink_to(repo)  # pretend this checkout is the prod one
    monkeypatch.setenv("PRODUCTION_CODE", str(tmp_path))
    env, why = paths.deployment_env()
    assert env == paths.PRODUCTION
    assert str(Path(paths.__file__).resolve().parent) in why


def test_code_outside_the_production_checkout_is_development():
    env, why = paths.deployment_env()
    assert env == paths.DEVELOPMENT
    assert str(Path(paths.__file__).resolve().parent) in why


def test_production_code_dir_defaults_to_the_documented_location(monkeypatch):
    monkeypatch.delenv("PRODUCTION_CODE", raising=False)
    assert paths.production_code_dir() == Path("~/workspace/production/jarvis_os").expanduser()


def test_detection_failure_falls_back_to_development(monkeypatch):
    """A badge must never be the reason a page 500s — and a check that broke must never
    be the reason dev looks like prod."""
    def boom() -> Path:
        raise OSError("no filesystem for you")

    monkeypatch.setattr(paths, "production_code_dir", boom)
    env, why = paths.deployment_env()
    assert env == paths.DEVELOPMENT
    assert why
