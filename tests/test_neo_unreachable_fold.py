"""One outage is one attention row, and the questions it stranded re-ask themselves once Neo answers again"""

from __future__ import annotations

from jarvis import claude_cli, ops
from jarvis import neo as neo_mod
from jarvis.neo_store import MAX_ANSWER_ATTEMPTS, MAX_REVIVALS, NeoStore

OAUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"


def _boom(*a, **k):
    raise claude_cli.ClaudeCliError(OAUTH)


def _ok(*a, **k):
    return neo_mod.parse_verdict('{"escalate": false, "answer": "fine", "reason": "ok"}')


def _strand(neo: NeoStore, kind: str, text: str, wo_id: str = "") -> int:
    """A question whose transport failed on its last attempt"""
    q = neo.ask("proj_a", wo_id, text, kind=kind)
    neo.conn.execute("UPDATE questions SET attempts=? WHERE id=?",
                     (MAX_ANSWER_ATTEMPTS, q["id"]))
    neo_mod.drain_queue(neo, "m", answer=_boom)
    assert neo.get(q["id"])["status"] == "failed"
    return int(q["id"])


def test_one_outage_is_one_attention_row_with_a_count(jarvis_home):
    neo = NeoStore()
    try:
        ids = [_strand(neo, "validation", f"MORPHEUS — PR #{n}") for n in range(4)]
        lone = _strand(neo, "triage", "which priority?")
    finally:
        neo.close()
    rows = [a for a in ops.os_status()["attention"] if a["status"] == "neo_escalated"]
    assert len(rows) == 2
    # the bigger pile first
    folded = rows[0]
    assert folded["count"] == 4
    assert sorted(folded["neo_question_ids"]) == sorted(ids)
    assert "4" in folded["title"] and "could not be reached" in folded["title"]
    assert OAUTH in folded["reason"]
    assert rows[1]["neo_question_id"] == lone and rows[1]["count"] == 1


def test_distinct_escalations_stay_distinct_but_identical_ones_fold(jarvis_home):
    neo = NeoStore()
    try:
        a = neo.ask("proj_a", "", "MORPHEUS — PR #1", kind="validation")
        b = neo.ask("proj_a", "", "MORPHEUS — PR #2", kind="validation")
        c = neo.ask("proj_a", "", "MORPHEUS — PR #1", kind="validation")
        for q in (a, b, c):
            neo.mark(q["id"], "escalated", reason="look at it")
    finally:
        neo.close()
    rows = [r for r in ops.os_status()["attention"] if r["status"] == "neo_escalated"]
    assert [r["count"] for r in rows] == [2, 1]
    assert sorted(rows[0]["neo_question_ids"]) == sorted([a["id"], c["id"]])


def test_neo_answering_again_revives_the_questions_the_outage_stranded(jarvis_home):
    neo = NeoStore()
    try:
        stranded = _strand(neo, "validation", "MORPHEUS — PR #9")
        worker_q = _strand(neo, "question", "CSV or JSON?", wo_id="wo-1")
        neo.ask("proj_a", "", "after re-login", kind="validation")
        results = neo_mod.drain_queue(neo, "m", answer=_ok)
        assert neo.get(stranded)["status"] == "answered"
        assert stranded in [r["question"]["id"] for r in results]
        # a worker's question may be stale by now: it stays with the user
        assert neo.get(worker_q)["status"] == "failed"
    finally:
        neo.close()


def test_revival_is_capped_so_a_broken_transport_cannot_loop(jarvis_home):
    neo = NeoStore()
    try:
        stranded = _strand(neo, "validation", "MORPHEUS — PR #9")
        for _ in range(MAX_REVIVALS + 2):
            assert neo.get(stranded)["status"] == "failed"
            neo.ask("proj_a", "", "a healthy one", kind="triage")
            neo_mod.drain_queue(
                neo, "m", answer=lambda st, q, *a: _boom() if q["id"] == stranded else _ok())
            if neo.get(stranded)["status"] == "queued":
                # revived, and its transport is still broken: spend the fresh budget
                neo.conn.execute("UPDATE questions SET attempts=?, retry_after=NULL "
                                 "WHERE id=?", (MAX_ANSWER_ATTEMPTS, stranded))
                neo_mod.drain_queue(neo, "m", answer=_boom)
        assert neo.get(stranded)["revivals"] == MAX_REVIVALS
        assert neo.get(stranded)["status"] == "failed"
    finally:
        neo.close()
