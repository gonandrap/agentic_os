"""Every query_filters() route lists newest first (spec 2026-11-09-filtered-views-newest-first §6)"""
import ast
import inspect

import pytest
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from jarvis import db, ops
from jarvis.central_store import CentralStore
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.ui import app as ui_app
from jarvis.ui.app import create_app

from test_time_in_state import backdate


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file, live_daemon):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(db, "now", lambda: now[0])

    def at(t):
        now[0] = 1_000_000.0 + t
    return at


def ids(rows):
    return [r["id"] for r in rows]


# A case seeds, then returns (url, rows picker, expected ids); deep reports stub ops (Q4141)

def dashboard(mp, clock):
    store = ProjectStore(ops.registered_project_paths()["proj_a"])
    a = store.create_work_order("needs you, oldest")
    b = store.create_work_order("running, older")
    c = store.create_work_order("running, newest")
    store.set_status(a["id"], "needs_review")
    for wo in (b, c):
        store.set_status(wo["id"], "running")
    backdate(store, a["id"], 0, 5)
    backdate(store, b["id"], 10, 15)
    backdate(store, c["id"], 20, 25)
    store.close()
    return [("/", lambda x: x["shown"], [a["id"], c["id"], b["id"]])]


def project(mp, clock):
    store = ProjectStore(ops.registered_project_paths()["proj_a"])
    done_late = store.create_work_order("created first, completed last")
    done_early = store.create_work_order("created later, completed first")
    review = store.create_work_order("needs you, oldest status")
    for wo in (done_late, done_early):
        store.set_status(wo["id"], "completed")
    store.set_status(review["id"], "needs_review")
    backdate(store, done_late["id"], 0, 300)
    backdate(store, done_early["id"], 10, 100)
    backdate(store, review["id"], 5, 6)
    store.close()
    rows = lambda x: x["rows"]  # noqa: E731
    return [("/project/proj_a?status=completed", rows, [done_late["id"], done_early["id"]]),
            ("/project/proj_a?status=all", rows,
             [review["id"], done_late["id"], done_early["id"]])]


def search_page(mp, clock):
    central = CentralStore()
    clock(0)
    old = central.add_backlog("proj_a", "zzprobe item")
    clock(100)
    new = central.add_backlog("proj_a", "zzprobe item")
    central.close()
    # Equal relevance: time breaks the tie (§4.1); an empty q lists nothing to order
    return [("/search?q=zzprobe&kind=backlog", lambda x: x["hits"], [new["id"], old["id"]])]


def inbox(mp, clock):
    central = CentralStore()
    clock(0)
    old = central.add_inbox("proj_a", "older note")
    clock(100)
    new = central.add_inbox("proj_a", "newer note")
    central.close()
    return [("/inbox", lambda x: [i for i in x["items"] if i["id"] in (old, new)], [new, old])]


def backlog(mp, clock):
    central = CentralStore()
    base = central.add_backlog("proj_a", "base")
    blocked = central.add_backlog("proj_a", "blocked", depends_on=[base["id"]])
    fresh = central.add_backlog("proj_a", "fresh")
    reviewed = central.add_backlog("proj_a", "reviewed late", status="dropped")
    dropped = central.add_backlog("proj_a", "created late", status="dropped")
    for item, created, at in ((base, 200, None), (blocked, 100, None), (fresh, 300, None),
                              (reviewed, 10, 900), (dropped, 20, None)):
        central.conn.execute("UPDATE backlog SET created_at=?, reviewed_at=? WHERE id=?",
                             (created, at, item["id"]))
    central.conn.commit()
    central.close()
    rows = lambda x: x["rows"]  # noqa: E731
    return [("/backlog?status=dropped", rows, [reviewed["id"], dropped["id"]]),
            ("/backlog", rows, [blocked["id"], fresh["id"], base["id"]])]


def knowledge(mp, clock):
    central = CentralStore()
    clock(0)
    pinned = central.add_knowledge("zz pinned and oldest")
    clock(100)
    old = central.add_knowledge("zz older")
    clock(200)
    new = central.add_knowledge("zz newer")
    central.pin_knowledge(pinned["id"])
    central.close()
    return [("/knowledge", lambda x: x["rows"], [pinned["id"], new["id"], old["id"]])]


def neo_page(mp, clock):
    neo = NeoStore()
    clock(0)
    early = neo.ask("proj_a", "wo-x", "asked first, answered last")
    clock(100)
    late = neo.ask("proj_a", "wo-x", "asked later, answered first")
    clock(200)
    neo.record_answer(late["id"], "yes", answered_by="user")
    clock(300)
    neo.record_answer(early["id"], "no", answered_by="user")
    neo.conn.commit()
    neo.close()
    return [("/neo", lambda x: x["history"], [early["id"], late["id"]])]


def gates_page(mp, clock):
    def gate(id, status, ts, escalated=False):
        return {"id": id, "status": status, "escalated": escalated, "closed_as": "",
                "uses": 0, "ts": ts, "project": "proj_a", "kind": "pr_merge"}
    mp.setattr(ops, "list_gates", lambda *a, **k: [
        gate("approved-new", "approved", 400), gate("held", "awaiting_case", 300),
        gate("neo", "pending", 200), gate("esc", "pending", 100, escalated=True),
        gate("approved-old", "approved", 50)])
    rows = lambda x: x["rows"]  # noqa: E731
    return [("/gates?state=approved", rows, ["approved-new", "approved-old"]),
            ("/gates", rows, ["esc", "neo", "held", "approved-new", "approved-old"])]


def alarms_page(mp, clock):
    def alarm(id, status, ts, decided_at=None):
        return {"id": id, "live": False, "subject_id": id, "review_status": "unreviewed",
                "alarm_status": status, "kind": "k", "project": "proj_a", "ts": ts,
                "decided_at": decided_at}
    mp.setattr(ops, "alarm_feed", lambda *a, **k: [
        alarm("esc", "escalated", 100), alarm("decided-late", "acked", 50, decided_at=400),
        alarm("acked", "acked", 300)])
    mp.setattr(ops, "alarm_review_queue", lambda *a, **k: [])
    rows = lambda x: x["history"]  # noqa: E731
    return [("/alarms?state=acked", rows, ["decided-late", "acked"]),
            ("/alarms", rows, ["esc", "decided-late", "acked"])]


def stuck_page(mp, clock):
    mp.setattr(ops, "stuck_report", lambda *a, **k: [
        {"id": f"wo-{n}", "project": "proj_a", "status": "running", "status_since": t,
         "stuck": stuck} for n, t, stuck in ((1, 100, True), (2, 200, False), (3, 300, False))])
    mp.setattr(ops, "stuck_unjudged", lambda *a, **k: [])
    return [("/stuck", lambda x: x["rows"], ["wo-1", "wo-3", "wo-2"])]


def morpheus_page(mp, clock):
    def item(id, state, ts, owed_by=""):
        return {"id": id, "project": "proj_a", "state": state, "owed_by": owed_by,
                "updated_ts": ts}
    items = [item("owed", "watching", 10, "user"), item("failed", "failed", 20),
             item("old", "watching", 30), item("new", "watching", 40),
             item("failed-owed", "failed", 5, "user")]
    mp.setattr(ops, "morpheus_report", lambda *a, **k: {
        "items": items, "counts": {"watching": 3, "failed": 2}})
    rows = lambda x: x["rows"]  # noqa: E731
    return [("/mo?state=failed", rows, ["failed", "failed-owed"]),
            # Pins sort as a tuple (§2), so owed-and-failed leads owed
            ("/mo", rows, ["failed-owed", "owed", "failed", "new", "old"])]


def release_page(mp, clock):
    def rel(id, status, ts):
        return {"id": id, "tag": id, "status": status, "ts": ts}
    pend = [{"id": f"p{t}", "origin": "wo", "subject": "s", "wo_id": "", "sha": "", "ts": t}
            for t in (10, 20)]
    mp.setattr(ops, "release_overview", lambda *a, **k: {
        "pending": pend, "project": "jarvis_os",
        "releases": [rel("failed", "failed", 10), rel("ok-new", "validated", 30),
                     rel("open", "pending", 20), rel("ok-old", "validated", 5)]})
    mp.setattr(ops, "release_projects", lambda *a, **k: [])
    rows = lambda x: x["releases"]  # noqa: E731
    return [("/release?status=validated", rows, ["ok-new", "ok-old"]),
            ("/release", rows, ["failed", "ok-new", "open", "ok-old"]),
            ("/release", lambda x: x["pending"], ["p20", "p10"])]


def release_item_page(mp, clock):
    def row(id, validation, ts):
        return {"id": id, "validation": validation, "origin": "wo", "subject": "s",
                "wo_id": "", "sha": "", "ts": ts}
    mp.setattr(ops, "release_detail", lambda *a, **k: {"rows": [
        row("failed", "failed", 10), row("ok-old", "validated", 20),
        row("ok-new", "validated", 30), row("open", "pending", 25)]})
    rows = lambda x: x["rows"]  # noqa: E731
    return [("/release/jarvis-1.0.0?validation=validated", rows, ["ok-new", "ok-old"]),
            ("/release/jarvis-1.0.0", rows, ["failed", "ok-new", "open", "ok-old"])]


def steward_page(mp, clock):
    def row(id, queue_ts, ts):
        return {"id": id, "project": "proj_a", "track": "t", "queue_ts": queue_ts, "ts": ts}
    mp.setattr(ops, "steward_queue_report", lambda *a, **k: {
        "asks": [], "waiting": [],
        "settled": [row("no-queue-ts", None, 300), row("requeued", 400, 10),
                    row("old-queue", 100, 500)]})
    return [("/steward?tab=settled", lambda x: x["tabs"]["settled"],
             ["requeued", "no-queue-ts", "old-queue"])]


def _evolution(mp, clock):
    def rule(id, last_fired, readable=True):
        return {"id": id, "hits": 1, "last_fired": last_fired, "readable": readable}
    mp.setattr(ops, "evolution_report", lambda *a, **k: {"by_gap_class": [], "per_rule": [
        rule("never", None), rule("unreadable", 10, readable=False), rule("old", 20),
        rule("new", 30)]})
    return [("/evolution", lambda x: x["rows"], ["unreadable", "new", "old", "never"])]


TABLE = {f.__name__: f for f in (
    dashboard, search_page, project, inbox, backlog, knowledge, neo_page, gates_page,
    alarms_page, stuck_page, morpheus_page, release_page, release_item_page, steward_page,
    _evolution)}
EXCLUDED = {
    "requestor_query": "helper, lists no rows",
    # Groups rows by ask; no section of the spec owns it (Q4141, bl-d393d1d4)
    "yours_page": "added after the spec by wo-22eaa522; its order is undecided",
}


@pytest.mark.parametrize("route", TABLE)
def test_every_filtered_route_lists_newest_first(route, client, project, clock, monkeypatch):
    cases = TABLE[route](monkeypatch, clock)
    seen = []

    def capture(self, request, name, context, **kw):
        seen.append(context)
        return HTMLResponse(name)
    monkeypatch.setattr(Jinja2Templates, "TemplateResponse", capture)
    for url, rows, expected in cases:
        seen.clear()
        r = client.get(url)
        assert r.status_code == 200 and r.text != "error.html", url
        assert ids(rows(seen[-1])) == expected, url


def _calls_query_filters(fn):
    """A direct call in fn's own body; a nested def is its own call site"""
    todo = list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "query_filters":
            return True
        todo.extend(ast.iter_child_nodes(node))
    return False


def test_every_query_filters_call_site_is_in_the_table_or_excluded():
    tree = ast.parse(inspect.getsource(ui_app))
    sites = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _calls_query_filters(n)}
    assert sites == set(TABLE) | set(EXCLUDED)
