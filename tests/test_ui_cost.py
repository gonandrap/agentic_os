"""The dashboard's spend surfaces: the `/cost` page and the per-work-order line.

`jarvis cost` shipped with no dashboard surface at all, so the one question the feature
exists to answer — where did my tokens go — could only be asked from a terminal.
`tests/test_usage.py` covers the transcript parser and `tests/test_cost_report.py` the
attribution; what is left here is mostly the ways a spend figure can LIE. A pruned
transcript rendered as zero turns a gap in the evidence into a claim about the spend, and
a page that 500s reading a file Jarvis does not own takes a decision surface down with
it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops, usage  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def transcript(tmp_path, monkeypatch):
    """Write a fake Claude Code transcript and point `usage` at it."""
    root = tmp_path / "transcripts"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def row(mid: str, write_tok: int, read: int, out: int, at: float | None,
            stop_reason: str | None) -> dict:
        one = {"type": "assistant",
               "message": {"id": mid, "model": "claude-opus-5",
                           "stop_reason": stop_reason,
                           "usage": {"input_tokens": 0,
                                     "cache_creation_input_tokens": write_tok,
                                     "cache_read_input_tokens": read,
                                     "output_tokens": out}}}
        if at is not None:
            one["timestamp"] = (datetime.fromtimestamp(at, tz=ZoneInfo("UTC"))
                                .isoformat().replace("+00:00", "Z"))
        return one

    def write(session_id: str, *, write_tok: int = 0, read: int = 0, out: int = 0,
              at: float | None = None, subagents: list[dict] | None = None):
        """One session. `subagents` names each with its own `write_tok`/`read`/`out`, and
        `placeholder=True` stages the two mid-stream rows Claude Code writes for a
        message whose sealed row never arrives (spec 2026-10-08 §1)."""
        (root / "-proj" / f"{session_id}.jsonl").write_text(
            json.dumps(row(f"m-{session_id}", write_tok, read, out, at, "end_turn"))
            + "\n")
        for i, sub in enumerate(subagents or []):
            sub_dir = root / "-proj" / session_id / "subagents"
            sub_dir.mkdir(parents=True, exist_ok=True)
            stop = None if sub.get("placeholder") else "end_turn"
            rows = [row(f"s{i}", sub.get("write_tok", 0), sub.get("read", 0),
                        sub.get("out", 0), sub.get("at"), stop)]
            if sub.get("placeholder"):
                rows.append(dict(rows[0]))      # the second block, no sealed row ever
            (sub_dir / f"agent-{i}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows))

    return write


def give_session(project, wo_id: str, session_id: str) -> None:
    """Attach a session to a work order — the join `cost_report` reads spend through."""
    store = ProjectStore(project)
    try:
        store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                           (session_id, wo_id))
    finally:
        store.close()


def test_cost_page_lists_work_orders_dearest_first(client, project, transcript):
    cheap = ops.create_work_order("proj_a", "a small ask")
    dear = ops.create_work_order("proj_a", "a large ask")
    transcript("sess-cheap", write_tok=1_000, out=100)
    transcript("sess-dear", write_tok=900_000, out=50_000)
    give_session(project, cheap["id"], "sess-cheap")
    give_session(project, dear["id"], "sess-dear")
    # The page reports the current usage week by default, and an order is in a window
    # through its TURNS (§5a of the window-selector spec).
    add_recorded_turn(project, cheap["id"], 0.0, 1_000)
    add_recorded_turn(project, dear["id"], 0.0, 1_000)

    page = client.get("/cost")
    assert page.status_code == 200
    assert page.text.index("a large ask") < page.text.index("a small ask")
    # Said on the page, not only in the docs: the user is on a subscription, so a bare
    # dollar figure is most likely to be misread as an invoice.
    assert "not a bill" in page.text


def test_cost_page_says_when_a_transcript_is_gone_instead_of_saying_zero(
        client, project, transcript):
    """An unmeasurable cost and a zero cost are different answers.

    Rendering them the same is the one thing this page must never do: someone opens it
    precisely to find out where the bill came from, and a silent omission invites a
    conclusion the evidence does not support.
    """
    wo = ops.create_work_order("proj_a", "measured long ago")
    give_session(project, wo["id"], "sess-that-was-pruned")
    add_recorded_turn(project, wo["id"], 0.0, 1_000)

    page = client.get("/cost")
    assert page.status_code == 200
    assert "no transcript left to measure" in page.text
    assert "1 whose transcript Claude Code has" in page.text


def test_cost_page_can_be_scoped_to_one_project(client, project, transcript):
    wo = ops.create_work_order("proj_a", "scoped ask")
    transcript("sess-scoped", write_tok=1_000, out=100)
    give_session(project, wo["id"], "sess-scoped")
    add_recorded_turn(project, wo["id"], 0.0, 1_000)

    assert "scoped ask" in client.get("/cost?project=proj_a").text
    unknown = client.get("/cost?project=nope")
    assert unknown.status_code == 200
    assert "not registered" in unknown.text


def test_the_work_order_page_shows_two_numbers_and_a_way_in(client, project,
                                                            transcript):
    """Total tokens, total dollars, and a link. Nothing else.

    The line this replaced tried to say the whole bill in one row — worker, jarvis,
    tokens, turns, re-write tax — and a reader could not answer any of the four
    questions it raised (wo-4576667e). The itemisation moved to the bill, which has
    room for it; what stays here is the two figures and the door.
    """
    wo = ops.create_work_order("proj_a", "priced task")
    transcript("sess-priced", write_tok=200_000, out=10_000)
    give_session(project, wo["id"], "sess-priced")

    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "210k tokens" in page.text
    assert f'href="/cost/proj_a/{wo["id"]}"' in page.text
    assert "details →" in page.text


def test_the_work_order_page_bills_each_turn_where_the_reader_already_is(client,
                                                                         project):
    """The same bill, turn by turn, on the page the conversation is on.

    Asked for explicitly (wo-4576667e): the bill has to be readable for the whole order
    AND for one turn of it, and the turns have to add up to the order. Expandable, so a
    page opened to read a conversation does not open on a token table.
    """
    wo = ops.create_work_order("proj_a", "billed in place")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)
    add_recorded_turn(project, wo["id"], 0.07, 90_000)

    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "Spend, turn by turn" in page.text
    assert "turn 1" in page.text and "turn 2" in page.text
    assert "<details>" in page.text                  # expandable, and no JavaScript
    assert "cache write" in page.text                # the line items, in place
    assert "Every token this work order spent is on exactly one of those lines" \
        in page.text


def test_a_message_carries_what_answering_it_cost(client, project):
    """A message is an ask; the turn it set going is what was paid for.

    Shown on the message and labelled as the turn's line, never as a second charge —
    `wo_turns.msg_id` is the join, and the figure is the one already in the totals.
    """
    wo = ops.create_work_order("proj_a", "asked and answered")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)
    store = ProjectStore(project)
    try:
        msg_id = store.queue_message(wo["id"], "please also do X", source="ui")
        turn = store.list_turns(wo["id"])[0]
        store.conn.execute("UPDATE wo_turns SET msg_id=? WHERE id=?",
                           (msg_id, turn["id"]))
        store.conn.commit()
    finally:
        store.close()

    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "please also do X" in page.text
    assert "the same line as on the bill" in page.text


def test_a_work_order_with_no_transcript_shows_no_cost_line_at_all(client, project,
                                                                   transcript):
    """Not "$0.00" — the work order has not been dispatched, so nothing is known."""
    wo = ops.create_work_order("proj_a", "never dispatched")
    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "details →" not in page.text


def test_a_broken_transcript_read_never_takes_the_work_order_page_down(
        client, project, monkeypatch):
    """The work order page carries the gate decision and the assumption review.

    A spend figure is the least important thing on it, so a failure reading files Jarvis
    does not own — Claude Code prunes them on its own schedule and owns their format —
    has to cost the line, not the page.
    """
    wo = ops.create_work_order("proj_a", "still readable")

    def boom(*args, **kwargs):
        raise OSError("transcript root vanished mid-read")

    monkeypatch.setattr(ops, "cost_report", boom)
    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "still readable" in page.text
    assert "fleet →" not in page.text


def add_recorded_turn(project, wo_id: str, cost: float, peak: int,
                      window: int = 1_000_000, at: float | None = None,
                      extra: dict | None = None) -> None:
    """A settled turn with its recorded usage envelope, as `_reap` would leave it.

    `at` is an explicit start time: the store stamps `db.now()`, so without it only the
    CURRENT window can be built — and a window selector is tested by what it leaves out.
    """
    store = ProjectStore(project)
    try:
        kind = "message" if store.list_turns(wo_id) else "dispatch"
        turn = store.create_turn(wo_id, kind=kind, prompt="p")
        usage = {"total_cost_usd": cost, "input": 2, "cache_write": 2558,
                 "cache_read": 45689, "cache_1h": 2558, "cache_5m": 0, "output": 941,
                 "api_calls": 1, "context_peak": peak, "context_window": window,
                 "duration_api_ms": 1000, "cost_by_model": {"claude-opus-5": cost}}
        usage.update(extra or {})
        store.finish_turn(turn["id"], "done", result="r", cost_usd=cost, num_turns=1,
                          usage_json=json.dumps(usage))
        if at is not None:
            store.conn.execute(
                "UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                (at, at + 60, turn["id"]))
            store.conn.commit()
    finally:
        store.close()


def test_cost_page_links_each_work_order_to_its_turn_drilldown(client, project,
                                                               transcript):
    wo = ops.create_work_order("proj_a", "drillable")
    transcript("sess-drill", write_tok=1_000, out=10)
    give_session(project, wo["id"], "sess-drill")
    add_recorded_turn(project, wo["id"], 0.0, 1_000)

    page = client.get("/cost")
    assert page.status_code == 200
    assert f'/cost/proj_a/{wo["id"]}' in page.text


def test_the_drilldown_shows_the_turn_table_and_context_growth(client, project):
    """The page this feature exists for: a bloated work order's cost curve, turn by
    turn — each turn's own cost and its context peak against the model's window."""
    wo = ops.create_work_order("proj_a", "bloating one")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)
    add_recorded_turn(project, wo["id"], 0.07, 90_000)

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "exact" in page.text                       # provenance: recorded
    assert "dispatch" in page.text and "message" in page.text
    assert "9.0%" in page.text                        # /context occupancy, turn 2
    assert "48k" in page.text and "90k" in page.text  # peak per turn: growth visible


def test_the_drilldown_labels_a_transcript_only_work_order_an_estimate(
        client, project, transcript):
    """The fallback path must never dress an estimate up as the record."""
    wo = ops.create_work_order("proj_a", "pre capture")
    transcript("sess-pre", write_tok=200_000, out=10_000)
    give_session(project, wo["id"], "sess-pre")

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "the conversation, from its transcript" in page.text
    assert "no turns on record at all" in page.text
    # The spend is real and itemised even so, and it lands on the line that says it
    # belongs to no turn rather than being quietly attributed to one.
    assert "outside any turn" in page.text


# -- what Jarvis itself spent ---------------------------------------------------------
#
# The half of the bill that had no surface at all: Neo answering the work order's
# questions, the panel's seats deliberating on them, the digest shortening the result.
# The page has to show the TOTAL and the SPLIT — a total alone hides that the OS spends
# on a work order at all, and a worker figure alone reads as the whole bill.


def add_os_calls(wo_id: str, *, seats: int = 0, neo_calls: int = 0,
                 output: int = 1_000) -> None:
    """Record OS-side calls against a work order, as the daemon's would be."""
    from jarvis import agent_usage

    for i in range(neo_calls):
        agent_usage.record("neo_answer", project="proj_a", wo_id=wo_id,
                           label="question", model="claude-opus-5", question_id=i + 1,
                           usage={"total_cost_usd": 0.02, "input": 10,
                                  "cache_write": 5_000, "cache_read": 20_000,
                                  "output": output})
    for seat in ("premise", "record", "blast", "taste", "chair")[:seats]:
        agent_usage.record("panel_seat", project="proj_a", wo_id=wo_id, label=seat,
                           model="claude-opus-5", question_id=1,
                           usage={"total_cost_usd": 0.01, "input": 10,
                                  "cache_write": 2_000, "cache_read": 8_000,
                                  "output": output})


def test_the_cost_page_splits_the_fleet_total_into_workers_and_jarvis(
        client, project, transcript):
    wo = ops.create_work_order("proj_a", "an ask that asked back")
    transcript("sess-split", write_tok=100_000, out=5_000)
    give_session(project, wo["id"], "sess-split")
    add_os_calls(wo["id"], neo_calls=2, seats=5)

    page = client.get("/cost")
    assert page.status_code == 200
    assert "workers ~$" in page.text and "jarvis ~$" in page.text
    assert "7 calls" in page.text
    # And per work order, with what the spend went ON: five seats is a shape a total
    # can never make visible.
    assert "5 panel seat" in page.text and "2 Neo answering" in page.text


def test_a_work_order_with_no_transcript_still_shows_what_jarvis_spent_on_it(
        client, project):
    """The OS's own calls are the OS's own record. They do not depend on a file Claude
    Code is free to prune, so pruning must not hide them."""
    wo = ops.create_work_order("proj_a", "pruned but not free")
    give_session(project, wo["id"], "sess-long-gone")
    add_os_calls(wo["id"], neo_calls=3)

    fleet = client.get("/cost")
    # "$x+": the jarvis half is known, the worker half is not — never a bare total that
    # would read as the whole bill.
    assert "+" in fleet.text and "no transcript left to measure" in fleet.text
    assert "jarvis ~$" in fleet.text

    # The work order page keeps its two figures; the bill is where the halves are named,
    # and the OS's half is on it even though the transcript is gone.
    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert "details →" in page.text
    bill = client.get(f"/cost/proj_a/{wo['id']}")
    assert "what Jarvis spent on this order" in bill.text
    assert "3 calls" in bill.text


def test_the_bill_names_every_actor_and_the_tokens_each_spent(client, project,
                                                              transcript):
    """The user's fourth question: where are the tokens per actor.

    A dollar figure per half was never the missing thing — the tokens were, because a
    total in tokens beside a total in dollars that describe different scopes is what
    made the old line unreadable.
    """
    wo = ops.create_work_order("proj_a", "priced with help")
    transcript("sess-helped", write_tok=200_000, out=10_000)
    give_session(project, wo["id"], "sess-helped")
    add_os_calls(wo["id"], neo_calls=1)

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "worker&#39;s own session" in page.text  # Jinja escapes the apostrophe
    assert "what Jarvis spent on this order" in page.text
    assert "1 call" in page.text
    # Tokens beside dollars on every actor line, not only on the headline.
    assert "210k" in page.text and "26k" in page.text


def test_the_bill_page_names_the_residue_and_what_it_could_not_measure(
        client, project, transcript):
    """§7 and §9: the residue row, the placeholder sentence, and the three states a
    reader must be able to tell apart — a residue, a measured zero, and a seal written
    before the measurement existed (which renders as nothing, never as a zero)."""
    now = time.time()
    wo = ops.create_work_order("proj_a", "with a placeholder subagent")
    transcript("sess-residue", write_tok=1_000, read=5_000, out=6_000, at=now + 5,
               subagents=[{"write_tok": 1_000, "read": 2_000, "out": 5,
                           "at": now + 20, "placeholder": True}])
    give_session(project, wo["id"], "sess-residue")
    add_recorded_turn(project, wo["id"], 0.5, 50_000, at=now,
                      extra={"input": 0, "cache_write": 10_000, "cache_read": 100_000,
                             "output": 100_000, "cache_1h": 0, "cache_5m": 10_000})

    page = client.get(f"/cost/proj_a/{wo['id']}")

    assert page.status_code == 200
    assert "unattributed" in page.text
    assert "API calls report placeholder" in page.text
    assert "a measured zero" not in page.text
    # The reworded captions, which used to assert the difference IS the subagents.
    assert "what no transcript accounts\n  for" in page.text
    assert "NEITHER accounts for" in page.text

    # A bill with no placeholder call anywhere says so — a measured zero.
    plain = ops.create_work_order("proj_a", "nothing hidden")
    transcript("sess-plain-ph", write_tok=1_000, out=500, at=now + 5)
    give_session(project, plain["id"], "sess-plain-ph")
    add_recorded_turn(project, plain["id"], 0.1, 10_000, at=now)
    plain_page = client.get(f"/cost/proj_a/{plain['id']}")
    assert "a measured zero" in plain_page.text
    assert "API calls report placeholder" not in plain_page.text

    # And a seal from before the measurement renders NEITHER (§9).
    store = ProjectStore(project)
    try:
        sealed = {k: v for k, v in ops.bill(plain["id"], live=True).items()
                  if k != "placeholder"}
        store.seal_bill(plain["id"], json.dumps(sealed))
        store.set_status(plain["id"], "completed")
    finally:
        store.close()
    old_page = client.get(f"/cost/proj_a/{plain['id']}")
    assert "a measured zero" not in old_page.text
    assert "API calls report placeholder" not in old_page.text


def test_the_bill_says_which_way_a_turn_it_cannot_re_read_was_counted(client, project):
    """Two superseded readings, and they are wrong in OPPOSITE directions.

    A version-1 turn counted a fraction of itself and a version-2 one counted the whole
    resumed session (issue #470). Both survive only where the result JSON is gone, both
    are labelled, and one label for both would tell half the readers the wrong story
    about which way their bill is off.
    """
    wo = ops.create_work_order("proj_a", "counted before the fix")
    store = ProjectStore(project)
    try:
        for version in (1, 2):
            turn = store.create_turn(wo["id"], kind="message", prompt="p")
            store.finish_turn(turn["id"], "done", result="r", cost_usd=0.05,
                              num_turns=1, usage_json=json.dumps({
                                  "usage_v": version, "total_cost_usd": 0.05,
                                  "input": 2, "cache_write": 2_558,
                                  "cache_read": 45_689, "output": 941,
                                  "by_model": [{"model": "claude-opus-5",
                                                "input": 2, "cache_write": 2_558,
                                                "cache_read": 45_689, "output": 941,
                                                "cost_usd": 0.05}]}))
    finally:
        store.close()

    page = client.get(f"/cost/proj_a/{wo['id']}")

    assert page.status_code == 200
    assert "counted the old way" in page.text
    assert "are the pre-fix reading" in page.text
    assert "running total and is OVERstated" in page.text


def test_the_bill_gives_each_panel_seat_its_own_line(client, project):
    """The standing ruling — one row per seat, never one per question — rendered.

    Which seat is dear is exactly what a panel's price is asked about, and an aggregate
    answers it for none of them.
    """
    wo = ops.create_work_order("proj_a", "panelled")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)
    add_os_calls(wo["id"], seats=5, neo_calls=1)

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "what Jarvis spent on this order" in page.text
    for seat in ("premise", "record", "blast", "taste", "chair"):
        assert seat in page.text
    assert "panel seat" in page.text and "5 calls" in page.text


def test_the_bill_says_so_when_jarvis_spent_nothing(client, project):
    """Zero here is a real answer — the work order asked Neo nothing — and it is worth
    saying, because a line that is simply absent reads as a line that was left out.

    This is the user's first question about the old surface ("not a single token spent
    on the OS that wasn't jarvis?"): what is NOT on the bill has to be named too.
    """
    wo = ops.create_work_order("proj_a", "self-sufficient")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "What is not on this bill" in page.text
    assert "asked Neo no questions" in page.text
    assert "outside Jarvis's transport" in page.text  # literal prose, not escaped


def test_the_cost_tab_is_reachable_from_every_page(client):
    """A surface nobody can find is the bug this work order was filed about."""
    assert '<a href="/cost"' in client.get("/").text
    assert 'class="here"' in client.get("/cost").text


def add_subprocess_calls(wo_id: str, n: int = 4, label: str = "pytest") -> None:
    """Spend the worker made BELOW itself — `claude` processes its own tool call ran."""
    from jarvis import agent_usage

    for _ in range(n):
        agent_usage.record(agent_usage.WORKER_SUBPROCESS, project="proj_a", wo_id=wo_id,
                           label=label, model="claude-opus-5",
                           usage={"total_cost_usd": 0.03, "input": 10,
                                  "cache_write": 4_000, "cache_read": 12_000,
                                  "output": 900})


def test_the_cost_page_shows_subprocess_spend_as_its_own_class(client, project,
                                                               transcript):
    """Three figures under the headline, not two. A work order that ran an eval suite and
    one that asked Neo four questions are spending differently, and folding them together
    loses exactly the distinction someone reading an expensive work order came for."""
    wo = ops.create_work_order("proj_a", "shipped the release")
    transcript("sess-shipped", write_tok=100_000, out=5_000)
    give_session(project, wo["id"], "sess-shipped")
    add_os_calls(wo["id"], neo_calls=1)
    add_subprocess_calls(wo["id"], n=4)

    page = client.get("/cost")
    assert page.status_code == 200
    assert "subprocesses ~$" in page.text
    assert "4 claude processes" in page.text
    # Still counted apart from Jarvis's own overhead, which was one call, not five.
    assert "1 call" in page.text


def test_the_drilldown_groups_subprocess_spend_by_what_ran_it(client, project):
    """One `pytest evals/llm` is forty calls. The OS's per-call table is the right shape
    for five panel seats and the wrong one for forty eval scenarios."""
    wo = ops.create_work_order("proj_a", "ran the evals")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)
    add_subprocess_calls(wo["id"], n=40, label="pytest")

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "claude processes the worker spawned itself" in page.text
    assert "pytest" in page.text and "40 calls" in page.text


def test_the_cost_page_shows_the_fleet_distribution(client, project):
    """The page could say which order cost the most and not what normal looks like.

    Same keys as `jarvis cost --fleet --json`, computed once by `ops.fleet_cost` — a
    second computation on the page is how two surfaces start disagreeing about a number.
    """
    wo = ops.create_work_order("proj_a", "the typical one")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)

    page = client.get("/cost")

    assert page.status_code == 200
    assert "cost_per_turn_usd" in page.text
    assert "p90" in page.text
    assert "usage week" in page.text, "the window says where it came from"


def test_the_cost_page_survives_a_fleet_section_that_cannot_be_built(client, project,
                                                                     monkeypatch):
    """The distribution is a SECTION. Losing it must not take the listing down with it."""
    monkeypatch.setattr(ops, "fleet_cost",
                        lambda **_k: (_ for _ in ()).throw(ops.OpsError("no catalog")))

    page = client.get("/cost")

    assert page.status_code == 200
    assert "What the work cost" in page.text


# -- the window selector ---------------------------------------------------------------
#
# docs/superpowers/specs/2026-10-07-cost-window-selector.md. The page could report one
# usage week and nothing else, and its two halves measured different populations. Every
# test here is about the two halves moving TOGETHER, and about a bad parameter being a
# refusal rather than a week's figures under the label the reader picked.


def last_week(**kwargs) -> dict:
    return ops.cost_window(window="week", offset=-1, **kwargs)


def test_the_default_window_is_unchanged(client, project):
    """Invisible until something is picked: no parameters, and it is still the week."""
    wo = ops.create_work_order("proj_a", "ran this week")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)

    page = client.get("/cost")

    assert page.status_code == 200
    assert "usage week" in page.text
    assert wo["id"] in page.text


def test_a_past_week_moves_both_halves(client, project):
    """Defect 3's acceptance test: the listing and the distribution report one window."""
    before = ops.create_work_order("proj_a", "ran last week")
    now = ops.create_work_order("proj_a", "ran this week")
    add_recorded_turn(project, before["id"], 0.05, 48_000,
                      at=last_week()["since"] + 3_600)
    add_recorded_turn(project, now["id"], 0.07, 50_000)

    past = client.get("/cost?window=week&offset=-1")
    current = client.get("/cost?window=week&offset=0")

    assert past.status_code == 200 and current.status_code == 200
    assert before["id"] in past.text and now["id"] not in past.text
    assert now["id"] in current.text and before["id"] not in current.text
    # The distribution counts the same population as the listing above it.
    assert "1 order with a turn in it" in past.text
    assert "1 order with a turn in it" in current.text


def test_a_past_5h_window(client, project):
    wo = ops.create_work_order("proj_a", "an order three windows ago")
    picked = ops.cost_window(window="5h", offset=-3)
    add_recorded_turn(project, wo["id"], 0.05, 48_000, at=picked["since"] + 60)

    page = client.get("/cost?window=5h&offset=-3")

    assert page.status_code == 200
    assert picked["label"] in page.text
    assert picked["until"] - picked["since"] == 5 * 3_600
    assert "5h slice of the usage week" in page.text, "the anchor is on the page"
    assert wo["id"] in page.text


def test_a_custom_range(client, project):
    """The `datetime-local` form round-trips, and the form pre-fills with the window."""
    wo = ops.create_work_order("proj_a", "inside the typed range")
    at = ops.cost_window(window="week")["since"] + 3_600
    add_recorded_turn(project, wo["id"], 0.05, 48_000, at=at)
    # Rendered AND parsed in the page's display zone, which with no `?tz=` is the
    # catalog's `week_reset_zone` (§11) — not UTC.
    zone = ZoneInfo(ops.cost_zone())
    fmt = "%Y-%m-%dT%H:%M"
    since = datetime.fromtimestamp(at - 600, zone).strftime(fmt)
    until = datetime.fromtimestamp(at + 600, zone).strftime(fmt)

    page = client.get(f"/cost?since={since}&until={until}")

    assert page.status_code == 200
    assert wo["id"] in page.text
    assert f'name="since" value="{since}"' in page.text
    assert f'name="until" value="{until}"' in page.text


@pytest.mark.parametrize("query,message", [
    ("window=5x", "is not a window this report knows"),
    ("window=week&offset=1", "names a window that has not happened yet"),
    ("window=week&offset=abc", "offset must be a whole number of windows"),
    ("since=2026-10-06T04:00&until=2026-10-01T04:00", "which is an empty window"),
    ("window=week&since=2026-10-01T04:00", "pass one or the other, not both"),
])
def test_invalid_params_refuse_rather_than_falling_back(client, project, query, message):
    """The one failure mode a selector must not have: a week's figures under the wrong
    label. Every bad parameter is a refusal."""
    wo = ops.create_work_order("proj_a", "ran this week")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)

    page = client.get(f"/cost?{query}")

    assert page.status_code == 200
    assert message in page.text
    assert "What a typical order costs" not in page.text
    assert wo["id"] not in page.text


def test_the_selector_survives_a_fleet_section_that_cannot_be_built(client, project,
                                                                    monkeypatch):
    """Losing the distribution must not lose the window the reader picked."""
    wo = ops.create_work_order("proj_a", "ran last week")
    add_recorded_turn(project, wo["id"], 0.05, 48_000,
                      at=last_week()["since"] + 3_600)
    monkeypatch.setattr(ops, "fleet_cost",
                        lambda **_k: (_ for _ in ()).throw(ops.OpsError("no catalog")))

    page = client.get("/cost?window=week&offset=-1")

    assert page.status_code == 200
    assert wo["id"] in page.text
    assert last_week()["label"] in page.text


@pytest.fixture()
def two_project_client(jarvis_home, fake_claude, tmp_path, project, claude_json):
    """A fleet of TWO registered projects — `/cost` with no scope reads every store."""
    from jarvis.testing import make_git_project

    other = make_git_project(tmp_path, "proj_b")
    claude_json(other)
    catalog = tmp_path / "catalog-two.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet", "max_in_flight": 50},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project), "description": "one"},
                     {"name": "proj_b", "path": str(other), "description": "two"}],
    }))
    ops.start_os(str(catalog), foreground=True)
    return TestClient(create_app(), follow_redirects=False), other


def os_call_now(project_name: str, wo_id: str, cost: float = 0.25) -> None:
    """One recorded OS call, stamped now — so it lands in the DEFAULT window."""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        central.add_agent_call("neo_answer", project=project_name, wo_id=wo_id,
                               model="claude-opus-5",
                               usage={"total_cost_usd": cost, "input": 10, "output": 100})
        central.conn.commit()
    finally:
        central.close()


def test_the_default_window_survives_two_projects_with_os_calls(two_project_client):
    """The fleet-wide page: `_os_groups` returns BOTH projects' ids, and each store is
    opened in turn. Looking a foreign id up in the open store raises `KeyError`, so this
    is the shape in which `/cost` with no parameters 500s."""
    client, other = two_project_client
    a = ops.create_work_order("proj_a", "proj_a asked Neo")
    b = ops.create_work_order("proj_b", "proj_b asked Neo")
    os_call_now("proj_a", a["id"])
    os_call_now("proj_b", b["id"])

    page = client.get("/cost")

    assert page.status_code == 200
    assert a["id"] in page.text and b["id"] in page.text


def test_the_window_is_in_the_url(client, project):
    current = client.get("/cost?project=proj_a")
    past = client.get("/cost?project=proj_a&window=week&offset=-2")

    assert "/cost?window=week&offset=-1&project=proj_a" in current.text
    assert "next ›" not in current.text, "offset > 0 is refused, so there is no link"
    assert "/cost?window=week&offset=-3&project=proj_a" in past.text
    assert "/cost?window=week&offset=-1&project=proj_a" in past.text  # next
    assert "next ›" in past.text


def test_the_reader_can_pick_the_display_zone(client, project):
    """§11: the zone is DISPLAY ONLY, and a bad one is a refusal with a sentence."""
    berlin = ops.cost_window(window="week", tz="Europe/Berlin")
    default = ops.cost_window(window="week")

    page = client.get("/cost?tz=Europe/Berlin")

    assert page.status_code == 200
    assert berlin["local_label"] in page.text
    assert default["local_label"] not in page.text
    # No number moved: the label is the only difference.
    assert (berlin["since"], berlin["until"]) == (default["since"], default["until"])
    assert default["label"] in page.text
    assert 'name="tz"' in page.text and 'value="Europe/Berlin"' in page.text

    bad = client.get("/cost?tz=Mars/Olympus")
    assert bad.status_code == 200
    # The template escapes the quotes the sentence puts round the zone name.
    assert "tz must be an IANA time zone name" in bad.text
    assert "Mars/Olympus" in bad.text
    assert "is not a zone this report knows" in bad.text
    assert default["local_label"] not in bad.text, "a refusal, never a fallback"


def test_the_picked_zone_rides_on_every_window_link(client, project):
    page = client.get("/cost?tz=Europe/Berlin")

    assert "/cost?window=week&offset=-1&project=&tz=Europe/Berlin" in page.text
    assert "/cost?window=5h&offset=-1&project=&tz=Europe/Berlin" in page.text
    assert '<input type="hidden" name="tz" value="Europe/Berlin">' in page.text


def test_a_custom_range_is_parsed_in_the_picked_zone(client, project):
    """One clock: the form is rendered AND parsed in the zone the reader picked."""
    page = client.get("/cost?since=2026-10-20T10:00&until=2026-10-20T12:00"
                      "&tz=Europe/Berlin")

    assert page.status_code == 200
    # Berlin was CEST (UTC+2) on that date, so 10:00 local is 08:00 UTC.
    assert "2026-10-20 08:00 to 2026-10-20 10:00 UTC" in page.text
    assert 'name="since" value="2026-10-20T10:00"' in page.text
    assert 'name="until" value="2026-10-20T12:00"' in page.text
    assert "Europe/Berlin" in page.text and "UTC — the clock" not in page.text
    # An explicit offset submitted by hand is still honoured.
    explicit = client.get("/cost?since=2026-10-20T10:00:00%2B00:00"
                          "&until=2026-10-20T12:00:00%2B00:00&tz=Europe/Berlin")
    assert "2026-10-20 10:00 to 2026-10-20 12:00 UTC" in explicit.text


def test_every_cost_surface_says_the_figure_is_a_floor(client, project):
    """Unconditional, and identical on both pages — `ops.COST_FLOOR_NOTE` is the single
    source. A caveat that appears in one surface and not another is one the reader learns
    to ignore."""
    wo = ops.create_work_order("proj_a", "nothing spent below itself")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)

    for url in ("/cost", f"/cost/proj_a/{wo['id']}"):
        page = client.get(url)
        assert page.status_code == 200
        assert "is a floor" in page.text, url


# -- a feature order's own spend -------------------------------------------------------


def test_the_feature_bill_shows_the_parents_own_calls_beside_its_orders(
        client, project, improvement_order):
    """The list under "the orders under it" sums to the headline only with this row:
    calls Jarvis made against the parent itself belong to no child."""
    from jarvis.bill import OWN_LABEL

    io = improvement_order
    add_os_calls(io["id"], neo_calls=2)

    page = client.get(f"/cost/proj_a/{io['id']}")
    assert page.status_code == 200
    assert OWN_LABEL in page.text


def test_the_feature_bill_grows_no_own_row_when_there_is_no_own_spend(
        client, project, improvement_order):
    from jarvis.bill import OWN_LABEL

    io = improvement_order

    page = client.get(f"/cost/proj_a/{io['id']}")
    assert page.status_code == 200
    assert "The orders under it" in page.text
    assert OWN_LABEL not in page.text
