"""Claude Code's 'hasn't heard from you' nudge is off in every worker (#1369)"""

from __future__ import annotations

import json

from jarvis import dispatch
from jarvis.catalog import ProjectSpec, WorkerDefaults


def test_every_worker_is_launched_with_the_silent_turn_reminder_off(project, jarvis_home):
    spec = ProjectSpec(name="proj_a", path=project, description="",
                       worker=WorkerDefaults())
    out = dispatch._write_worker_settings(spec, {"id": "wo-nudge1", "title": "t"})

    assert json.loads(out.read_text())["env"][dispatch.SILENT_TURN_REMINDER_ENV] == "0"
