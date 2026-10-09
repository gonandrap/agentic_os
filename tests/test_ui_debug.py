"""The debugging page — `GET /wo/{name}/{wo_id}/debug` — and the JSON the poll reads.

§7 of docs/superpowers/specs/2026-09-24-order-observability.md. The four payloads (`ops.diagnose`,
`ops.live_report`, `ops.inspect_report`, `ops.context_report`) are each covered by their
own file; what is left here is the two ways ONE page over four readings can lie:

* a block that cannot be read taking the other three down with it — a 500 here becomes an
  inbox item and an `INV-UI-HEALTHY` fleet alarm, so degradation has to be a note;
* absent rendered as zero (issue #227) — a missing session, an expired transcript and an
  order predating the context ledger are three gaps in the evidence, never three zeros.

The transcript row builders are copied from `tests/test_live.py` rather than imported, for
the reason that file gives: they describe the shape Claude Code writes, and a shared
fixture would make one child's edit the other's failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import autopsy, cli, inspection, ops, uilog, usage  # noqa: E402
from jarvis.catalog import load_catalog  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import TEMPLATES, create_app  # noqa: E402

BLOCKS = ("diagnosis", "live", "anatomy", "context")


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(at: float, text: str) -> dict:
    return {"type": "user", "timestamp": stamp(at), "promptSource": "sdk",
            "message": {"content": text}}


def assistant_row(at: float, mid: str, *, write: int = 0, read: int = 0,
                  content: list | None = None, out: int = 1) -> dict:
    return {
        "type": "assistant", "timestamp": stamp(at),
        "message": {"id": mid, "model": "claude-opus-5",
                    "usage": {"input_tokens": 0, "cache_creation_input_tokens": write,
                              "cache_read_input_tokens": read, "output_tokens": out},
                    "content": content or [{"type": "text", "text": "ok"}]},
    }


def tool_rows(start: float, end: float, tool_id: str, name: str,
              payload: dict | None = None) -> list[dict]:
    return [
        {"type": "assistant", "timestamp": stamp(start),
         "message": {"id": f"m-{tool_id}", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "output_tokens": 1},
                     "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                  "input": payload or {}}]}},
        {"type": "user", "timestamp": stamp(end),
         "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]}},
    ]


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    """A transcript root the OS's readers resolve sessions against."""
    root = tmp_path / "debug-projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict]) -> None:
        (root / "-proj" / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    return write


@pytest.fixture()
def started(jarvis_home, fake_claude, project, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return load_catalog(catalog_file)


@pytest.fixture()
def client(started):
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def dispatched(started, project):
    """A really dispatched work order: a session id, a turn row and a context payload.

    Through `dispatch.dispatch_work_order` rather than a hand-built turn, the way
    `tests/test_context.py` does it — a fabricated turn would pass with the ledger
    unwired, and the context block is one of the four this page has to show.
    """
    from jarvis import dispatch

    ops.create_work_order("proj_a", "measure me", description="do a thing")
    store = ProjectStore(project)
    central = CentralStore()
    wo = store.claim_next_pending()
    dispatch.dispatch_work_order(store, central, started.projects[0], wo,
                                 os_config=started.os)
    try:
        yield {"store": store, "central": central, "spec": started.projects[0],
               "wo_id": wo["id"], "session": store.get_work_order(wo["id"])["session_id"]}
    finally:
        central.close()
        store.close()


def _inside(store: ProjectStore, wo_id: str, seq: int) -> float:
    """A timestamp inside that turn's process window — the join the context ledger is
    made on (`tests/test_context.py`'s helper of the same name). An unfinished turn owns
    everything after its start (`ops._in_window`), so a running one needs no midpoint."""
    turn = next(t for t in store.list_turns(wo_id) if t["seq"] == seq)
    if turn["ended_at"] is None:
        return turn["started_at"] + 0.01
    return turn["started_at"] + (turn["ended_at"] - turn["started_at"]) / 2


def _order(text: str, *names: str) -> list[int]:
    return [text.index(f'id="block-{n}"') for n in names]


# -- 1. the page, and the order the spec ranks the blocks in ---------------------------

def test_the_page_renders_all_four_blocks_with_the_diagnosis_first(
        client, dispatched, transcripts):
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    at = _inside(store, wo_id, 1)
    transcripts(dispatched["session"],
                [prompt_row(at, "go"),
                 *tool_rows(at, at + 1, "t1", "Bash", {"command": "uv run pytest -q"}),
                 assistant_row(at + 2, "m1", write=60_000)])

    page = client.get(f"/wo/proj_a/{wo_id}/debug")

    assert page.status_code == 200
    places = _order(page.text, *BLOCKS)
    assert places == sorted(places)
    assert "could not be read" not in page.text
    # The anatomy's own evidence: the tool profile and the call's parameters, which have
    # no dashboard surface at all before this page.
    assert "uv run pytest -q" in page.text
    assert "Bash" in page.text


def test_the_page_states_the_caps_and_floors_its_readings_were_taken_at(
        client, dispatched, transcripts):
    """A truncation the reader cannot size is not reproducible (spec §4a)."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    transcripts(dispatched["session"], [prompt_row(_inside(store, wo_id, 1), "go")])

    page = client.get(f"/wo/proj_a/{wo_id}/debug").text

    assert str(inspection.PARAM_CAPS.per_value) in page
    assert "write floor" in page and "join floor" in page
    assert str(inspection.SUBAGENT_DEPTH_READ) in page


def test_the_partition_percentages_are_formatted_exactly_as_the_cli_formats_them(
        client, dispatched, transcripts):
    """One number, one string. `%.0f` is half-up and a floor here would read 33% on the
    page against the terminal's 34% for a share of 0.335 — two surfaces disagreeing about
    one figure, which is the whole reason the payload does the arithmetic (spec §7)."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    at = _inside(store, wo_id, 1)
    # A turn whose clock is mostly a tool call, so `share` carries a figure that rounds.
    transcripts(dispatched["session"],
                [prompt_row(at, "go"),
                 *tool_rows(at, at + 7, "t1", "Bash", {"command": "sleep 7"}),
                 assistant_row(at + 9, "m1")])

    page = client.get(f"/wo/proj_a/{wo_id}/debug").text
    (unit,) = ops.inspect_report(wo_id, "proj_a")["units"]

    for key in inspection.PARTS:
        # The CLI's own expression, in the markup's exact slot: a template that floored or
        # kept a decimal would fail here.
        assert f"{unit['share'][key] * 100:.0f}%</span>" in page

    # And structurally, because the two roundings only diverge on a share landing on .x5,
    # which no fixture can guarantee: a floor is a different function, not a different
    # rendering of the same one.
    source = (TEMPLATES / "_debug_anatomy.html").read_text()
    assert '"%.0f"|format(u.share[k] * 100)' in source
    assert "round(" not in source


def test_an_unknown_work_order_is_a_404_with_the_stale_link_hint(client):
    """The same wording `/wo/{name}/{wo_id}` builds — one helper, so the two cannot
    drift and tell a visitor two different things about one bad link."""
    gone = client.get("/wo/proj_a/wo-nope/debug")
    assert gone.status_code == 404
    assert "no work order" in gone.text

    nowhere = client.get("/wo/proj_gone/wo-nope/debug")
    assert nowhere.status_code == 404
    assert "does not know about" in nowhere.text


# -- 2. `?debug=1` is a different thing and stays one ---------------------------------

def test_the_debug_query_param_on_the_work_order_page_is_untouched(client, dispatched):
    """`?debug=1` means "show debug-level timeline events" and is not this page."""
    wo_id = dispatched["wo_id"]

    plain = client.get(f"/wo/proj_a/{wo_id}")
    debug = client.get(f"/wo/proj_a/{wo_id}?debug=1")

    assert plain.status_code == debug.status_code == 200
    # Neither is the debugging page: no block of it renders on either.
    for page in (plain.text, debug.text):
        assert 'id="block-diagnosis"' not in page
    # And the query parameter still does its old job: the timeline grows.
    assert debug.text.count("tl-") >= plain.text.count("tl-")


# -- 3. each block degrades alone -----------------------------------------------------

@pytest.mark.parametrize("fn,block", [("diagnose", "diagnosis"),
                                      ("live_report", "live"),
                                      ("inspect_report", "anatomy"),
                                      ("context_report", "context")])
def test_one_unreadable_block_leaves_the_other_three_standing(client, dispatched,
                                                              monkeypatch, fn, block):
    """A 500 here becomes an inbox item and a fleet alarm. Four readings, four
    independent fetches — one of them failing is a note, never an error page."""
    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ops, fn, boom)

    page = client.get(f"/wo/proj_a/{dispatched['wo_id']}/debug")

    assert page.status_code == 200
    for name in BLOCKS:
        assert f'id="block-{name}"' in page.text
    # EXACTLY one: a block that took a sibling down with it would report twice.
    assert page.text.count("could not be read") == 1
    assert "RuntimeError" in page.text and "kaboom" in page.text


# -- 3b. the two derivations this page renders, after the 2026-09-28 spec -------------


def test_a_hold_the_round_ended_renders_closed_on_the_debug_page(client, project):
    """The dashboard end of §2d1. wo-3615faf7 read "still held" 11.7h after the round
    that ended the hold passed, because the synthesised transport hold was closed only by
    a resubmission. Spec §2d1 of
    docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md."""
    from jarvis import db
    from jarvis.holds import HOLD_CAUSES, PAUSE_USAGE_LIMIT
    from jarvis.project_store import VALIDATION_HELD_CAUSE

    wo = ops.create_work_order("proj_a", "held, then judged")
    store = ProjectStore(project)
    try:
        held_at = db.now() - 7200
        store.add_event(wo["id"], "validation_failed",
                        {"round": 1, "cause": VALIDATION_HELD_CAUSE}, ts=held_at)
        store.add_event(wo["id"], "validation_passed", {"round": 1}, ts=held_at + 3600)
    finally:
        store.close()

    page = client.get(f"/wo/proj_a/{wo['id']}/debug")

    assert page.status_code == 200
    assert HOLD_CAUSES[PAUSE_USAGE_LIMIT] in page.text
    assert "still held" not in page.text


def test_the_work_order_page_says_the_spans_stop_short_of_the_status(client, project):
    """The dashboard end of §2d2's belt: `_states.html` renders `states.notes`, so the
    page says the record stops short instead of presenting a stale span as the present
    tense. A reader that silently believed the gap is why nobody noticed for 20.7h."""
    wo = ops.create_work_order("proj_a", "the gapped one")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "waiting_input")
        store.conn.execute("UPDATE work_orders SET status='needs_review' WHERE id=?",
                           (wo["id"],))
        store.conn.commit()
    finally:
        store.close()

    page = client.get(f"/wo/proj_a/{wo['id']}")

    assert page.status_code == 200
    # Jinja-escaped, as every OS sentence on this page is: the note has an apostrophe.
    assert ops.SPANS_BEHIND_NOTE.replace("'", "&#39;") in page.text


# -- 4. absent is never zero (issue #227) ---------------------------------------------

def test_an_order_with_no_session_says_so_instead_of_reporting_zeroes(client):
    """No session, no transcript, no ledger: three gaps in the evidence, and a page that
    printed 0 turns would be making a claim the record does not support."""
    wo = ops.create_work_order("proj_a", "never dispatched")

    page = client.get(f"/wo/proj_a/{wo['id']}/debug")

    assert page.status_code == 200
    assert "nothing can be read" in page.text          # the live frame's own note
    assert "no transcript" in page.text                # the anatomy's `found: false`
    assert ops.NOT_RECORDED in page.text               # the ledger's forward-only note
    assert "0 turns" not in page.text and "0 tokens" not in page.text
    # §4 of 2026-09-27: and the page says WHICH reading gave that answer — rendered above
    # the `found` short-circuit, so it is visible in the one case it matters.
    assert page.text.count(autopsy.NOT_RECORDED_NOTE) >= 2  # anatomy AND the ledger


def test_an_order_predating_the_context_ledger_renders_the_forward_only_note(
        client, dispatched):
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    store.conn.execute("UPDATE wo_turns SET context_json=NULL WHERE wo_id=?", (wo_id,))
    store.conn.commit()

    page = client.get(f"/wo/proj_a/{wo_id}/debug")

    assert page.status_code == 200
    assert ops.NOT_RECORDED in page.text
    assert ops.TURN_NOT_RECORDED in page.text
    # An unsealed order's ledger says it was DERIVED: the forward-only note is about the
    # ledger, the provenance is about the reading, and the two absences are not one.
    assert "derived from the session transcript" in page.text


def test_a_sealed_order_says_on_the_page_that_it_was_read_from_its_seal(
        client, dispatched, transcripts):
    """§4: every surface PRINTS which reading answered. The transcript is deleted here,
    which is the case the seal exists for — the page still shows the anatomy."""
    from jarvis import autopsy

    store, wo_id = dispatched["store"], dispatched["wo_id"]
    at = _inside(store, wo_id, 1)
    transcripts(dispatched["session"],
                [prompt_row(at, "go"), assistant_row(at + 2, "m1", write=60_000)])
    name, path, _row = ops.find_work_order(wo_id)
    autopsy.seal(name, path, store.get_work_order(wo_id))

    page = client.get(f"/wo/proj_a/{wo_id}/debug")

    assert page.status_code == 200
    assert "SEALED autopsy" in page.text
    assert autopsy.autopsy_level_note(autopsy.NORMAL) in page.text


# -- 5. the JSON the poll reads -------------------------------------------------------

def test_the_live_json_route_is_the_payload_verbatim(client):
    """`ops.live_report` and nothing reshaped: a route that rephrased one key is a
    second answer for the page to disagree with (PR 65)."""
    wo = ops.create_work_order("proj_a", "no session yet")

    got = client.get(f"/api/wo/proj_a/{wo['id']}/live")

    assert got.status_code == 200
    assert got.json() == ops.live_report(wo["id"], "proj_a")


def test_the_live_json_route_404s_for_an_unknown_work_order(client):
    gone = client.get("/api/wo/proj_a/wo-nope/live")
    assert gone.status_code == 404
    assert "wo-nope" in gone.json()["error"]


def test_the_page_polls_that_route_and_stops_on_a_bad_response(client, dispatched):
    page = client.get(f"/wo/proj_a/{dispatched['wo_id']}/debug").text
    assert f"/api/wo/proj_a/{dispatched['wo_id']}/live" in page
    assert "setInterval" in page or "setTimeout" in page


# -- 6. the residual is labelled, and the authority travels with the hypothesis -------

def test_the_polls_formatters_carry_the_same_thresholds_as_the_servers(client,
                                                                      dispatched):
    """The poll writes into spans the server rendered through `app.fmt_tok`/`app.fmt_dur`,
    so an unformatted value turns "silent 4m" into "silent 243.7" on the first refresh.

    Pinned by DERIVING both sides rather than by quoting the numbers here, and IN ORDER:
    a set comparison passes a comparison skewed to 3599 while the division beside it still
    says 3600, which is exactly the shape of the bug this pins.
    """
    import ast
    import inspect as pyinspect
    import re
    import textwrap

    from jarvis.ui import app as ui_app

    def scales(text: str) -> list[str]:
        return [n.replace("_", "") for n in re.findall(r"\b\d[\d_]*\d\b", text)]

    def body(fn) -> str:
        # Statements only: the docstring's own examples ("1.3M / 47k / 812") are not
        # thresholds, so the source is parsed and the docstring node dropped.
        tree = ast.parse(textwrap.dedent(pyinspect.getsource(fn))).body[0]
        return "\n".join(ast.unparse(node) for node in tree.body
                         if not (isinstance(node, ast.Expr)
                                 and isinstance(node.value, ast.Constant)
                                 and isinstance(node.value.value, str)))

    script = client.get(f"/wo/proj_a/{dispatched['wo_id']}/debug").text.split("<script>")[1]

    def helper(name: str) -> str:
        return script.split(f"var {name} = function")[1].split("};")[0]

    for server_fn, js in ((ui_app.fmt_tok, "fmtTok"), (ui_app.fmt_dur, "fmtDur")):
        thresholds = scales(body(server_fn))
        assert thresholds, f"{server_fn.__name__} carries no threshold — rewritten?"
        assert scales(helper(js)) == thresholds
    # The output strings too: a threshold kept and a suffix changed disagrees just as
    # loudly, and `—` is `fmt_dur`'s answer for an absent duration.
    for suffix in ('+ "M"', '+ "k"', '+ "h"', '+ "m"', '+ "s"', '"—"'):
        assert suffix in script
    assert "fmtTok(d.tokens.context)" in script and "fmtDur(d.stale_seconds)" in script


def test_a_prefix_break_is_shown_with_the_authority_and_the_residual_labelled(
        client, dispatched, transcripts):
    """The write classification is the authority and the delta is the hypothesis
    (kn-fafe92b7): the page names the cause the payload named and raises no alarm."""
    from jarvis import worker_session

    store, wo_id = dispatched["store"], dispatched["wo_id"]
    store.finish_turn(store.latest_turn(wo_id)["id"], "done", result="ok")
    worker_session.send(store, dispatched["spec"], store.get_work_order(wo_id), "more")
    store.finish_turn(store.latest_turn(wo_id)["id"], "done", result="ok")
    first, second = _inside(store, wo_id, 1), _inside(store, wo_id, 2)
    transcripts(dispatched["session"],
                [prompt_row(first, "go"), assistant_row(first, "m1", write=60_000),
                 prompt_row(second, "more"),
                 assistant_row(second, "m2", write=40_000, read=10)])

    page = client.get(f"/wo/proj_a/{wo_id}/debug").text

    assert "residual" in page
    assert ops.PREFIX_AUTHORITY in page
    assert "the prefix broke at turn 2" in page


# -- 7. cross-links both ways ---------------------------------------------------------

def test_the_bill_links_to_the_debug_page_for_a_work_order_only(client, dispatched):
    wo_id = dispatched["wo_id"]
    fo = ops.create_feature_order("proj_a", "a big ask", description="lots of it")

    bill = client.get(f"/cost/proj_a/{wo_id}")
    feature_bill = client.get(f"/cost/proj_a/{fo['id']}")

    assert f"/wo/proj_a/{wo_id}/debug" in bill.text
    # The debugging page is a work-order page: a feature order has no session, no turn
    # and no live frame, so the link would lead to a 404.
    assert "/debug" not in feature_bill.text


def test_the_debug_page_links_back_to_the_bill_and_the_work_order(client, dispatched):
    wo_id = dispatched["wo_id"]

    page = client.get(f"/wo/proj_a/{wo_id}/debug").text

    assert f"/cost/proj_a/{wo_id}" in page
    assert f'href="/wo/proj_a/{wo_id}"' in page


def test_the_work_order_page_links_to_the_debug_page(client, dispatched):
    """A page reachable only from the bill is unreachable in practice."""
    page = client.get(f"/wo/proj_a/{dispatched['wo_id']}").text
    assert f"/wo/proj_a/{dispatched['wo_id']}/debug" in page


# -- 8. `jarvis cost` says where the detail lives -------------------------------------

def test_jarvis_cost_names_the_three_commands_that_hold_the_detail(dispatched, capsys):
    assert cli.main(["cost", "proj_a"]) == 0

    out = capsys.readouterr().out
    assert "jarvis inspect" in out
    assert "jarvis watch" in out
    assert "jarvis wo why" in out


# -- 9. the poll does not bury the access log ----------------------------------------

def test_the_live_poll_is_quiet_while_it_succeeds_and_logged_when_it_fails(client,
                                                                          dispatched):
    """Same reason `/api/status` is quiet: a two-second poll is ~95% of the file and
    buries the pages the user actually opened. Failures are logged whatever the path."""
    ok = client.get(f"/api/wo/proj_a/{dispatched['wo_id']}/live")
    assert ok.status_code == 200
    logged = (uilog.access_log_path().read_text()
              if uilog.access_log_path().exists() else "")
    assert "/live" not in logged

    client.get("/api/wo/proj_a/wo-nope/live")
    assert "[404] GET /api/wo/proj_a/wo-nope/live" in uilog.access_log_path().read_text()


# -- 10. everything on this page is attacker-influenced text -------------------------

def test_tool_parameters_are_html_escaped(client, dispatched, transcripts):
    """`params`, prompts and transcript quotes are text a worker (or whatever it read)
    wrote. Jinja autoescape is never bypassed on any of it."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    at = _inside(store, wo_id, 1)
    transcripts(dispatched["session"],
                [prompt_row(at, "go"),
                 *tool_rows(at, at + 1, "t1", "Bash",
                            {"command": "<script>alert(1)</script>"}),
                 assistant_row(at + 2, "m1")])

    page = client.get(f"/wo/proj_a/{wo_id}/debug").text

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page


def test_the_anatomy_page_shows_the_navigation_counts(client, dispatched, transcripts):
    """§3: the page and `jarvis inspect` read the SAME literals, so neither can report a
    navigation reading the other contradicts."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    at = _inside(store, wo_id, 1)
    transcripts(dispatched["session"],
                [prompt_row(at, "go"),
                 *tool_rows(at, at + 1, "t1", "Bash",
                            {"command": "grep -rn total_for src/pricing.py"}),
                 *tool_rows(at + 1, at + 2, "t2", "mcp__serena__find_symbol",
                            {"name_path_pattern": "a"}),
                 *tool_rows(at + 2, at + 3, "t3", "Bash", {"description": "no command"}),
                 assistant_row(at + 4, "m1")])

    page = client.get(f"/wo/proj_a/{wo_id}/debug").text
    (unit,) = ops.inspect_report(wo_id, "proj_a")["units"]

    assert unit["nav"] == {"symbol_calls": 1, "source_nav_calls": 1, "unclassified": 1}
    assert inspection.nav_line(unit["nav"]) in page
