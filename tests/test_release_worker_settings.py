"""A release worker may run the ship script `auto` mode would veto — issue #1206"""

import json

from jarvis import db, release
from jarvis.catalog import ProjectSpec
from jarvis.gates import GateConfig
from jarvis.dispatch import RELEASE_ALLOW_RULES, _write_worker_settings


def _settings(project, wo_id: str, *, batch: list[str] | None) -> dict:
    spec = ProjectSpec(name="proj_a", path=project, description="",
                       gates=GateConfig(enabled=frozenset({"release"})))
    wo = {"id": wo_id,
          **({"metadata": db.to_json({release.BATCH_KEY: batch})}
             if batch is not None else {})}
    return json.loads(_write_worker_settings(spec, wo).read_text())


def test_a_release_order_may_run_the_ship_script(project, jarvis_home):
    allow = _settings(project, "wo-rel1234",
                      batch=["https://github.com/acme/proj/issues/1"])["permissions"]["allow"]
    assert "Bash(scripts/shipit.sh:*)" in allow
    assert RELEASE_ALLOW_RULES == ("Bash(scripts/shipit.sh:*)",)


def test_the_rule_is_the_script_and_nothing_wider(project, jarvis_home):
    """A broad `Bash(:*)` would hand a release worker every command there is"""
    settings = _settings(project, "wo-rel5678", batch=[])
    ordinary = _settings(project, "wo-ord5678", batch=None)["permissions"]["allow"]
    bash = [r for r in settings["permissions"]["allow"] if r.startswith("Bash(")]
    assert [r for r in bash if r not in ordinary] == ["Bash(scripts/shipit.sh:*)"]
    # The OS's own gate is what authorises the release, and it still mediates
    assert "release" in settings["env"]["JARVIS_GATES"]


def test_an_ordinary_order_gets_no_such_rule(project, jarvis_home):
    allow = _settings(project, "wo-ord1234", batch=None)["permissions"]["allow"]
    assert not [r for r in allow if "shipit" in r]
