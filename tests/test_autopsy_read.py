"""The read side of the seal: one chokepoint, and every surface saying which it read.

§4 of docs/specs/2026-09-27-order-autopsy-durability.md. `autopsy.anatomy_for` is the one
place that chooses between a sealed reading and a fresh one, and standing rule (a) — a
persisted figure and a freshly derived one must never disagree on the same order — is a
claim about a SURFACE, so it is proven here and nowhere else: one order, `ops.inspect_report`
run before and after the seal with the transcript untouched, equal once provenance is popped.

Rule (b) is the other half: an order whose autopsy was never persisted renders as *not
recorded*, never as zero, and the three states *not recorded*, *no transcript* and
*genuinely zero* stay distinguishable at all four surfaces.
"""

from __future__ import annotations

import ast
import inspect as inspect_mod
import json
from pathlib import Path

import pytest

from jarvis import autopsy, catalog, inspection, live, ops, supervisor
from jarvis.catalog import InspectConfig
from jarvis.project_store import ProjectStore

from tests.test_inspection import (  # noqa: F401
    FIXTURE_ROOT,
    FIXTURE_SESSION,
    assistant_row,
    prompt_row,
    real_session,
    started,
    tool_rows,
    write_transcript,
)

SESSION = "sess-read-side"


def an_order(session: str = SESSION, *, title: str = "an order that finished") -> dict:
    """One settled work order with `session` on it, and the row itself."""
    wo = ops.create_work_order("proj_a", title)
    name, path, _row = ops.find_work_order(wo["id"])
    store = ProjectStore(path)
    try:
        store.update_work_order(wo["id"], session_id=session, status="completed")
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def rows() -> list[dict]:
    """Three cache writes either side of the default 20,000 write floor, and one join.

    NO tool parameters: a `normal` seal carries none by design and §6 is what makes them
    reachable, so an order that ran one would differ on `params` alone and say nothing
    about rule (a). That difference is what `provenance["params"]` exists to declare.
    """
    return [
        prompt_row(0, "You are the worker agent for wo-1"),
        assistant_row(10, "m1", write=50_000),
        *tool_rows(20, 90, "t1", "Agent"),
        assistant_row(100, "m2", write=30_000),
        assistant_row(110, "m3", write=8_000),
    ]


def seal_it(wo_id: str) -> dict:
    name, path, row = ops.find_work_order(wo_id)
    return autopsy.seal(name, path, row)


def reread(wo_id: str) -> dict:
    return ops.find_work_order(wo_id)[2]


def drop_transcript(tmp_path, session: str = SESSION) -> None:
    (tmp_path / "projects" / "-proj" / f"{session}.jsonl").unlink()


# -- rule (a) at a surface -------------------------------------------------------------


def test_the_anatomy_report_says_exactly_the_same_thing_once_the_order_is_sealed(
        started, write_transcript):
    """Standing rule (a), and only this child can prove it: the same order, the same
    transcript, read fresh and then read from its seal, must not disagree by one key."""
    write_transcript(SESSION, rows())
    wo = an_order()

    before = ops.inspect_report(wo["id"])
    seal_it(wo["id"])
    after = ops.inspect_report(wo["id"])

    assert [u.pop("provenance")["source"] for u in before["units"]] == ["derived"]
    assert [u.pop("provenance")["source"] for u in after["units"]] == ["sealed"]
    assert after == before


def test_the_context_ledger_says_exactly_the_same_thing_once_the_order_is_sealed(
        started, write_transcript):
    write_transcript(SESSION, rows())
    wo = an_order()

    before = ops.context_report(wo["id"])
    seal_it(wo["id"])
    after = ops.context_report(wo["id"])

    assert before.pop("provenance")["source"] == "derived"
    assert after.pop("provenance")["source"] == "sealed"
    assert after == before


def test_a_sealed_order_is_read_from_its_seal_once_the_transcript_is_gone(
        started, write_transcript, tmp_path):
    """The whole point of the seal at a surface: the transcript is pruned and the report
    still states the clock it ran, rather than reporting `found: false`."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    drop_transcript(tmp_path)

    (unit,) = ops.inspect_report(wo["id"])["units"]

    assert unit["found"] is True and unit["provenance"]["source"] == "sealed"
    assert [w["written"] for w in unit["writes"]] == [50_000, 30_000]


def test_a_live_read_is_forced_past_the_seal_the_way_a_bill_forces_one(
        started, write_transcript):
    """`live=True` is `bill.build(..., live=True)`'s parameter and its justification: the
    thing a test comparing a seal against a fresh reading needs."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    row = reread(wo["id"])
    cfg = InspectConfig()

    sealed, sealed_prov = autopsy.anatomy_for(row, cfg, spans=[])
    fresh, fresh_prov = autopsy.anatomy_for(row, cfg, spans=[], live=True)

    assert (sealed_prov["source"], fresh_prov["source"]) == ("sealed", "derived")
    assert fresh.as_dict() == sealed.as_dict()


# -- rule (b) at all four surfaces -----------------------------------------------------


def test_the_anatomy_of_an_order_with_no_session_is_not_recorded_and_never_zero(started):
    """The no-session path `ops.inspect_report` has always had must survive the
    chokepoint: no session, no seal and no transcript are three gaps, not three zeros."""
    wo = an_order(session="")

    (unit,) = ops.inspect_report(wo["id"])["units"]

    assert unit["found"] is False and unit["turns"] == []
    assert unit["provenance"]["source"] == "derived"
    assert autopsy.NOT_RECORDED_NOTE in unit["provenance"]["note"]


def test_the_context_ledger_of_an_order_with_no_session_is_not_recorded(started):
    wo = an_order(session="")

    res = ops.context_report(wo["id"])

    assert res["recorded"] is False and res["note"] == ops.NOT_RECORDED
    assert autopsy.NOT_RECORDED_NOTE in res["provenance"]["note"]


def test_the_diagnosis_of_an_order_with_no_session_is_not_recorded(started):
    wo = an_order(session="")

    res = ops.diagnose(wo["id"])

    assert res["holds"]["unexplained"]["seconds"] is None
    assert autopsy.NOT_RECORDED_NOTE in res["holds"]["provenance"]["note"]


def test_the_evidence_packet_of_an_order_with_no_session_is_not_recorded(started):
    wo = an_order(session="")

    lines = supervisor._session_lines(reread(wo["id"]), InspectConfig())

    assert lines == ["(the work order has no session)"]
    assert not any("0s wall" in line for line in lines)


# -- the floors, and the sentence a short list must never tell -------------------------


def test_a_write_floor_above_the_sealed_one_is_answered_by_the_seal_and_filtered(
        started, write_transcript, tmp_path):
    """Asked for MORE than the seal was taken at, the seal can answer: it holds every
    write the question wants, and the ones below the requested floor come out."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    drop_transcript(tmp_path)

    (unit,) = ops.inspect_report(wo["id"], write_floor=40_000)["units"]

    assert unit["provenance"]["source"] == "sealed"
    assert unit["provenance"]["floor_note"] == ""
    assert unit["provenance"]["write_floor"] == 40_000
    assert [w["written"] for w in unit["writes"]] == [50_000]


def test_a_write_floor_below_the_sealed_one_derives_while_the_transcript_is_there(
        started, write_transcript):
    """A seal taken at 20,000 holds no write below it, ever — so the transcript answers
    while it exists, and the reading is the full one."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])

    (unit,) = ops.inspect_report(wo["id"], write_floor=5_000)["units"]

    assert unit["provenance"]["source"] == "derived"
    assert unit["provenance"]["floor_note"] == ""
    assert [w["written"] for w in unit["writes"]] == [50_000, 30_000, 8_000]


def test_a_write_floor_below_the_sealed_one_names_the_sealed_floor_once_it_must_answer(
        started, write_transcript, tmp_path):
    """The sentence a short list must never be allowed to tell. The seal holds nothing
    below 20,000 and the transcript is gone, so the report NAMES the sealed floor rather
    than handing back two writes that read as "there were none under 20,000"."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    drop_transcript(tmp_path)

    (unit,) = ops.inspect_report(wo["id"], write_floor=5_000)["units"]

    assert unit["provenance"]["source"] == "sealed"
    assert "20,000" in unit["provenance"]["floor_note"]
    # Reported at the floor it can actually speak for, never at the one asked for.
    assert unit["provenance"]["write_floor"] == 20_000


def test_a_join_floor_above_the_sealed_one_is_answered_by_the_seal(
        started, write_transcript, tmp_path):
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    drop_transcript(tmp_path)

    (unit,) = ops.inspect_report(wo["id"], join_floor=60)["units"]

    assert unit["provenance"]["source"] == "sealed"
    assert unit["provenance"]["floor_note"] == ""
    assert unit["provenance"]["join_floor"] == 60
    assert [s["name"] for s in unit["joins"]] == ["Agent"]


def test_a_join_floor_below_the_sealed_one_derives_while_the_transcript_is_there(
        started, write_transcript):
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])

    (unit,) = ops.inspect_report(wo["id"], join_floor=5)["units"]

    assert unit["provenance"]["source"] == "derived"
    assert unit["provenance"]["join_floor"] == 5


def test_a_join_floor_below_the_sealed_one_names_the_sealed_floor_once_it_must_answer(
        started, write_transcript, tmp_path):
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    drop_transcript(tmp_path)

    (unit,) = ops.inspect_report(wo["id"], join_floor=5)["units"]

    assert unit["provenance"]["source"] == "sealed"
    assert "30s" in unit["provenance"]["floor_note"]
    assert unit["provenance"]["join_floor"] == 30


# -- the three-state level rendering ---------------------------------------------------


def test_the_three_autopsy_levels_render_three_different_sentences():
    """`full` is unreachable until §6 and the rendering must already carry it, so the
    three states are three strings and no surface can collapse two of them."""
    said = {autopsy.autopsy_level_note(level)
            for level in (autopsy.UNKNOWN, autopsy.NORMAL, autopsy.FULL)}

    assert len(said) == 3
    assert all(said)


def test_a_seal_taken_at_full_says_so_even_though_nothing_writes_one_yet(
        started, write_transcript):
    """Hand-built, because `full` is §6's to make reachable: the read side must already
    tell the three levels apart rather than hard-coding two."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    name, path, row = ops.find_work_order(wo["id"])
    payload = autopsy.unseal(row)
    payload["autopsy_level"] = autopsy.FULL
    store = ProjectStore(path)
    try:
        store.seal_autopsy(row["id"], json.dumps(payload))
    finally:
        store.close()

    _anatomy, prov = autopsy.anatomy_for(reread(wo["id"]), InspectConfig(), spans=[])

    assert prov["level"] == autopsy.FULL
    assert prov["level_note"] == autopsy.autopsy_level_note(autopsy.FULL)


def test_a_seal_written_before_the_level_existed_reads_as_the_third_state(
        started, write_transcript):
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    name, path, row = ops.find_work_order(wo["id"])
    payload = autopsy.unseal(row)
    payload.pop("autopsy_level")
    store = ProjectStore(path)
    try:
        store.seal_autopsy(row["id"], json.dumps(payload))
    finally:
        store.close()

    _anatomy, prov = autopsy.anatomy_for(reread(wo["id"]), InspectConfig(), spans=[])

    assert prov["level"] == autopsy.UNKNOWN
    assert prov["level_note"] == autopsy.autopsy_level_note(autopsy.UNKNOWN)


# -- the two readers that must NOT prefer a seal ---------------------------------------


def test_the_live_no_transcript_frame_points_at_jarvis_inspect_and_reads_no_seal(
        started, write_transcript, tmp_path):
    """§7: `live_report` is a byte-cursor reader of one file and the parent spec forbids
    persistence there, so a sealed order's live frame still says there is nothing to read
    — and now names the command that CAN answer."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    drop_transcript(tmp_path)

    frame = ops.live_report(wo["id"])

    assert frame["state"] == live.NO_TRANSCRIPT and frame["found"] is False
    assert "jarvis inspect" in frame["note"]
    assert frame.get("provenance") is None


def test_live_alarms_still_prefers_no_seal(started, write_transcript, tmp_path,
                                           monkeypatch):
    """It reads at a deliberately different floor (`alarm_write_tokens`) and its subject
    is a RUNNING session, which has no seal. Asserted so that widening the AST pin cannot
    quietly change it."""
    write_transcript(SESSION, rows())
    wo = an_order()
    seal_it(wo["id"])
    drop_transcript(tmp_path)
    reads: list[str] = []
    real = inspection.read_session
    monkeypatch.setattr(inspection, "read_session",
                        lambda session, *a, **k: (reads.append(session),
                                                  real(session, *a, **k))[1])

    inspection.live_alarms(SESSION, catalog.InspectConfig(), dispatched=0.0)

    assert reads == [SESSION], "live_alarms derives, and never from a seal"


# -- the AST pin -----------------------------------------------------------------------

#: The modules a read of a session anatomy is pinned in. `inspection.live_alarms` calls
#: `inspection.read_session` directly and is EXEMPT: it reads at `alarm_write_tokens`
#: rather than `report_write_floor`, so a seal taken at the report floor cannot answer
#: it, and its subject is a running session, which has no seal. Whoever widens this scope
#: to `inspection.py` must whitelist it.
PINNED = ("ops.py", "supervisor.py")


def anatomy_reads(tree: ast.AST) -> list[int]:
    """Every `inspection.read_session(...)` in one tree, by line.

    Keyed on the MODULE QUALIFIER and not on the attribute alone: `usage.read_session` is
    a DIFFERENT function of the same name on the bill path (`ops.py`, `bill.py`) and must
    pass.
    """
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_session"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "inspection"]


def test_every_anatomy_read_in_ops_and_supervisor_goes_through_the_chokepoint():
    """THE GUARD. Nothing at the call site distinguishes a read that prefers the seal
    from one that cannot, so the rule is checked on the source: `ops` and `supervisor`
    reach an anatomy through `autopsy.anatomy_for`, which is the one place that chooses.
    """
    body, first = inspect_mod.getsourcelines(autopsy.anatomy_for)
    allowed = range(first, first + len(body))
    root = Path(inspection.__file__).parent
    offenders = []

    for path in sorted(root.glob("*.py")):
        if path.name not in PINNED:
            continue
        for line in anatomy_reads(ast.parse(path.read_text())):
            offenders.append(f"{path.name}:{line}")

    assert offenders == [], (
        "read an anatomy through `autopsy.anatomy_for`, so the seal is preferred and the "
        "surface can say which reading answered")
    # The whitelist is a line range in `autopsy.py`, the shape
    # `test_only_db_write_transaction_opens_a_transaction` uses: the chokepoint's own
    # derive must stay a derive.
    assert anatomy_reads(ast.parse(Path(autopsy.__file__).read_text())) and all(
        line in allowed or line in _seal_lines()
        for line in anatomy_reads(ast.parse(Path(autopsy.__file__).read_text())))


def _seal_lines() -> range:
    body, first = inspect_mod.getsourcelines(autopsy.seal)
    return range(first, first + len(body))


def test_the_anatomy_pin_would_catch_the_move_it_forbids():
    """A guard nobody has ever seen fail is a guard nobody knows works. The same walk,
    over source that does the forbidden thing — and over the bill path's namesake, which
    must go on passing."""
    forbidden = ast.parse("from . import inspection\n"
                          "def unit(wo, cfg):\n"
                          "    return inspection.read_session(wo['session_id'], cfg)\n")
    allowed = ast.parse("from . import usage as usage_mod\n"
                        "def bill(session):\n"
                        "    return usage_mod.read_session(session)\n")

    assert anatomy_reads(forbidden) == [3]
    assert anatomy_reads(allowed) == []
