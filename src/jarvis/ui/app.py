"""Jarvis web dashboard — server-rendered, zero JS, reads the same stores and calls
the same ops functions as the CLI. Binds to localhost by default (no auth in MVP)."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import invariants, ops, uilog
from ..central_store import CentralStore
from ..daemon import daemon_running
from ..paths import PRODUCTION, deployment_env
from ..project_store import (
    ACTIVE_STATUSES,
    FO_OPEN_STATUSES,
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    WO_STATUSES,
    ProjectStore,
)
from ..timeline import build_timeline, count_debug

TEMPLATES = Path(__file__).parent / "templates"

STATUS_META = {
    "pending":       {"word": "pending",     "icon": "◌", "tone": "muted"},
    "dispatching":   {"word": "dispatching", "icon": "◍", "tone": "active"},
    "running":       {"word": "running",     "icon": "●", "tone": "active"},
    "waiting_input": {"word": "waiting on you", "icon": "◉", "tone": "warn"},
    "needs_review":  {"word": "needs review",   "icon": "◭", "tone": "warn"},
    "waiting_pr_merge": {"word": "waiting for PR merge", "icon": "⑃", "tone": "ok"},
    "completed":     {"word": "completed",   "icon": "✓", "tone": "ok"},
    "failed":        {"word": "failed",      "icon": "✗", "tone": "bad"},
    "cancelled":     {"word": "cancelled",   "icon": "–", "tone": "muted"},
}
# A feature order's own lifecycle. Separate from STATUS_META rather than merged into it:
# the words overlap ("pending", "completed") but they mean different things — a pending
# work order is queued for a worker, a pending feature order has not been decomposed yet
# — and one table serving both would have to pick one meaning for the shared keys.
FO_STATUS_META = {
    "pending":     {"word": "not planned yet", "icon": "◌", "tone": "muted"},
    "planning":    {"word": "planning",     "icon": "◍", "tone": "active"},
    "plan_review": {"word": "plan in review", "icon": "◭", "tone": "warn"},
    "executing":   {"word": "executing",    "icon": "●", "tone": "active"},
    "completed":   {"word": "completed",    "icon": "✓", "tone": "ok"},
    "failed":      {"word": "failed",       "icon": "✗", "tone": "bad"},
    "cancelled":   {"word": "cancelled",    "icon": "–", "tone": "muted"},
}
ORIGIN_META = {
    "jarvis": {"word": "jarvis", "framework": True},
    "ui":     {"word": "ui",     "framework": True},
    "manual": {"word": "manual", "framework": False},
    # `injected` is not a warning: the user handed this session over on purpose. `adhoc`
    # is the legacy marker from when Jarvis adopted sessions on its own, and stays one.
    "injected": {"word": "injected", "framework": True},
    "adhoc":  {"word": "ad-hoc", "framework": False},
    # Framework-spawned like `jarvis`, but nobody asked for it by hand: Neo filed it
    # itself to correct a contradicting ledger entry.
    "neo":    {"word": "neo",    "framework": True},
}
LEVEL_TONE = {"info": "muted", "warning": "warn", "critical": "bad"}
# Privileged-action gates. `pending` splits in two on the page — with Neo (costs the
# user nothing) vs escalated to the user — so it carries the neutral mark here.
# `dismissed` is neutral-toned on purpose: nothing was permitted and nothing was
# refused, so neither the ok nor the bad colour tells the truth about it.
GATE_META = {
    "pending":   {"word": "pending",   "icon": "◌", "tone": "warn"},
    "approved":  {"word": "approved",  "icon": "✓", "tone": "ok"},
    "denied":    {"word": "denied",    "icon": "✗", "tone": "bad"},
    "dismissed": {"word": "not a gate", "icon": "⊘", "tone": "muted"},
    "expired":   {"word": "expired",   "icon": "–", "tone": "muted"},
}

# How often the dashboard re-reads OS state. Not a page reload — the browser swaps
# the live regions in place (see dashboard.html), so in-progress typing survives.
REFRESH_SECONDS = 15

# The open statuses worth a row of their own, in this order: the two that owe the user a
# decision, then what is being worked on right now, then what is waiting for them to go
# and merge it. Only `pending` and `dispatching` collapse into a count line, the way
# settled work orders do — the point of the page is "what needs me", and a work order the
# OS has not started yet needs nobody.
#
# `needs_review` and `waiting_input` are here because invariants.true_blockers names them
# as genuine blockers, and a listing that disagrees with that is a bug in the listing.
# Collapsing them was one: a project whose only open work was three `needs_review` orders
# counted them in the header and then said "nothing running and nothing to merge" in the
# same breath. Status, not the attention flag, decides: `jarvis wo ack` puts the flag down
# for good, and an acked work order that vanished from the only listing of open work would
# be unfindable.
FEATURED_STATUSES = ("needs_review", "waiting_input", "running", "waiting_pr_merge")


def group_open(wos: list[dict], revealed: str = "") -> tuple[list[dict], list[dict]]:
    """Split open work orders into the featured rows and the rest.

    The rest come back only when the user has expanded them (`revealed` is their status
    or "all"); the caller renders the counts either way, so nothing is hidden silently.
    Featured rows are ordered by FEATURED_STATUSES, then by whatever order they arrived
    in (newest first out of the store).
    """
    featured = [wo for status in FEATURED_STATUSES for wo in wos
                if wo["status"] == status]
    rest = [wo for wo in wos if wo["status"] not in FEATURED_STATUSES]
    shown = [wo for wo in rest if revealed in ("all", wo["status"])]
    return featured, shown


def _counts_by_status(statuses) -> list[tuple[str, int]]:
    """(status, n) pairs in WO_STATUSES order — a stable line, not dict order."""
    counted = Counter(statuses)
    return [(s, counted[s]) for s in WO_STATUSES if counted.get(s)]


def fmt_age(ts: float | None) -> str:
    if not ts:
        return "–"
    d = time.time() - ts
    for limit, unit, div in ((90, "s", 1), (5400, "m", 60), (129600, "h", 3600)):
        if d < limit:
            return f"{int(d / div)}{unit}"
    return f"{int(d / 86400)}d"


def fmt_ts(ts: float | None) -> str:
    """An absolute local date-time. `fmt_age` answers "how long ago", which is the right
    question for a running thing and the wrong one for a bill: a sealed bill's date is a
    fact about the record, not a countdown, and "412d" is not a date anyone can cite."""
    if not ts:
        return "–"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


#: Paths the access log ignores while they succeed. `/api/status` is the dashboard's own
#: 15-second refresh poll — left in, it is ~95% of the lines and buries the thing the
#: access log exists to show: which pages the *user* actually opened. Failures are logged
#: whatever the path, so a broken poll still leaves a trace.
QUIET_PATHS = ("/api/status",)


def _rel_url(request: Request) -> str:
    """Path plus query — `?error=…` is often the whole story of a failed click."""
    q = request.url.query
    return request.url.path + (f"?{q}" if q else "")


def instance_badge() -> dict[str, str | bool]:
    """Which Jarvis this dashboard is driving, for the header.

    Production and development are the same code in two checkouts on one machine
    (docs/DEPLOYMENT.md), and their dashboards are otherwise identical — so the badge
    is the only thing stopping someone from acting on the live fleet while believing
    they are in the dev sandbox. Both facts are read once per process: neither the
    environment nor the installed version can change under a running server.

    The version is the running one, never a constant in the source: on `main` the
    version string deliberately lags the shipped tag (only release branches carry the
    bump), so a literal would be wrong in exactly the place it matters most.
    """
    from ..bugreport import jarvis_version
    env, detail = deployment_env()
    try:
        version = jarvis_version()
    except Exception:  # noqa: BLE001 — see gate_badge: a badge must not 500 a page
        version = "unknown"
    return {"env": env, "prod": env == PRODUCTION, "version": version,
            "shown": _version_for_badge(version),
            "label": "prod" if env == PRODUCTION else "dev",
            "detail": f"{env} · {detail} · version {version}"}


def _version_for_badge(version: str) -> str:
    """`0.5.0` → `v0.5.0`; `dev-a1b2c3d` → `a1b2c3d`.

    `bugreport.jarvis_version` reports a release as a bare number and everything else
    as `dev-<sha>` (a dev build is not "0.5.0 plus a bit"). The badge already says
    which instance this is, so pasting that in raw would read `dev · vdev-a1b2c3d` —
    the word twice, and a `v` in front of a commit sha. Only a real version number
    gets the `v`; the tooltip carries the string unaltered either way.
    """
    if version.startswith("dev-"):
        return version[len("dev-"):]
    return f"v{version}" if version[:1].isdigit() else version


def _false_positive_rate(rows: list) -> str | None:
    """"3 of 11 (27%)" — how often the gate fired on a command that ships nothing.

    Measured over requests that were actually ruled on. Pending ones are excluded
    because they have no answer yet, and including them would drag the rate down
    towards zero simply by being slow to review.
    """
    ruled = [g for g in rows if g["status"] != "pending"]
    if not ruled:
        return None
    n = sum(1 for g in ruled if g["status"] == "dismissed")
    return f"{n} of {len(ruled)} ({round(100 * n / len(ruled))}%)"


def gate_badge() -> int | None:
    """How many gates are waiting on the user, for the nav.

    Only escalated ones count: a request Neo is still reviewing is deliberately
    free of charge, and badging it would undo the point of having Neo. Never
    raises — a badge must not be the reason a page 500s.
    """
    try:
        return len([g for g in ops.list_gates(pending_only=True)
                    if g["escalated"]]) or None
    except Exception:  # noqa: BLE001 — see docstring
        return None


def _decorate_question(q: dict) -> dict:
    """Add the two display-only fields the `/neo` question blocks render.

    `digest_view` is the shortened rendering (`jarvis.digest`), or None when there
    isn't one — never attempted, attempted and failed, or the row predates the feature.
    The template shows the full question in every one of those cases, so a page with no
    digests anywhere is the page as it was before this existed.

    `full_context` is what the disclosure reveals: EXACTLY the question-specific prompt
    Neo was sent, built by the same function that builds it for the call. Not the system
    prompt — that is the persona plus every learning, identical on every question, and
    putting it here would bury the one thing the user opened the disclosure to read.
    Building it here rather than storing it means it cannot drift from what Neo gets.
    """
    from .. import digest, neo
    q["digest_view"] = digest.decode(q.get("digest"))
    q["full_context"] = neo.build_question_prompt(q)
    return q


def _digest_credit() -> str:
    """Attribution for the vendored output style, shown once on the page."""
    from .. import digest
    return digest.skill_attribution()


def fmt_tok(n: int | None) -> str:
    """Token counts, at the magnitude a reader actually compares — 1.3M, 47k, 812.

    Mirrors `cli._tok`, deliberately as a second small implementation rather than an
    import: the CLI's is a private helper of a module the UI has no other reason to
    load, and the shared thing here is a formatting convention, not behaviour.
    """
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_dur(seconds: float | None) -> str:
    """Turn durations at a readable magnitude — 1.2h, 4m, 15s.

    Mirrors `cli._dur` the same way `fmt_tok` mirrors `cli._tok`: the shared thing is
    a formatting convention, not behaviour.
    """
    if seconds is None:
        return "—"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def wo_bill(wo_id: str, project: str) -> dict | None:
    """One work order's itemised bill, or None if there is nothing measurable to show.

    NEVER RAISES, and that is the whole contract. This is a read of Claude Code's
    transcripts — files Jarvis does not own, that it prunes on its own schedule, and
    whose format is not Jarvis's to guarantee. The work order page has to render when a
    worker is blocked on a gate, and a page that 500s because a JSONL file moved would
    take that decision surface down with it. None means "no figure to show" and the
    template simply omits the line.

    A bill with no tokens on it is None too: an order whose transcript has been pruned
    and which cost the OS nothing has nothing to itemise, and a page of zeroes reads as
    a claim that it was free.
    """
    try:
        bill = ops.bill(wo_id, project=project)
    except Exception:  # noqa: BLE001 — see docstring
        return None
    return bill if bill["total"]["tokens"]["total"] else None


def turn_lines_by_message(bill: dict | None) -> dict[int, dict]:
    """message id → the bill line for the turn that message set going.

    So the conversation can carry its own cost: a message is the ASK, and what it cost
    is the turn that answered it — the same line, the same numbers, shown where the
    reader is already looking rather than only on a separate page. Never a second
    accounting: it is a reference to a line that is already in the totals, which is why
    the page says "the turn it started" rather than presenting it as the message's own.
    """
    if not bill:
        return {}
    out: dict[int, dict] = {}
    for line in bill.get("turns") or []:
        msg_id = (line.get("turn") or {}).get("msg_id")
        if msg_id:
            out[msg_id] = line
    return out


def create_app() -> FastAPI:
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        # Every page reflects live OS state (inbox acks, work order status); a
        # browser serving a stale copy from disk cache or history (bfcache) after
        # the user navigates back would show notifications as still unacked.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.globals.update(
        status_meta=STATUS_META, origin_meta=ORIGIN_META, gate_meta=GATE_META,
        fo_status_meta=FO_STATUS_META, level_tone=LEVEL_TONE, fmt_age=fmt_age,
        # "a worker turn may be in flight right now", so the page can withhold the
        # `claude --resume` invitation rather than put a second driver on one session.
        active_statuses=ACTIVE_STATUSES,
        instance=instance_badge(),
        fmt_tok=fmt_tok, fmt_dur=fmt_dur, fmt_ts=fmt_ts,
    )

    def render(request: Request, template: str, active: str = "dashboard",
               status_code: int = 200, **ctx) -> HTMLResponse:
        from ..neo_store import NeoStore
        ctx["active"] = active
        ctx["daemon_up"] = daemon_running() is not None
        neo = NeoStore()
        try:
            c = neo.counts()
        finally:
            neo.close()
        ctx["neo_badge"] = (c.get("escalated", 0) + c.get("failed", 0)
                            + c.get("unreviewed", 0)) or None
        ctx["gate_badge"] = gate_badge()
        return templates.TemplateResponse(request, template, ctx,
                                          status_code=status_code)

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> HTMLResponse:
        """Last line of defence: a bare "Internal Server Error" tells the user
        nothing, and a dead-end deep link out of a Telegram alert is exactly where
        they land. Name the failure on the page and put the traceback on disk."""
        uilog.record_error(request.method, request.url.path, exc)
        message = (f"Something went wrong loading {request.url.path} — "
                   f"{type(exc).__name__}: {exc}. "
                   f"The full traceback is in {uilog.ui_log_path()}, and the daemon "
                   "will raise it in `jarvis inbox` on its next tick.")
        try:
            return render(request, "error.html", message=message, status_code=500)
        except Exception:  # noqa: BLE001 — the chrome itself may be what broke
            return HTMLResponse(f"<h1>Something went wrong</h1><p>{message}</p>",
                                status_code=500)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        """One line per request in `$JARVIS_HOME/logs/ui-access.log`.

        Uvicorn runs at log_level='warning' and its access log would go to the journal
        anyway. Without this there is no record of which deep links were followed or
        which of them failed, which is what made "I clicked the link and got an internal
        server error" impossible to place in time.
        """
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The handler above renders the page, but it runs *outside* this middleware
            # (ServerErrorMiddleware is outermost), so this is the only place that sees
            # both the failure and the elapsed time.
            uilog.record_access(request.method, _rel_url(request), 500,
                                (time.perf_counter() - t0) * 1000)
            raise
        if response.status_code >= 400 or request.url.path not in QUIET_PATHS:
            uilog.record_access(request.method, _rel_url(request),
                                response.status_code,
                                (time.perf_counter() - t0) * 1000)
        return response

    # -- pages ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, show: str = ""):
        """The pulse. Open work orders are grouped so the ones the user can act on —
        a decision they owe, running work, a PR waiting for them to merge it — are
        rows, and the rest is a count line they can expand (`show` = a status name,
        or "all")."""
        st = ops.os_status()
        rows = [{**wo, "project": p["name"]}
                for p in st["projects"] for wo in p.get("open_work_orders", [])]
        revealed = show if show == "all" or any(wo["status"] == show for wo in rows) \
            else ""
        featured, rest = group_open(rows, revealed)
        rest_counts = _counts_by_status(
            wo["status"] for wo in rows if wo["status"] not in FEATURED_STATUSES)
        return render(request, "dashboard.html", st=st, refresh=REFRESH_SECONDS,
                      featured=featured, rest=rest, rest_counts=rest_counts,
                      revealed=revealed)

    @app.get("/project/{name}", response_class=HTMLResponse)
    def project(request: Request, name: str, hidden: str = "", show: str = ""):
        """A project's work orders — the ones that want the user, by default.

        Work orders owing the user a decision, running ones, and PRs waiting to be
        merged get a row each. Everything else — work the OS has not started yet, and
        the settled orders that are the bulk of any project with history — collapses
        into per-status counts the user can expand. `show` is "" (featured only), any
        status name (plus that group), or "all".
        """
        paths = ops.registered_project_paths()
        if name not in paths:
            return render(request, "error.html", message=f"unknown project {name!r}")
        show_hidden = hidden not in ("", "0", "false")
        revealed = show if show in WO_STATUSES else ("all" if show == "all" else "")
        if revealed == "all":
            statuses = None
        else:
            statuses = OPEN_STATUSES + ((revealed,) if revealed else ())
        store = ProjectStore(paths[name])
        try:
            # Open feature orders get their own short list above the work orders, and
            # deliberately do NOT expand into their children here: the children are
            # already in the listing below as ordinary work orders, and printing the
            # tree twice on one page is how a page stops being read. The tree lives on
            # the feature's own page.
            features = [{**fo, "progress": ops.feature_progress(store, fo)}
                        for fo in store.list_feature_orders(statuses=FO_OPEN_STATUSES)]
            wos = store.list_work_orders(statuses=statuses, include_hidden=show_hidden)
            # Inside the store's lifetime: the label reads the dependencies' own rows.
            blocked = {wo["id"]: ops.blocked_by(store, wo) for wo in wos}
            blocked = {k: v for k, v in blocked.items() if v}
            # Same lifetime, same reason: the note reads this work order's last turn.
            pauses = {wo["id"]: invariants.pause_note(store, wo) for wo in wos}
            pauses = {k: v for k, v in pauses.items() if v}
            visible_counts = store.status_counts()
            all_counts = store.status_counts(include_hidden=True)
            counts = all_counts if show_hidden else visible_counts
            hidden_count = sum(all_counts.values()) - sum(visible_counts.values())
        finally:
            store.close()
        central = CentralStore()
        try:
            backlog = central.list_backlog(project=name, status="open")
        finally:
            central.close()
        settled = [(s, counts[s]) for s in TERMINAL_STATUSES if counts.get(s)]
        featured, rest = group_open(wos, revealed)
        # Counted in SQL rather than from `wos`, which is capped at a page: the same
        # reason the settled line is. Terminal statuses have their own line below.
        other_open = [s for s in OPEN_STATUSES if s not in FEATURED_STATUSES]
        open_counts = [(s, counts[s]) for s in other_open if counts.get(s)]
        return render(request, "project.html", project_name=name, path=paths[name],
                      featured=featured, rest=rest, open_counts=open_counts,
                      backlog=backlog, show_hidden=show_hidden, blocked=blocked,
                      pauses=pauses,
                      hidden_count=hidden_count, settled=settled, revealed=revealed,
                      features=features)

    @app.get("/fo/{name}/{fo_id}", response_class=HTMLResponse)
    def feature_order(request: Request, name: str, fo_id: str, error: str = ""):
        """A feature order's page. Its main content is the dependency tree.

        The one view where the whole plan is visible at once — the ask, the
        decomposition as it was submitted, and each child's live status against it. It
        is also where an escalated plan is decided, because deciding needs all three of
        those on one screen and no other page has them.
        """
        try:
            detail = ops.show_feature_order(fo_id, name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e))
        return render(request, "feature_order.html", fo=detail, project=detail["project"],
                      error=error)

    @app.get("/project/{name}/sessions", response_class=HTMLResponse)
    def project_sessions(request: Request, name: str):
        """The inject panel, as a fragment the project page pulls in after it renders.

        Separate from the page on purpose: this is the one view that shells out to
        `claude agents --json`, and a slow or missing CLI must cost the project page
        nothing. `ops.injectable_sessions` never raises — it returns the error as text
        for the fragment to show inline.
        """
        found = ops.injectable_sessions(name)
        return templates.TemplateResponse(
            request, "_sessions.html",
            {"project_name": name, "sessions": found["sessions"],
             "error": found["error"]},
        )

    @app.get("/wo/{name}/{wo_id}", response_class=HTMLResponse)
    def work_order(request: Request, name: str, wo_id: str, debug: str = ""):
        try:
            pname, path, wo = ops.find_work_order(wo_id, name)
        except ops.OpsError as e:
            # Almost every visitor who lands here followed a deep link out of a
            # notification, so the useful answer is "your link is stale", not the
            # lookup's own phrasing. Say which half of the link went bad.
            known = sorted(ops.registered_project_paths())
            if name not in known:
                hint = (f"This link points at project {name!r}, which the OS does not "
                        f"know about — it was never registered, or it has since been "
                        f"removed from the catalog. Registered projects: "
                        f"{', '.join(known) or 'none'}.")
            else:
                hint = (f"Project {name!r} is registered, but it has no work order "
                        f"{wo_id!r} — the link is from before it existed, or the work "
                        f"order was deleted (`jarvis wo delete` erases the record).")
            return render(request, "error.html", message=str(e), hint=hint,
                          status_code=404)
        store = ProjectStore(path)
        try:
            events = store.list_events(wo_id)
            messages = store.list_messages(wo_id)
            assumptions = store.pending_assumptions(wo_id)
            # A worker held at a gate looks identical to an idle one from here, so
            # the reason it stopped belongs on the page it stopped on.
            store.expire_approvals()
            approvals = store.list_approvals(wo_id)
            # Why a `running` work order has nothing running. Same idea as the gate
            # line above: the reason it stopped belongs on the page it stopped on.
            pause = invariants.pause_note(store, wo)
            # And why a `waiting_input` one is not in fact waiting on the reader. Both
            # notes are display; `true_blockers` decides what actually costs attention.
            waiting = ops.waiting_on(store, wo)
        finally:
            store.close()
        show_debug = debug not in ("", "0", "false")
        bill = wo_bill(wo_id, pname)
        return render(request, "work_order.html", project=pname, wo=wo,
                      pause=pause, waiting=waiting,
                      timeline=build_timeline(wo, events, messages,
                                              include_debug=show_debug,
                                              questions=ops.neo_question_texts(wo_id)),
                      debug=show_debug, debug_count=count_debug(events),
                      messages=messages, assumptions=assumptions,
                      approvals=approvals, bill=bill,
                      turn_lines=turn_lines_by_message(bill))

    @app.get("/cost", response_class=HTMLResponse)
    def cost_page(request: Request, project: str = ""):
        """What the fleet's work cost, dearest first — the dashboard half of `jarvis cost`.

        Its own page rather than a column on the dashboard: this reads and parses every
        session transcript Claude Code still holds (~0.4s for a fleet of sixty), and the
        dashboard re-reads itself every 15 seconds. Spend is a question someone asks
        deliberately, not one worth paying for on every pulse.
        """
        try:
            report = ops.cost_report(project=project or None)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e))
        return render(request, "cost.html", active="cost", report=report,
                      units=report["units"], totals=report["totals"],
                      project=project,
                      projects=sorted(ops.registered_project_paths()))

    @app.get("/cost/{name}/{order_id}", response_class=HTMLResponse)
    def bill_page(request: Request, name: str, order_id: str):
        """One order's itemised bill — the page that answers "where did my tokens go".

        Work orders and feature orders share it, because the question and the answer's
        shape are the same and only the depth differs: a feature order's bill expands
        into its orders, each order's into its turns, each turn's into the worker, the
        OS calls it caused and the processes it spawned. `jarvis.bill` reconciles every
        level before rendering, and the page states the result rather than assuming it.
        """
        try:
            bill = ops.bill(order_id, project=name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e), status_code=404)
        turn_rows = bill.get("turn_rows") or []
        # Bars are scaled to the context window when any turn reports one, else to the
        # largest peak on the page — growth stays comparable either way.
        scale = max([t.get("context_window") or 0 for t in turn_rows]
                    + [t.get("context_peak") or 0 for t in turn_rows] + [1])
        return render(request, "bill.html", active="cost", bill=bill, project=name,
                      turn_rows=turn_rows, bar_scale=scale)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        central = CentralStore()
        try:
            items = central.unacked_inbox()
        finally:
            central.close()
        return render(request, "inbox.html", active="inbox", items=items)

    @app.get("/backlog", response_class=HTMLResponse)
    def backlog(request: Request):
        central = CentralStore()
        try:
            items = central.list_backlog(status=None)
            open_ids = {i["id"] for i in items if i["status"] == "open"}
            blockers = {i["id"]: central.unfinished_dependencies(i["id"])
                        for i in items if i["id"] in open_ids}
        finally:
            central.close()
        return render(request, "backlog.html", active="backlog", items=items,
                      blockers=blockers)

    @app.get("/knowledge", response_class=HTMLResponse)
    def knowledge(request: Request):
        from ..central_store import PINNED_TAG, has_tag
        central = CentralStore()
        try:
            rows = central.search_knowledge("", limit=200)
            topics = central.knowledge_topics()
        finally:
            central.close()
        rows = sorted(rows, key=lambda r: (not has_tag(r["tags"], PINNED_TAG), -r["ts"]))
        for r in rows:
            r["pinned"] = has_tag(r["tags"], PINNED_TAG)
        return render(request, "knowledge.html", active="knowledge", rows=rows,
                      topics=topics)

    @app.post("/knowledge/pin")
    def knowledge_pin(kn_id: str = Form(...), pinned: str = Form("")):
        """Pinned entries ride in every worker prompt verbatim; everything else is an
        index line the worker looks up. This is the toggle between the two."""
        central = CentralStore()
        try:
            central.pin_knowledge(kn_id, pinned=pinned == "1")
        finally:
            central.close()
        return RedirectResponse("/knowledge", status_code=303)

    @app.get("/neo", response_class=HTMLResponse)
    def neo_page(request: Request):
        from ..neo_store import NeoStore
        neo = NeoStore()
        try:
            counts = neo.counts()
            # Oldest first: that is the order Neo drains them, and the oldest is the
            # one most likely to be stuck.
            in_flight = list(reversed(
                neo.list_questions(statuses=("queued", "answering"))))
            escalated = neo.list_questions(statuses=("escalated", "failed"))
            unreviewed = neo.list_questions(statuses=("answered",),
                                            review_status="unreviewed")
            unreviewed = [q for q in unreviewed if q["answered_by"] == "neo"]
            history = [q for q in neo.list_questions(limit=100)
                       if q["status"] == "answered"
                       and not (q["answered_by"] == "neo"
                                and q["review_status"] == "unreviewed")]
            learnings = neo.all_learnings(limit=100)
            # How the panel deliberated, for the questions this page already shows.
            # Deliberation is inspectable on demand and never pushed at anyone, so it
            # renders collapsed and a question with no opinions gets no block at all —
            # which is also why this dict holds only the questions that HAVE them.
            opinions = {}
            for q in (*in_flight, *escalated, *unreviewed, *history):
                rows = neo.opinions(q["id"])
                if rows:
                    opinions[q["id"]] = rows
        finally:
            neo.close()
        for q in escalated + unreviewed:
            _decorate_question(q)
        return render(request, "neo.html", active="neo", counts=counts,
                      in_flight=in_flight, escalated=escalated,
                      unreviewed=unreviewed, history=history, learnings=learnings,
                      opinions=opinions, digest_credit=_digest_credit())

    @app.get("/gates", response_class=HTMLResponse)
    def gates_page(request: Request):
        """Privileged-action approvals. Four states, four different asks of the
        user: escalated ones need a decision, ones still with Neo need nothing (but
        can be pre-empted), decided ones are the audit trail — and dismissed ones are
        not an audit trail at all.

        The dismissed ones are split out rather than listed with the verdicts because
        they are a different measurement: they say nothing about what the fleet was
        allowed to ship, and everything about how often the OS's own recogniser is
        wrong. Mixed into the decided table they would read as approvals-by-another-name
        and the false-positive rate would be invisible, which is the whole reason the
        verdict is separate from `approved`.
        """
        rows = ops.list_gates(include_request=True)
        pending = [g for g in rows if g["status"] == "pending"]
        dismissed = [g for g in rows if g["status"] == "dismissed"]
        decided = [g for g in rows if g["status"] not in ("pending", "dismissed")]
        return render(request, "gates.html", active="gates",
                      escalated=[g for g in pending if g["escalated"]],
                      with_neo=[g for g in pending if not g["escalated"]],
                      decided=decided, dismissed=dismissed,
                      false_positive_rate=_false_positive_rate(rows))

    @app.get("/api/status")
    def api_status():
        return JSONResponse(ops.os_status())

    # -- actions (same ops functions as the CLI) --------------------------------------

    @app.post("/wo/create")
    def create_wo(project: str = Form(...), title: str = Form(...),
                  description: str = Form(""), model: str = Form("")):
        try:
            wo = ops.create_work_order(project, title, description=description,
                                       model=model or None, origin="ui")
        except ops.OpsError as e:
            return RedirectResponse(f"/?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{project}/{wo['id']}", status_code=303)

    @app.post("/project/{name}/inject")
    def inject(name: str, session_id: str = Form(...), title: str = Form("")):
        """Hand one of the user's own Claude sessions to Jarvis. Creates the record and
        nothing else — nothing is written into the session until they send it a message."""
        try:
            res = ops.inject_session(session_id, project_name=name, title=title or None)
        except ops.OpsError as e:
            return RedirectResponse(f"/project/{name}?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{name}/{res['wo_id']}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/send")
    def send(name: str, wo_id: str, message: str = Form(...)):
        ops.send_message(wo_id, message, source="ui", project_name=name)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/review")
    def review(name: str, wo_id: str, decision: str = Form(...),
               feedback: str = Form("")):
        ops.review_work_order(wo_id, accept=(decision == "accept"), feedback=feedback)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/cancel")
    def cancel_wo(name: str, wo_id: str):
        ops.cancel(wo_id)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/done")
    def done_wo(name: str, wo_id: str):
        try:
            ops.mark_done(wo_id, project_name=name)
        except ops.OpsError as e:
            # Pending assumptions are the one refusal: they want a decision, not a
            # close. The panel that takes it is on this same page.
            return RedirectResponse(f"/wo/{name}/{wo_id}?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/ack")
    def ack_wo(name: str, wo_id: str):
        try:
            ops.ack_attention(wo_id, project_name=name)
        except ops.OpsError as e:
            # The one case that refuses: pending assumptions want a decision, not a
            # dismissal. Say so instead of silently doing nothing.
            return RedirectResponse(f"/wo/{name}/{wo_id}?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/hide")
    def hide_wo(name: str, wo_id: str):
        ops.hide_work_order(wo_id, hidden=True, project_name=name)
        return RedirectResponse(f"/project/{name}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/unhide")
    def unhide_wo(name: str, wo_id: str):
        ops.hide_work_order(wo_id, hidden=False, project_name=name)
        return RedirectResponse(f"/project/{name}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/delete")
    def delete_wo(name: str, wo_id: str):
        ops.delete_work_order(wo_id, project_name=name)
        return RedirectResponse(f"/project/{name}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/resume-auto")
    def resume_auto(name: str, wo_id: str):
        ops.resume_in_auto(wo_id, project_name=name)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/fo/create")
    def create_fo(project: str = Form(...), title: str = Form(...),
                  description: str = Form("")):
        try:
            fo = ops.create_feature_order(project, title, description=description,
                                          origin="ui")
        except ops.OpsError as e:
            return RedirectResponse(f"/project/{project}?error={e}", status_code=303)
        return RedirectResponse(f"/fo/{project}/{fo['id']}", status_code=303)

    @app.post("/fo/{name}/{fo_id}/review")
    def review_plan(name: str, fo_id: str, decision: str = Form(...),
                    feedback: str = Form("")):
        try:
            ops.review_plan(fo_id, accept=(decision == "accept"), feedback=feedback,
                            decided_by="user", project_name=name)
        except ops.OpsError as e:
            # A rejection with no reason is the refusal that actually happens here: the
            # planner sees only the reason, so without it the revision is a guess.
            return RedirectResponse(f"/fo/{name}/{fo_id}?error={e}", status_code=303)
        return RedirectResponse(f"/fo/{name}/{fo_id}", status_code=303)

    @app.post("/fo/{name}/{fo_id}/cancel")
    def cancel_fo(name: str, fo_id: str):
        try:
            ops.cancel_feature_order(fo_id, project_name=name)
        except ops.OpsError as e:
            return RedirectResponse(f"/fo/{name}/{fo_id}?error={e}", status_code=303)
        return RedirectResponse(f"/fo/{name}/{fo_id}", status_code=303)

    @app.post("/neo/{question_id}/review")
    def neo_review(question_id: int, decision: str = Form(...),
                   feedback: str = Form("")):
        try:
            ops.neo_review(question_id, approved=(decision == "approve"),
                           feedback=feedback)
        except ops.OpsError as e:
            return RedirectResponse(f"/neo?error={e}", status_code=303)
        return RedirectResponse("/neo", status_code=303)

    @app.post("/neo/{question_id}/answer")
    def neo_answer(question_id: int, text: str = Form(...)):
        try:
            ops.neo_answer_escalated(question_id, text)
        except ops.OpsError as e:
            return RedirectResponse(f"/neo?error={e}", status_code=303)
        return RedirectResponse("/neo", status_code=303)

    @app.post("/neo/learn")
    def neo_learn(content: str = Form(...), project: str = Form("")):
        from ..neo_store import NeoStore
        neo = NeoStore()
        try:
            neo.add_learning(content, project=project, source="manual")
        finally:
            neo.close()
        return RedirectResponse("/neo", status_code=303)

    @app.post("/gates/{approval_id}/decide")
    def decide_gate(approval_id: int, decision: str = Form(...),
                    reason: str = Form(""), project: str = Form(""),
                    next: str = Form("")):
        """Open or refuse a gate. Approval ids are per-project autoincrements, so the
        form carries the project the row was rendered from — without it two projects
        holding the same id make `ops.decide_gate` refuse to guess.

        `next` returns the user to the page they decided from (the gates tab or a work
        order). Only same-site paths are honoured: a form field is attacker-settable,
        and an open redirect out of the dashboard is not worth the convenience.

        `dismiss` is the third button: it clears a command the recogniser matched by
        mistake without recording that any privileged action was authorised.
        """
        back = next if next.startswith("/") and not next.startswith("//") else "/gates"
        # Unknown button values fall through to `denied`, which is the fail-closed
        # reading: a mangled form must never be able to open a gate.
        verdict = {"approve": "approved", "deny": "denied",
                   "dismiss": "dismissed"}.get(decision, "denied")
        try:
            ops.decide_gate(approval_id, verdict=verdict,
                            reason=reason, project_name=project or None)
        except ops.OpsError as e:
            sep = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{sep}error={e}", status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/inbox/ack")
    def ack(inbox_id: str = Form("")):
        central = CentralStore()
        try:
            central.ack_inbox(int(inbox_id) if inbox_id else None)
        finally:
            central.close()
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/backlog/promote/{item_id}")
    def promote(item_id: str, force: str = Form("")):
        try:
            result = ops.promote_backlog(item_id, force=bool(force))
        except ops.OpsError as e:
            return RedirectResponse(f"/backlog?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{result['project']}/{result['wo_id']}", status_code=303)

    return app
