"""Jarvis web dashboard — server-rendered, zero JS, reads the same stores and calls
the same ops functions as the CLI. Binds to localhost by default (no auth in MVP)."""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import bill, fleet, invariants, ops, specs, uilog
from ..catalog import CatalogError
from ..central_store import CentralStore
from ..daemon import daemon_running
from ..inspection import ALARM_KINDS
from ..paths import PRODUCTION, deployment_env
from ..project_store import (
    ACTIVE_STATUSES,
    FO_OPEN_STATUSES,
    FO_STATUSES,
    FO_TERMINAL_STATUSES,
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    WO_STATUSES,
    ProjectStore,
)
from ..timeline import build_conversation, build_timeline, count_debug

TEMPLATES = Path(__file__).parent / "templates"


def _fleet_if_pending(wo: dict) -> "fleet.Fleet | None":
    """The account's state, but ONLY for a `pending` order (src/jarvis/fleet.py).

    Nothing else's label can change with it, and reading it opens every project's store —
    not a cost to pay on every page view. None when the catalog cannot be resolved: a
    page that cannot answer "why is it waiting" still has to render.
    """
    if wo["status"] != "pending":
        return None
    try:
        return fleet.current(ops.resolve_catalog())
    except (ops.OpsError, CatalogError):
        return None


STATUS_META = {
    "pending":       {"word": "pending",     "icon": "◌", "tone": "muted"},
    "dispatching":   {"word": "dispatching", "icon": "◍", "tone": "active"},
    "running":       {"word": "running",     "icon": "●", "tone": "active"},
    "waiting_input": {"word": "waiting on you", "icon": "◉", "tone": "warn"},
    # Toned `active`, not `warn`: a round in flight is the OS working, and nothing is
    # being asked of the user. Templates index this dict BY STATUS — every one of them
    # via `.get(status, <the raw status>)`, so the cost of a missing key is not a
    # traceback but every surface quietly printing the bare word instead of the meaning.
    "validating":    {"word": "under review",   "icon": "◑", "tone": "active"},
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
    # A feature order validates as a whole once every child is done — same reading as
    # the work-order entry above, one level up.
    "validating":  {"word": "under review",  "icon": "◑", "tone": "active"},
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


def alarm_badge() -> int | None:
    """How many work orders are asking for the user BECAUSE of a cost alarm.

    Counted over orders, not over events: several alarms on one turn are one ask and
    one ack. Never raises — a badge must not be the reason a page 500s.
    """
    try:
        return len({a["wo_id"] for a in ops.list_cost_alarms() if a["live"]}) or None
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


def config_setters(history: list[dict]) -> dict[str, str]:
    """path → the id of the most recent version that changed it.

    Only half of a key's provenance: a version's change list says what was WRITTEN at
    the time, and `ops.config_show`'s `written` says what the document still sets. A key
    the file no longer sets is on a default however many versions once named it.
    """
    setters: dict[str, str] = {}
    for row in reversed(history):  # oldest first, so the last writer wins
        for change in row["changes"]:
            setters[change["path"]] = row["id"]
    return setters


def config_scope_of(path: str) -> str:
    """The catalog block a resolved path lives in: `os`, or one `projects.<name>`."""
    parts = path.split(".")
    return "os" if parts[0] == "os" else ".".join(parts[:2])


def config_scopes(resolved: dict) -> list[dict]:
    """Every block the page can be pointed at, for the picker at the top.

    One scope is on screen at a time: the fleet's settings and forty projects' used
    to be one column the reader scrolled through to reach the project they came for.
    """
    names = sorted({p.split(".")[1] for p in resolved if p.startswith("projects.")})
    return ([{"key": "os", "title": "os — the fleet"}]
            + [{"key": f"projects.{n}", "title": n} for n in names])


def config_tree(labels: list[str], node: str = "") -> list[dict]:
    """One scope's dotted labels as the nested nodes a tree renders.

    Only an interior segment becomes a node: `neo.panel.fast_path` gives `neo` and
    `neo.panel`, and the leaf is a setting the node's page shows. `open` is what keeps
    the selected node's ancestors unfolded — the tree is plain HTML, so a node's state
    has to be decided here rather than by a click nobody is listening for.
    """
    def build(prefix: str, entries: list[str]) -> list[dict]:
        groups: dict[str, list[str]] = {}
        for label in entries:
            head, _, rest = label.partition(".")
            if rest:
                groups.setdefault(head, []).append(rest)
        out = []
        for head in sorted(groups):
            path = f"{prefix}{head}"
            out.append({"path": path, "name": head,
                        "count": sum(1 for x in labels if x.startswith(f"{path}.")),
                        "children": build(f"{path}.", groups[head]),
                        "here": node == path,
                        "open": node == path or node.startswith(f"{path}.")})
        return out
    return build("", labels)


CONFIG_TYPE_NAMES = [
    (bool, "true or false"), (int, "a whole number"), (float, "a number"),
    (str, "text"), (list, "a JSON list"), (dict, "a JSON object"),
]


def config_type_name(value: object) -> str:
    for kind, name in CONFIG_TYPE_NAMES:
        if isinstance(value, kind):
            return name
    return "a value"


def config_widget(value: object) -> str:
    """Which input a setting is edited with — the whole of the page's type awareness."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, dict)):
        return "json"
    return "text"


def config_input_text(value: object) -> str:
    """The submitted-text form of a value, so an edit starts from what is already set."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value)


def config_value_of(current: object, text: str) -> object:
    """What the page will write for `text`, given the value already there.

    A text setting takes its text VERBATIM — `os.defaults.model` is the string `123` if
    that is what the user typed — and everything else goes through the CLI's own parser,
    so `true`, `3` and `["a"]` mean on this page what they mean in the terminal.
    """
    if isinstance(current, str):
        return text
    if current is None and not text.strip():
        return None
    return ops.parse_config_value(text)


def config_type_ok(current: object, new: object) -> bool:
    """May `new` be written where `current` is? Neo, q193.

    The page is where this lives because the page is what has an untyped text box:
    `parse_catalog` takes `true` for `os.defaults.model` without a word, and answers a
    bad whole number with a bare `ValueError` rather than the `CatalogError` `ops`
    converts. Widening `ops.set_config`'s own contract is a different change.

    An unset optional takes anything; nothing else takes `null`, because a key present
    and null is not the same as absent and `jarvis config unset` is what clears one.
    """
    if current is None:
        return True
    if new is None:
        return False
    if isinstance(current, bool):
        return isinstance(new, bool)
    if isinstance(current, int):
        return isinstance(new, int) and not isinstance(new, bool)
    if isinstance(current, float):
        return isinstance(new, (int, float)) and not isinstance(new, bool)
    return isinstance(new, type(current))


def _config_nodes(label: str) -> list[str]:
    """Every interior node a dotted label passes through: `a.b.c` → `a`, `a.b`."""
    parts = label.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def _config_url(scope: str, node: str = "", q: str = "") -> str:
    """The page as it is being read right now, so a save comes back to it rather than
    dropping the reader at the top of the fleet's settings."""
    query = {k: v for k, v in (("scope", scope), ("node", node), ("q", q)) if v}
    return "/config" + (f"?{urlencode(query)}" if query else "")


def config_rows(show: dict, setters: dict[str, str], scope: str,
                node: str = "", query: str = "") -> list[dict]:
    """The settings one scope shows, narrowed by the tree node or by the search box.

    A SEARCH OVERRIDES THE NODE rather than narrowing it: someone typing `autocompact`
    is searching precisely because they do not know which node it is under, and a search
    inside the selected node answers nothing unless they had already guessed right.
    """
    resolved, written = show["resolved"], set(show["written"])
    needle = query.strip().lower()
    rows = []
    for path in sorted(resolved):
        if config_scope_of(path) != scope:
            continue
        label = path[len(scope) + 1:]
        if needle:
            if needle not in label.lower():
                continue
        elif node and not label.startswith(f"{node}."):
            continue
        value = resolved[path]
        rows.append({"path": path, "label": label, "value": value,
                     # Which node the match came out of, so a search result set can be
                     # grouped by it (Neo, q194) — the tree is not narrowed by a search,
                     # so the row has to say where it lives.
                     "node": label.rpartition(".")[0],
                     "widget": config_widget(value), "text": config_input_text(value),
                     "written": path in written, "safety": ops.safety_key(path),
                     "version": setters.get(path) if path in written else None})
    return rows


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
        # The bill's two explanations, taken from the module that computes the numbers
        # rather than written into the template: a caveat that says one thing on the
        # page and another in the terminal is one the reader learns to ignore.
        rate_note=bill.rate_note, tokens_mean=bill.TOKENS_MEAN,
        write_rate_of=bill.write_rate_of,
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
        ctx["alarm_badge"] = alarm_badge()
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
    def project(request: Request, name: str, hidden: str = "", show: str = "",
                fo: str = ""):
        """A project's work orders — the ones that want the user, by default.

        Work orders owing the user a decision, running ones, and PRs waiting to be
        merged get a row each. Everything else — work the OS has not started yet, and
        the settled orders that are the bulk of any project with history — collapses
        into per-status counts the user can expand. `show` is "" (featured only), any
        status name (plus that group), or "all".

        `fo` is the same reveal for the feature orders, on its own param rather than
        sharing `show`: the two lifecycles spell three settled statuses the same way
        (see FO_STATUS_META), so one param would expand both lists on every click.
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
        fo_revealed = fo if fo in FO_STATUSES else ("all" if fo == "all" else "")
        if fo_revealed == "all":
            fo_statuses = None
        else:
            fo_statuses = FO_OPEN_STATUSES + ((fo_revealed,) if fo_revealed else ())
        store = ProjectStore(paths[name])
        try:
            # Open feature orders get their own short list above the work orders, and
            # deliberately do NOT expand into their children here: the children are
            # already in the listing below as ordinary work orders, and printing the
            # tree twice on one page is how a page stops being read. The tree lives on
            # the feature's own page.
            features = [{**row, "progress": ops.feature_progress(store, row)}
                        for row in store.list_feature_orders(statuses=fo_statuses)]
            fo_counts = store.feature_status_counts()
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
        fo_settled = [(s, fo_counts[s]) for s in FO_TERMINAL_STATUSES
                      if fo_counts.get(s)]
        return render(request, "project.html", project_name=name, path=paths[name],
                      featured=featured, rest=rest, open_counts=open_counts,
                      backlog=backlog, show_hidden=show_hidden, blocked=blocked,
                      pauses=pauses,
                      hidden_count=hidden_count, settled=settled, revealed=revealed,
                      features=features, fo_settled=fo_settled,
                      fo_revealed=fo_revealed)

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
        # The deliberation, for the fold. `show_feature_order` deliberately does NOT
        # carry it — that document is what `jarvis fo show` prints, and the seats'
        # replies must not ride into it.
        store = ProjectStore(ops.registered_project_paths()[detail["project"]])
        try:
            validation = ops.validation_detail(store, fo_id=fo_id)
        finally:
            store.close()
        return render(request, "feature_order.html", fo=detail, project=detail["project"],
                      validation=validation, error=error)

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
            # Both lists: `assumptions` is the record, `unreviewed` is the ask — §4.
            assumptions = store.all_assumptions(wo_id)
            unreviewed = store.pending_assumptions(wo_id)
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
            # How this status should READ — derived once, in the one function every
            # other surface derives it from. The header used to build its own wording
            # out of STATUS_META alone, which is how a listing and a header came to
            # disagree about the same work order once before (PR 65).
            label = invariants.status_label(store, wo, _fleet_if_pending(wo))
            validation = ops.validation_detail(store, wo_id=wo_id)
            # WHERE THE REST OF THIS ORDER IS. The brief is deliberately only the margin
            # around a section of the feature's spec now, so a page that showed the brief
            # alone would be a page missing most of the work. `section_text` is NOT passed
            # to the template: pasting it here would re-create the duplication the whole
            # change removed — the pointer is the point.
            spec = specs.spec_of(store, wo)
        finally:
            store.close()
        show_debug = debug not in ("", "0", "false")
        bill = wo_bill(wo_id, pname)
        return render(request, "work_order.html", project=pname, wo=wo,
                      pause=pause, waiting=waiting, status_label=label,
                      validation=validation, spec=spec,
                      timeline=build_timeline(wo, events, messages,
                                              include_debug=show_debug),
                      debug=show_debug, debug_count=count_debug(events),
                      # What was said, and what happened — two readings of one record,
                      # neither derivable from the other. See `timeline`'s docstring.
                      conversation=build_conversation(events, messages),
                      assumptions=assumptions, unreviewed=unreviewed,
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
        """The base, and what it costs against what it is used for.

        Entries render as their INDEX LINE with the body behind a disclosure — the same
        160 characters a worker decides from. A page that dumps every entry in full is
        reading matter nobody has; the headline is the artefact that actually does the
        work, and seeing it truncated mid-sentence is the point.
        """
        from ..central_store import PINNED_TAG, has_tag, headline
        central = CentralStore()
        try:
            rows = central.search_knowledge("", limit=200)
            topics = central.knowledge_topics()
            hits = central.knowledge_hit_counts()
        finally:
            central.close()
        rows = sorted(rows, key=lambda r: (not has_tag(r["tags"], PINNED_TAG), -r["ts"]))
        for r in rows:
            r["pinned"] = has_tag(r["tags"], PINNED_TAG)
            r["headline"] = headline(r["content"])
            r["reads"] = hits.get(r["id"], 0)
        return render(request, "knowledge.html", active="knowledge", rows=rows,
                      topics=topics, usage=ops.knowledge_usage_report(limit=8))

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
        return render(request, "neo.html", active="neo",
                      in_flight=in_flight, escalated=escalated,
                      unreviewed=unreviewed, history=history, learnings=learnings,
                      opinions=opinions, digest_credit=_digest_credit())

    @app.get("/neo/question/{question_id}", response_class=HTMLResponse)
    def neo_question_page(request: Request, question_id: int):
        """One question and its answer — where a work order's timeline sends the reader.

        The `question_asked` entry points here rather than reprinting the question: §3 of
        docs/superpowers/specs/2026-08-23-the-work-order-record.md.
        """
        from ..neo_store import NeoStore
        neo = NeoStore()
        try:
            q = neo.get(question_id)
            opinions = neo.opinions(question_id) if q else []
        finally:
            neo.close()
        if q is None:
            return render(request, "error.html", active="neo",
                          message=f"neo question {question_id} not found",
                          status_code=404)
        return render(request, "neo_question.html", active="neo",
                      q=_decorate_question(q), opinions=opinions)

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

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request, a: str = "", b: str = "", scope: str = "",
                    node: str = "", q: str = ""):
        """What the fleet is configured to run, who changed it, and what changed.

        A form over `jarvis config` — see
        docs/superpowers/specs/2026-08-27-the-config-console.md §8 for the ledger and
        the provenance column; `jarvis wo show wo-516126ce` for the editor, the
        scope picker and the tree that replaced the one long column.

        Scope, node and search are URL state, not script: text a browser test cannot
        see is text nothing proves, and this page's whole shape is chosen around that
        (the same reason §8 forbids tabs here).
        """
        show = ops.config_show()
        history = ops.config_history(limit=50)
        scopes = config_scopes(show["resolved"])
        keys = [s["key"] for s in scopes]
        if scope not in keys:
            scope = keys[0]
        labels = sorted(p[len(scope) + 1:] for p in show["resolved"]
                        if config_scope_of(p) == scope)
        if node not in {n for label in labels for n in _config_nodes(label)}:
            node = ""
        # Default the diff to the change that landed: the head against the version
        # before it, so the page answers "what changed last" unasked.
        ids = [row["id"] for row in history]
        if not a and not b and len(ids) >= 2:
            a, b = ids[1], ids[0]
        diff = diff_error = None
        if a and b:
            try:
                diff = ops.config_diff(a, b)
            except ops.OpsError as e:
                diff_error = str(e)
        return render(request, "config.html", active="config", show=show,
                      scopes=scopes, scope=scope, node=node, q=q,
                      scope_title=next(s["title"] for s in scopes if s["key"] == scope),
                      # The tree root wants the SHORT name: "os — the fleet" wrapped
                      # to four lines in a 236px column and squashed the tree.
                      scope_short="os" if scope == "os" else scope.split(".", 1)[1],
                      tree=config_tree(labels, node),
                      rows=config_rows(show, config_setters(history), scope, node, q),
                      total=len(labels), here=_config_url(scope, node, q),
                      history=history, diff=diff, diff_error=diff_error, a=a, b=b)

    @app.get("/alarms", response_class=HTMLResponse)
    def alarms_page(request: Request):
        """Turns the OS raised WHILE they were still costing money.

        Split by whether the work order is still ASKING, because the two halves are
        different things and mixing them is how a cost alarm becomes wallpaper: the top
        is a queue the user is meant to empty, the bottom is the record of what the
        fleet has spent and is meant to be long.

        Acking is per WORK ORDER, not per alarm, and the page groups the live half that
        way — one order with three alarms is one decision. That is not a shortcut: the
        attention flag carries one sentence, so there was never more than one ask.

        The middle half comes from its own read rather than being filtered out of
        `rows`: the supervisor's reasoning and Neo's advice are not in the frozen
        `list_cost_alarms` dict, and widening that dict is what four sibling surfaces
        bind against. See `ops.alarm_review_queue`.
        """
        rows = ops.list_cost_alarms()
        live: dict[str, dict] = {}
        for a in (r for r in rows if r["live"]):
            group = live.setdefault(a["wo_id"], {**a, "alarms": []})
            group["alarms"].append(a)
        return render(request, "alarms.html", active="alarms",
                      live=list(live.values()),
                      review_queue=ops.alarm_review_queue(),
                      history=[r for r in rows if not r["live"]],
                      kinds=ALARM_KINDS)

    @app.get("/alarms/{project}/{alarm_id}", response_class=HTMLResponse)
    def alarm_page(request: Request, project: str, alarm_id: str):
        """One alarm — where the work order's timeline and a Neo escalation both link.

        `/alarms` is a list with no anchor, so pointing a "review it →" at it opens on
        whichever row sorts first; that shipped once on `/neo` and came back as two bug
        reports. §5 of docs/superpowers/specs/2026-08-31-the-supervisor.md.
        """
        try:
            alarm = ops.alarm_detail(alarm_id, project_name=project)
        except ops.OpsError as e:
            return render(request, "error.html", active="alarms",
                          message=str(e), status_code=404)
        return render(request, "alarm.html", active="alarms", a=alarm)

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
    def ack_wo(name: str, wo_id: str, back: str = Form("")):
        # `back` is where the user pressed the button. Acking from a LIST is a different
        # act from acking on the order's own page — the user is working through a queue
        # and wants the next row, not a detail page they then have to leave. Restricted
        # to a known path so a form cannot be used to bounce anyone off the dashboard.
        home = f"/wo/{name}/{wo_id}"
        landing = "/alarms" if back == "alarms" else home
        try:
            ops.ack_attention(wo_id, project_name=name)
        except ops.OpsError as e:
            # The one case that refuses: pending assumptions want a decision, not a
            # dismissal. Say so instead of silently doing nothing.
            return RedirectResponse(f"{landing}?error={e}", status_code=303)
        return RedirectResponse(landing, status_code=303)

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

    @app.post("/fo/{name}/{fo_id}/resume")
    def resume_fo(name: str, fo_id: str, fix: str = Form("")):
        try:
            ops.resume_feature_order(fo_id, fix=fix, project_name=name)
        except ops.OpsError as e:
            return RedirectResponse(f"/fo/{name}/{fo_id}?error={e}", status_code=303)
        return RedirectResponse(f"/fo/{name}/{fo_id}", status_code=303)

    def _same_site_back(next: str, fallback: str, error: str = "") -> str:
        """Where a decision returns the reader — the page they decided from, or the
        surface's own fallback. Same-site paths only, as in `decide_gate`: a form field
        is attacker-settable and an open redirect is not worth the convenience. The
        error flash rides in the query, which has to precede the tab fragment or the
        browser reads it as part of the fragment.

        Was `_neo_back`; the alarm review needs exactly this and a second copy of a
        redirect guard is how one of the two comes to be the lax one.
        """
        back = next if next.startswith("/") and not next.startswith("//") else fallback
        if not error:
            return back
        path, hash_, frag = back.partition("#")
        return f"{path}{'&' if '?' in path else '?'}error={error}{hash_}{frag}"

    @app.post("/neo/{question_id}/review")
    def neo_review(question_id: int, decision: str = Form(...),
                   feedback: str = Form(""), next: str = Form("")):
        try:
            ops.neo_review(question_id, approved=(decision == "approve"),
                           feedback=feedback)
        except ops.OpsError as e:
            return RedirectResponse(_same_site_back(next, "/neo#tab-review", str(e)),
                                    status_code=303)
        return RedirectResponse(_same_site_back(next, "/neo#tab-review"), status_code=303)

    @app.post("/neo/{question_id}/answer")
    def neo_answer(question_id: int, text: str = Form(...), next: str = Form("")):
        try:
            ops.neo_answer_escalated(question_id, text)
        except ops.OpsError as e:
            return RedirectResponse(_same_site_back(next, "/neo#tab-escalated", str(e)),
                                    status_code=303)
        return RedirectResponse(_same_site_back(next, "/neo#tab-escalated"), status_code=303)

    @app.post("/alarms/{project}/{alarm_id}/review")
    def alarm_review(project: str, alarm_id: str, decision: str = Form(...),
                     feedback: str = Form(""), next: str = Form("")):
        """The user's verdict on the supervisor's. Same shape as `neo_review` above,
        and the same guard on `next`."""
        fallback = f"/alarms/{project}/{alarm_id}"
        try:
            ops.review_alarm(alarm_id, approved=(decision == "approve"),
                             feedback=feedback, project_name=project)
        except ops.OpsError as e:
            return RedirectResponse(_same_site_back(next, fallback, str(e)),
                                    status_code=303)
        return RedirectResponse(_same_site_back(next, fallback), status_code=303)

    @app.post("/neo/learn")
    def neo_learn(content: str = Form(...), project: str = Form("")):
        from ..neo_store import NeoStore
        neo = NeoStore()
        try:
            neo.add_learning(content, project=project, source="manual")
        finally:
            neo.close()
        return RedirectResponse("/neo#tab-learnings", status_code=303)

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

    @app.post("/config/set")
    def config_set(path: str = Form(...), value: str = Form(""),
                   reason: str = Form(""), back: str = Form("/config")):
        """The page's only write, and it is `jarvis config set` under another name.

        Every setting is editable now, so the type check that boolean-only used
        to give for free is explicit and lives here — Neo, q193.
        """
        back = back if back.startswith("/config") else "/config"
        try:
            current = ops.config_get(path)["value"]
            new = config_value_of(current, value)
            if not config_type_ok(current, new):
                raise ops.OpsError(
                    f"{path} takes {config_type_name(current)} — {value!r} is not. "
                    f"`jarvis config set {path} <value>` says the same thing.")
            ops.set_config(path, new, reason=reason)
        # ValueError/TypeError: `parse_catalog` coerces with bare `int()`/`str()` and
        # raises neither `CatalogError` nor anything `ops` converts (kn-650b6f24).
        except (ops.OpsError, ValueError, TypeError) as e:
            sep = "&" if "?" in back else "?"
            # `quote`, not the default `quote_plus`: the flash is read by a human
            # off the address bar as often as by the page.
            return RedirectResponse(
                f"{back}{sep}{urlencode({'error': str(e)}, quote_via=quote)}",
                status_code=303)
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
