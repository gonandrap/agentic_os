"""Jarvis web dashboard — server-rendered"""

from __future__ import annotations

import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import bill, fleet, fleetcost, github, invariants, ops, specs, uilog, wiring
from ..bill import OWN_LABEL
from ..central_store import CentralStore
from ..daemon import daemon_running
from ..inspection import ALARM_KINDS, PARTS
from ..paths import PRODUCTION, deployment_env
from ..project_store import (
    ACTIVE_STATUSES,
    FO_OPEN_STATUSES,
    FO_STATUSES,
    FO_TERMINAL_STATUSES,
    NO_TURN,
    OPEN_STATUSES,
    TERMINAL_STATUSES,
    WO_STATUSES,
    ProjectStore,
    feature_status_label,
    is_feature_order_id,
    order_path,
    validation_standing,
)
from ..timeline import build_conversation, build_timeline, count_debug
from . import markdown

TEMPLATES = Path(__file__).parent / "templates"


def _fleet_if_held(wos: list[dict]) -> "fleet.Fleet | None":
    """The account's state for these orders, or None — `ops.fleet_if_held` (#714)"""
    return ops.fleet_if_held(wos)


STATUS_META = {
    "pending":       {"word": "pending",     "icon": "◌", "tone": "muted"},
    "dispatching":   {"word": "dispatching", "icon": "◍", "tone": "active"},
    "running":       {"word": "running",     "icon": "●", "tone": "active"},
    # Toned `muted`, not `warn`, and absent from FEATURED_STATUSES below: a manager between its feature's... (#264)
    "idle":          {"word": "idle",           "icon": "◎", "tone": "muted"},
    "waiting_input": {"word": "waiting on you", "icon": "◉", "tone": "warn"},
    # Toned `active`, not `warn`: a round in flight is the OS working
    "validating":    {"word": "under review",   "icon": "◑", "tone": "active"},
    "needs_review":  {"word": "needs review",   "icon": "◭", "tone": "warn"},
    "waiting_pr_merge": {"word": "waiting for PR merge", "icon": "⑃", "tone": "ok"},
    # Toned `warn` rather than `bad`: nothing went wrong with the work
    "budget_exhausted": {"word": "budget spent", "icon": "◒", "tone": "warn"},
    "completed":     {"word": "completed",   "icon": "✓", "tone": "ok"},
    "failed":        {"word": "failed",      "icon": "✗", "tone": "bad"},
    "cancelled":     {"word": "cancelled",   "icon": "–", "tone": "muted"},
}
# A feature order's own lifecycle
FO_STATUS_META = {
    "pending":     {"word": "not planned yet", "icon": "◌", "tone": "muted"},
    "planning":    {"word": "planning",     "icon": "◍", "tone": "active"},
    "plan_review": {"word": "plan in review", "icon": "◭", "tone": "warn"},
    "executing":   {"word": "executing",    "icon": "●", "tone": "active"},
    # A feature order validates as a whole once every child is done
    "validating":  {"word": "under review",  "icon": "◑", "tone": "active"},
    "budget_exhausted": {"word": "budget spent", "icon": "◒", "tone": "warn"},
    "completed":   {"word": "completed",    "icon": "✓", "tone": "ok"},
    "failed":      {"word": "failed",       "icon": "✗", "tone": "bad"},
    "cancelled":   {"word": "cancelled",    "icon": "–", "tone": "muted"},
}
ORIGIN_META = {
    "jarvis": {"word": "jarvis", "framework": True},
    "ui":     {"word": "ui",     "framework": True},
    "manual": {"word": "manual", "framework": False},
    # `injected` is not a warning: the user handed this session over on purpose
    "injected": {"word": "injected", "framework": True},
    "adhoc":  {"word": "ad-hoc", "framework": False},
    # Framework-spawned like `jarvis`
    "neo":    {"word": "neo",    "framework": True},
}
LEVEL_TONE = {"info": "muted", "warning": "warn", "critical": "bad"}
# Privileged-action gates
GATE_META = {
    # Recorded, unargued, in front of nobody
    "awaiting_case": {"word": "awaiting case", "icon": "✎", "tone": "warn"},
    "pending":   {"word": "pending",   "icon": "◌", "tone": "warn"},
    "approved":  {"word": "approved",  "icon": "✓", "tone": "ok"},
    "denied":    {"word": "denied",    "icon": "✗", "tone": "bad"},
    "dismissed": {"word": "not a gate", "icon": "⊘", "tone": "muted"},
    "expired":   {"word": "expired",   "icon": "–", "tone": "muted"},
    # Not a status but a DISPLAY state: `expired` with `closed_as='abandoned'`
    "abandoned": {"word": "abandoned, never reviewed", "icon": "⊗", "tone": "muted"},
    # Same treatment, opposite outcome: `expired` with `closed_as='spent'` was approved and then USED
    "spent": {"word": "spent", "icon": "✓", "tone": "ok"},
}


def gate_display(g: dict) -> str:
    """The key this gate renders under, which is not always its status: `expired` splits by `closed_as`"""
    if g["status"] == "expired" and g["closed_as"] in ("spent", "abandoned"):
        return g["closed_as"]
    return g["status"]


# How often the dashboard re-reads OS state
REFRESH_SECONDS = 15

# The open statuses worth a row of their own
FEATURED_STATUSES = ("budget_exhausted", "needs_review", "waiting_input", "running",
                     "waiting_pr_merge")


def group_open(wos: list[dict], revealed: str = "") -> tuple[list[dict], list[dict]]:
    """Split open work orders into the featured rows and the rest"""
    featured = [wo for status in FEATURED_STATUSES for wo in wos
                if wo["status"] == status]
    rest = [wo for wo in wos if wo["status"] not in FEATURED_STATUSES]
    shown = [wo for wo in rest if revealed in ("all", wo["status"])]
    return featured, shown


def _counts_by_status(statuses) -> list[tuple[str, int]]:
    """(status, n) pairs in WO_STATUSES order — a stable line, not dict order"""
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


def fmt_left(ts: float | None) -> str:
    """How long until a deadline — the mirror of `fmt_age`"""
    if not ts:
        return "–"
    d = ts - time.time()
    if d <= 0:
        return "any moment now"
    return f"{int(d / 60)}m" if d >= 60 else f"{int(d)}s"


def fmt_ts(ts: float | None) -> str:
    """An absolute local date-time"""
    if not ts:
        return "–"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _offset_param(raw: str) -> int:
    """`?offset=` as an int, refused in the OS's own vocabulary rather than by FastAPI (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        raise ops.OpsError(f"offset must be a whole number of windows — {raw!r} is not "
                           f"a number") from None


def _window_inputs(window: dict) -> dict[str, str]:
    """The active window as `datetime-local` values, so the custom form pre-fills (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
    fmt = "%Y-%m-%dT%H:%M"
    tzinfo = ZoneInfo(window["zone"])
    out = {key: datetime.fromtimestamp(window[key], tzinfo).strftime(fmt)
           for key in ("since", "until")}
    # The zone form carries a custom range with its OFFSET
    out.update({f"{key}_fixed": datetime.fromtimestamp(window[key], tzinfo).isoformat()
                for key in ("since", "until")})
    return out


def _in_zone(raw: str, zone: str) -> float | str:
    """A naive datetime-local string as the epoch it means IN `zone` (§11)"""
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(zone))
    return when.timestamp()


#: Paths the access log ignores while they succeed
QUIET_PATHS = ("/api/status", "/healthz")

#: The same rule for polls whose path carries an id (docs/superpowers/specs/2026-09-24-order-observability.md)
QUIET_SUFFIXES = ("/live",)


def _quiet(path: str) -> bool:
    """Is a SUCCESSFUL request on this path beneath the access log's notice?"""
    return path in QUIET_PATHS or path.endswith(QUIET_SUFFIXES)


# -- the dashboard's own liveness (spec §1, §2, §3 of


def _routed(request) -> bool:
    """Does this request match a route?"""
    from starlette.routing import Match

    try:
        for route in request.app.routes:
            if route.matches(request.scope)[0] == Match.FULL:
                return True
    except Exception:  # noqa: BLE001 — never raise on the request path
        return False
    return False


def _pool_ping() -> bool:
    """The probe's whole job in the thread: return"""
    return True


class PoolProbe:
    """Is the one anyio threadpool every page handler runs in still serving?"""

    def __init__(self) -> None:
        from .. import bugreport
        from ..catalog import UiHealthConfig

        self.started_at = time.time()
        # The settings the probe last resolved
        self.cfg = UiHealthConfig()
        self.last_ok: float | None = None
        self.consecutive_failures = 0
        self.wedged_since: float | None = None
        self.stack_dump: str | None = None
        # Routed requests in flight, keyed by a counter: a monotonic start time and a label
        self._inflight: dict[int, tuple[float, str]] = {}
        self._inflight_seq = 0
        self.probe_failure_reason = ""
        # Resolved once, here: `jarvis_version` shells out to git on its first call
        try:
            self.version = bugreport.jarvis_version()
        except Exception:  # noqa: BLE001
            self.version = ""

    @staticmethod
    def limiter_figures() -> dict[str, int]:
        """The anyio capacity limiter every sync handler queues on"""
        import anyio.to_thread

        try:
            limiter = anyio.to_thread.current_default_thread_limiter()
            stats = limiter.statistics()
            return {"borrowed": int(stats.borrowed_tokens),
                    "available": int(limiter.available_tokens),
                    "total": int(stats.total_tokens),
                    "waiting": int(stats.tasks_waiting)}
        except Exception:  # noqa: BLE001 — no loop, or an anyio that moved
            return {"borrowed": 0, "available": 0, "total": 0, "waiting": 0}

    def begin(self, method: str, path: str) -> int:
        """Register one routed request"""
        self._inflight_seq += 1
        key = self._inflight_seq
        self._inflight[key] = (time.monotonic(), f"{method} {path}")
        return key

    def end(self, key: int) -> None:
        self._inflight.pop(key, None)

    def oldest_inflight(self) -> tuple[float, str] | None:
        """Age in seconds and label of the longest-running routed request, or None"""
        entries = list(self._inflight.values())
        if not entries:
            return None
        started, label = min(entries, key=lambda e: e[0])
        return (time.monotonic() - started, label)

    @property
    def pool_healthy(self) -> bool:
        """The last probe ROUND passed — the pool round-trip AND the in-flight check"""
        return self.consecutive_failures == 0

    def wedged(self, trip_threshold: int) -> bool:
        """Failed often enough to be a wedge rather than one slow moment"""
        return self.consecutive_failures >= trip_threshold

    def payload(self, cfg) -> dict:
        now = time.time()
        oldest = self.oldest_inflight()
        # No `version` and no `stack_dump`: `/healthz` is unauthenticated (review round 1)
        return {
            "pool_healthy": self.pool_healthy,
            "wedged": self.wedged(cfg.trip_threshold),
            "enabled": cfg.enabled,
            "limiter": self.limiter_figures(),
            "last_ok_age_seconds": (None if self.last_ok is None
                                    else round(now - self.last_ok, 3)),
            "consecutive_failures": self.consecutive_failures,
            "uptime_seconds": round(now - self.started_at, 1),
            "wedged_since": self.wedged_since,
            "inflight": len(self._inflight),
            "oldest_inflight_seconds": (None if oldest is None else round(oldest[0], 3)),
            "probe_failure_reason": self.probe_failure_reason,
        }

    async def step(self, cfg) -> bool:
        """One round, two checks: the pool round-trip"""
        import anyio

        try:
            with anyio.fail_after(cfg.probe_timeout_seconds):
                await anyio.to_thread.run_sync(_pool_ping)
        except TimeoutError:
            return self._fail("pool_timeout", cfg)
        oldest = self.oldest_inflight()
        if oldest is not None and oldest[0] > cfg.probe_timeout_seconds:
            return self._fail("inflight_stall", cfg)
        self.last_ok = time.time()
        self.consecutive_failures = 0
        self.probe_failure_reason = ""
        return True

    def _fail(self, reason: str, cfg) -> bool:
        self.probe_failure_reason = reason
        self.consecutive_failures += 1
        if self.wedged(cfg.trip_threshold):
            self._trip()
        return False

    def _trip(self) -> None:
        """Dump this process's stacks — the only way to get the frames at all, since py-spy could not attach"""
        stamp = uilog.record_wedge(limiter=self.limiter_figures(),
                                   uptime_seconds=time.time() - self.started_at,
                                   version=self.version)
        self.wedged_since = stamp.get("since")
        self.stack_dump = stamp.get("dump") or None

    async def run(self) -> None:
        """The lifespan task: probe, sleep, repeat"""
        import anyio

        while True:
            # Re-resolved every round, so `jarvis config set os.ui_health.…` is in force without restarting the...
            self.cfg = ops.ui_health_config()
            if self.cfg.enabled:
                await self.step(self.cfg)
            await anyio.sleep(max(1, self.cfg.probe_interval_seconds))


def _cost_payloads(picked: dict, project: str, method: str = "TASK") -> dict:
    """The three /cost payloads for one resolved window (docs/superpowers/specs/2026-10-22-cost-tab-precompute.md)"""
    report = ops.cost_report(project=project or None, window=picked)
    # The distribution is a SECTION of this page
    try:
        fleet = ops.fleet_cost(project=project or None, resolved=picked)["fleet"]
    except Exception:                                   # noqa: BLE001
        fleet = None
    # The meter is a THIRD payload from the same resolved window
    try:
        meter = ops.cost_meter(resolved=picked, project=project or None)["meter"]
    except Exception as e:                              # noqa: BLE001
        uilog.record_error(method, "cost/meter", e)
        meter = None
    return {"report": report, "fleet": fleet, "meter": meter}


REFRESH_SECONDS = 60


class CostCache:
    """Default /cost windows, precomputed (docs/superpowers/specs/2026-10-22-cost-tab-precompute.md)"""

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}

    def compute(self, name: str) -> dict:
        picked = ops.cost_window(window=name)
        return {"window": picked, **_cost_payloads(picked, ""), "computed_at": time.time()}

    def lookup(self, picked: dict) -> dict | None:
        entry = self.entries.get(picked.get("window") or "")
        if entry is None or ((entry["window"]["since"], entry["window"]["until"])
                             != (picked["since"], picked["until"])):
            return None
        return entry

    async def refresh(self) -> None:
        """One round over every named window; a failure keeps the last good entry"""
        import anyio

        for name in fleetcost.WINDOW_NAMES:
            try:
                self.entries[name] = await anyio.to_thread.run_sync(self.compute, name)
            except Exception as e:                      # noqa: BLE001
                uilog.record_error("TASK", f"cost/cache/{name}", e)

    async def run(self) -> None:
        import anyio

        while True:
            await self.refresh()
            await anyio.sleep(REFRESH_SECONDS)


def _stale_link_hint(name: str, wo_id: str) -> str:
    """Why a work-order deep link went bad, as the visitor should read it"""
    known = sorted(ops.registered_project_paths())
    if name not in known:
        return (f"This link points at project {name!r}, which the OS does not "
                f"know about — it was never registered, or it has since been "
                f"removed from the catalog. Registered projects: "
                f"{', '.join(known) or 'none'}.")
    return (f"Project {name!r} is registered, but it has no work order "
            f"{wo_id!r} — the link is from before it existed, or the work "
            f"order was deleted (`jarvis wo delete` erases the record).")


def _block(label: str, read, *args, **kwargs) -> dict:
    """One of the debugging page's four readings, fetched so the other three survive it"""
    try:
        return {"payload": read(*args, **kwargs), "error": ""}
    except ops.OpsError as e:
        return {"payload": None, "error": f"{label} could not be read — {e}"}
    except Exception as e:  # noqa: BLE001 — three blocks must outlive the fourth
        uilog.record_error("GET", f"debug/{label}", e)
        return {"payload": None,
                "error": (f"{label} could not be read — {type(e).__name__}: {e}. "
                          f"The traceback is in {uilog.ui_log_path()} and the daemon "
                          "will raise it in `jarvis inbox` on its next tick.")}


def _fix_filed_notice(filed: str) -> str:
    """What the fix POST just did, rebuilt from its `?filed=` — `ops` owns every word"""
    if filed.isdigit():
        return ops.fix_filed_notice(int(filed))
    if filed.startswith(ops.FIX_PENDING_PREFIX):
        rest = filed[len(ops.FIX_PENDING_PREFIX):]
        return ops.fix_pending_notice(int(rest)) if rest.isdigit() else ""
    return ops.fix_flag_notice(filed) or ""


def _rel_url(request: Request) -> str:
    """Path plus query — `?error=…` is often the whole story of a failed click"""
    q = request.url.query
    return request.url.path + (f"?{q}" if q else "")


def instance_badge() -> dict[str, str | bool]:
    """Which Jarvis this dashboard is driving, for the header (docs/DEPLOYMENT.md)"""
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
    """`0.5.0` → `v0.5.0`; `dev-a1b2c3d` → `a1b2c3d`"""
    if version.startswith("dev-"):
        return version[len("dev-"):]
    return f"v{version}" if version[:1].isdigit() else version


def _false_positive_rate(rows: list) -> str | None:
    """"3 of 11 (27%)" — how often the gate fired on a command that ships nothing"""
    ruled = [g for g in rows if g["status"] in ("approved", "denied", "dismissed")]
    if not ruled:
        return None
    n = sum(1 for g in ruled if g["status"] == "dismissed")
    return f"{n} of {len(ruled)} ({round(100 * n / len(ruled))}%)"


def _abandoned(rows: list) -> int:
    """Held requests whose worker never came back — no case, no contest"""
    return sum(1 for g in rows if g["closed_as"] == "abandoned")


def gate_badge() -> int | None:
    """How many gates are waiting on the user, for the nav"""
    try:
        return len([g for g in ops.list_gates(pending_only=True)
                    if g["escalated"]]) or None
    except Exception:  # noqa: BLE001 — see docstring
        return None


def alarm_badge() -> int | None:
    """How many SUBJECTS are asking for the user because of an alarm (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
    try:
        return len({a["subject_id"] for a in ops.list_cost_alarms()
                    if a["live"]}) or None
    except Exception:  # noqa: BLE001 — see docstring
        return None


def _order_href(order_id: str) -> str | None:
    """Where an allow-listed order's page is, or None when it no longer resolves"""
    try:
        # Issue #997: `fo-` only, so an allow-listed `io-`/`inv-` id was looked up as a work order
        if is_feature_order_id(order_id):
            pname, _, _ = ops.find_feature_order(order_id)
        else:
            pname, _, _ = ops.find_work_order(order_id)
        return order_path(pname, order_id)
    except ops.OpsError:
        return None


def brake_state() -> dict | None:
    """The user's brake (`jarvis pause`) and the post-reopen ramp, for the banner every page carries (#843)"""
    try:
        central = CentralStore()
        try:
            fl = ops.fleet_summary(central)
        finally:
            central.close()
    except Exception:  # noqa: BLE001 — see docstring
        return None
    if not fl["paused"] and not fl["ramp"]:
        return None
    fl["since_text"] = invariants.clock(fl["since"]) if fl.get("since") else ""
    fl["allowed"] = [{"id": i, "href": _order_href(i)} for i in fl["allow"]]
    if fl["ramp"]:
        fl["ramp"] = {**fl["ramp"], "since_text": invariants.clock(fl["ramp"]["since"]),
                      "until_text": invariants.clock(fl["ramp"]["until"])}
    return fl


def _decorate_question(q: dict) -> dict:
    """Add the two display-only fields the `/neo` question blocks render"""
    from .. import digest, issues, neo
    q["digest_view"] = digest.decode(q.get("digest"))
    q["full_context"] = neo.build_question_prompt(q)
    # `triage` only, and empty everywhere else: the one kind whose subject is a tracker issue rather than...
    q["issue_url"] = (issues.triage_payload(q) or {}).get("issue_url") or ""
    return q


def _digest_credit() -> str:
    """Attribution for the vendored output style, shown once on the page"""
    from .. import digest
    return digest.skill_attribution()


def fmt_tok(n: int | None) -> str:
    """Token counts, at the magnitude a reader actually compares — 1.3M, 47k, 812"""
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_dur(seconds: float | None) -> str:
    """Turn durations at a readable magnitude — 1.2h, 4m, 15s"""
    if seconds is None:
        return "—"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def wo_bill(wo_id: str, project: str) -> dict | None:
    """One work order's itemised bill, or None if there is nothing measurable to show"""
    try:
        bill = ops.bill(wo_id, project=project)
    except Exception:  # noqa: BLE001 — see docstring
        return None
    return bill if bill["total"]["tokens"]["total"] else None


def turn_lines_by_message(bill: dict | None) -> dict[int, dict]:
    """message id → the bill line for the turn that message set going"""
    if not bill:
        return {}
    out: dict[int, dict] = {}
    for line in bill.get("turns") or []:
        msg_id = (line.get("turn") or {}).get("msg_id")
        if msg_id:
            out[msg_id] = line
    return out


def config_setters(history: list[dict]) -> dict[str, str]:
    """path → the id of the most recent version that changed it"""
    setters: dict[str, str] = {}
    for row in reversed(history):  # oldest first, so the last writer wins
        for change in row["changes"]:
            setters[change["path"]] = row["id"]
    return setters


def config_scope_of(path: str) -> str:
    """The catalog block a resolved path lives in: `os`, or one `projects.<name>`"""
    parts = path.split(".")
    return "os" if parts[0] == "os" else ".".join(parts[:2])


def config_scopes(resolved: dict) -> list[dict]:
    """Every block the page can be pointed at, for the picker at the top"""
    names = sorted({p.split(".")[1] for p in resolved if p.startswith("projects.")})
    return ([{"key": "os", "title": "os — the fleet"}]
            + [{"key": f"projects.{n}", "title": n} for n in names])


def config_tree(labels: list[str], node: str = "") -> list[dict]:
    """One scope's dotted labels as the nested nodes a tree renders"""
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
    """Which input a setting is edited with — the whole of the page's type awareness"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, dict)):
        return "json"
    return "text"


def config_input_text(value: object) -> str:
    """The submitted-text form of a value, so an edit starts from what is already set"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value)


def config_value_of(current: object, text: str) -> object:
    """What the page will write for `text`, given the value already there"""
    if isinstance(current, str):
        return text
    if current is None and not text.strip():
        return None
    return ops.parse_config_value(text)


def config_type_ok(current: object, new: object) -> bool:
    """May `new` be written where `current` is?"""
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
    """Every interior node a dotted label passes through: `a.b.c` → `a`, `a.b`"""
    parts = label.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def _config_url(scope: str, node: str = "", q: str = "") -> str:
    """The page as it is being read right now"""
    query = {k: v for k, v in (("scope", scope), ("node", node), ("q", q)) if v}
    return "/config" + (f"?{urlencode(query)}" if query else "")


def config_rows(show: dict, setters: dict[str, str], scope: str,
                node: str = "", query: str = "") -> list[dict]:
    """The settings one scope shows, narrowed by the tree node or by the search box"""
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
                     # Which node the match came out of, so a search result set can be grouped by it (Neo, q194)
                     "node": label.rpartition(".")[0],
                     "widget": config_widget(value), "text": config_input_text(value),
                     "written": path in written, "safety": ops.safety_key(path),
                     "version": setters.get(path) if path in written else None})
    return rows


#: Which of the user's own MCP servers, skills and plugins reach this scope's workers
WIRING_GROUPS = (
    ("mcp", "MCP servers",
     "Every server your Claude configuration has. Deselecting one keeps it out of the "
     "sessions this scope dispatches — your own Claude configuration is not touched."),
    ("skill", "Skills",
     "Your own skills, plus the block Claude Code ships. A plugin's skills follow "
     "their plugin, in the next group."),
    ("plugin", "Plugins",
     "An enabled plugin reaches a worker whole: its MCP servers, its skills and its "
     "agents. Deselecting it takes all three."),
)

#: The one row the page has evidence about, so it is the one row that carries any (docs/superpowers/findings/2026-09-14-which-mcp-server-moves-the-prefix.md)
WIRING_EVIDENCE = {
    wiring.LEVER_CONNECTORS:
        "Measured: this block precedes a conversation-sized cache write 83.7% of the "
        "time — the worst of any cohort measured. Nothing cleared any other server.",
}


def wiring_project(scope: str) -> str | None:
    """The project a /config scope names, or None for the fleet base"""
    return None if scope == "os" else scope.split(".", 1)[1]


def wiring_groups(scope: str, *, refresh: bool = False) -> dict[str, object]:
    """The wiring section for one scope, or an `error` the page renders instead"""
    try:
        data = ops.wiring_show(project=wiring_project(scope), refresh=refresh,
                               block=False)
    except (ops.OpsError, OSError, ValueError) as e:
        return {"error": str(e), "groups": [], "errors": [], "reading": False}
    groups = []
    # ONCE PER LEVER, not once per row: nine claude.ai connectors share one switch
    said: set[str] = set()
    for kind, title, blurb in WIRING_GROUPS:
        rows = []
        for i in (x for x in data["items"] if x.kind == kind):
            evidence = "" if i.lever in said else WIRING_EVIDENCE.get(i.lever, "")
            if evidence:
                said.add(i.lever)
            rows.append({"item": i, "evidence": evidence})
        if rows:
            groups.append({"kind": kind, "title": title, "blurb": blurb, "rows": rows})
    return {"error": "", "groups": groups, "errors": data["errors"],
            "reading": data["reading"], "ts": data["ts"],
            "unwired": data["unwired"], "project": data["project"]}


def create_app() -> FastAPI:
    probe = PoolProbe()
    cost_cache = CostCache()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Owns the liveness probe task (spec §2)"""
        import anyio

        async with anyio.create_task_group() as tg:
            tg.start_soon(probe.run)
            tg.start_soon(cost_cache.run)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.pool_probe = probe
    app.state.cost_cache = cost_cache

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Does the dashboard still serve? (spec §1)"""
        body = probe.payload(probe.cfg)
        return JSONResponse(body, status_code=200 if body["pool_healthy"] else 503)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        # Every page reflects live OS state (inbox acks, work order status)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    # Inside the factory, like every other same-tier import in this file: `cli` and `ui` are one tier and...
    from ..cli import (ATTENTION_LIST_NOTE, INBOX_LIST_NOTE, PART_LABELS, PART_SHORT,
                       _held_phrase)

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.globals.update(
        status_meta=STATUS_META, origin_meta=ORIGIN_META, gate_meta=GATE_META,
        gate_display=gate_display,
        # What each list is and which command clears it — the same words `jarvis status` prints (io-d2a5dd69)
        attention_list_note=ATTENTION_LIST_NOTE, inbox_list_note=INBOX_LIST_NOTE,
        # Every link to an order's page goes through this — see `order_path` (#997)
        order_path=order_path,
        # A round's word, tone and icon — the same tuple `ops.round_line` and `automerge.decide` render from (#581)
        validation_standing=validation_standing,
        fo_status_meta=FO_STATUS_META, level_tone=LEVEL_TONE, fmt_age=fmt_age,
        fmt_left=fmt_left,
        # Shared with `jarvis alarms` rather than spelled inline
        turn_label=ops.turn_label, no_turn=NO_TURN,
        # "What is not on this bill", worded ONCE in `bill.ABSENT_NOTES` and read by this page and `jarvis...
        absent_notes=bill.absent_notes,
        # Same reason one sentence along: the subagent side's STRUCTURAL zero is worded once
        subagent_rewrite_zero=bill.SUBAGENT_REWRITE_ZERO,
        # And one sentence further along: what a placeholder output figure means
        placeholder_note=bill.PLACEHOLDER_NOTE,
        placeholder_zero=bill.PLACEHOLDER_ZERO,
        # The tool table's ROW ORDER, shared with `jarvis cost --fleet` for the reason the partial already...
        tool_table=fleetcost.tool_table,
        # Same reason, for the assumption badge: `jarvis wo show` and this page must not be able to disagree...
        assumption_decider=ops.assumption_decider,
        # And the OS's RULING on one, same reason one authority along: the page and `jarvis wo show` must not... (#712)
        assumption_ruling_line=ops.assumption_ruling_line,
        # The early pass's three facts, same reason again (§8 of the early-review spec): Neo's provisional...
        provisional_line=ops.provisional_line,
        objection_line=ops.objection_line,
        objection_response_line=ops.objection_response_line,
        # And what has already LANDED under a plan the user has not ratified (docs/superpowers/specs/2026-09-24-a-planner-assumption-holds-its-feature.md)
        overtaken_line=ops.overtaken_line,
        # "a worker turn may be in flight right now"
        active_statuses=ACTIVE_STATUSES,
        # The statuses the user's brake can hold — where a "let this order through" button means something
        fleet_held_statuses=invariants.FLEET_HELD_STATUSES,
        instance=instance_badge(),
        fmt_tok=fmt_tok, fmt_dur=fmt_dur, fmt_ts=fmt_ts,
        # The bill's two explanations, taken from the module that computes the numbers rather than written...
        rate_note=bill.rate_note, tokens_mean=bill.TOKENS_MEAN,
        write_rate_of=bill.write_rate_of,
        # The debugging page's legends, taken from the modules that MEASURE rather than spelled into the...
        parts=PARTS, part_labels=PART_LABELS, part_short=PART_SHORT,
        prefix_authority=ops.PREFIX_AUTHORITY,
        # The residual's own sentence, shared with `jarvis wo why` for the same reason: a residual described...
        unexplained_note=ops.UNEXPLAINED_NOTE,
        # The status-span view's two sentences, from `ops` and not spelled here
        approximate_note=ops.FO_APPROXIMATE_NOTE,
        no_trigger_phrase=ops.NO_TRIGGER_PHRASE,
        # "a fleet usage limit and 2 others"
        held_phrase=_held_phrase,
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
        ctx["brake"] = brake_state()
        return templates.TemplateResponse(request, template, ctx,
                                          status_code=status_code)

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> HTMLResponse:
        """Last line of defence: a bare "Internal Server Error" tells the user nothing"""
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
        """One line per request in `$JARVIS_HOME/logs/ui-access.log`"""
        t0 = time.perf_counter()
        # Runs on the event loop, before `call_next` queues a sync handler onto the pool: the one place a...
        key = (probe.begin(request.method, request.url.path)
               if request.url.path != "/healthz" and _routed(request) else None)
        try:
            try:
                response = await call_next(request)
            except Exception:
                # The handler above renders the page
                uilog.record_access(request.method, _rel_url(request), 500,
                                    (time.perf_counter() - t0) * 1000)
                raise
            if response.status_code >= 400 or not _quiet(request.url.path):
                uilog.record_access(request.method, _rel_url(request),
                                    response.status_code,
                                    (time.perf_counter() - t0) * 1000)
            return response
        finally:
            # A 500 and a cancellation both clean up (spec §1)
            if key is not None:
                probe.end(key)

    # -- pages

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, show: str = ""):
        """The pulse"""
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
                      revealed=revealed, model_ids=ops.suggested_model_ids())

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", project: str = "", kind: str = ""):
        """Find an artifact again — across the fleet, or inside one project"""
        from .. import search as search_mod

        projects = sorted(ops.registered_project_paths())
        if project and project not in projects:
            project = ""
        kind = kind if kind in search_mod.KINDS else ""
        hits = search_mod.search(q, project=project or None,
                                 kinds=(kind,) if kind else None, limit=60)
        return render(request, "search.html", active="search", q=q, project=project,
                      kind=kind, hits=hits, counts=search_mod.counts(hits),
                      projects=projects, kinds=search_mod.KINDS)

    @app.get("/project/{name}", response_class=HTMLResponse)
    def project(request: Request, name: str, hidden: str = "", show: str = "",
                fo: str = ""):
        """A project's work orders — the ones that want the user, by default"""
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
            # Open feature orders get their own short list above the work orders
            features = [{**row, "progress": ops.feature_progress(store, row)}
                        for row in store.list_feature_orders(statuses=fo_statuses)]
            # Beside the features and never merged into them: `list_feature_orders` defaults to kind='feature'
            improvements = [
                {**row,
                 "status_label": feature_status_label("improvement", row["status"])}
                for row in store.list_feature_orders(statuses=FO_OPEN_STATUSES,
                                                     kind="improvement")]
            # A third kind beside them, on the same rule and for the same reason (§2.9 of... (docs/superpowers/specs/2026-09-27-investigation-orders.md)
            investigations = [
                {**row,
                 "status_label": feature_status_label("investigation", row["status"])}
                for row in store.list_feature_orders(statuses=FO_OPEN_STATUSES,
                                                     kind="investigation")]
            fo_counts = store.feature_status_counts()
            wos = store.list_work_orders(statuses=statuses, include_hidden=show_hidden)
            # Inside the store's lifetime: the label reads the dependencies' own rows
            blocked = {wo["id"]: ops.blocked_by(store, wo) for wo in wos}
            blocked = {k: v for k, v in blocked.items() if v}
            # Same lifetime, same reason: the note reads this work order's last turn
            pauses = {wo["id"]: invariants.pause_note(store, wo) for wo in wos}
            # ...and the account-wide one, for the orders that own no refusal of their own (#714)
            account = _fleet_if_held(wos)
            for wo in wos:
                if not pauses.get(wo["id"]):
                    pauses[wo["id"]] = invariants.fleet_hold_note(wo, account)
            pauses = {k: v for k, v in pauses.items() if v}
            # Inside the store's lifetime like every other read on this page
            issue_board = ops.issue_board(store)
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
        # Counted in SQL rather than from `wos`
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
                      improvements=improvements,
                      investigations=investigations,
                      issue_board=issue_board,
                      fo_revealed=fo_revealed)

    @app.get("/fo/{name}/{fo_id}", response_class=HTMLResponse)
    def feature_order(request: Request, name: str, fo_id: str, error: str = ""):
        """A feature order's page"""
        # An id whose prefix names another kind gets its own page (#997)
        target = order_path(name, fo_id)
        if target != f"/fo/{name}/{fo_id}":
            return RedirectResponse(target, status_code=303)
        try:
            detail = ops.show_feature_order(fo_id, name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e))
        # The deliberation, for the fold
        store = ProjectStore(ops.registered_project_paths()[detail["project"]])
        try:
            validation = ops.validation_detail(store, fo_id=fo_id)
            # Taken in the same block, for the same reason: one connection per page (spec §7)
            states = ops.state_durations(store, fo_id=fo_id).as_dict()
        finally:
            store.close()
        # EACH PLAN ROW'S FRAGMENT, RESOLVED SERVER-SIDE — §5(c)
        doc = str((detail.get("plan") or {}).get("design_doc_content") or "")
        child_anchors = {
            c["key"]: markdown.anchor_for(doc, str(c.get("spec_section") or "")) or ""
            for c in (detail.get("plan") or {}).get("children", [])
            if c.get("spec_section")
        }
        return render(request, "feature_order.html", fo=detail, project=detail["project"],
                      states=states, child_anchors=child_anchors,
                      cap=ops.feature_order_budget(fo_id, detail["project"]),
                      # Already on `detail` for `jarvis fo show`
                      issues=detail["issues"],
                      validation=validation, error=error)

    @app.get("/spec/{name}/{fo_id}", response_class=HTMLResponse)
    def spec_page(request: Request, name: str, fo_id: str):
        """THE FEATURE'S SPEC, RENDERED — §1 of... (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)"""
        paths = ops.registered_project_paths()
        if name not in paths:
            return render(request, "error.html",
                          message=f"no project named {name!r} is registered")
        store = ProjectStore(paths[name])
        try:
            plan = specs.plan_of(store, fo_id)
            try:
                fo = store.get_feature_order(fo_id)
            except KeyError:
                fo = {}
        finally:
            store.close()
        content = str(plan.get("design_doc_content") or "")
        repo_path = str(plan.get("design_doc") or "")
        source = str(plan.get("design_doc_source") or "")
        # The blob link only once it can be DERIVED
        blob = (github.blob_url(paths[name], repo_path)
                if content and repo_path and source and not source.startswith("branch ")
                else None)
        return render(request, "spec.html", project=name, fo_id=fo_id,
                      title=str(fo.get("title") or ""),
                      status=str(fo.get("status") or ""),
                      repo_path=repo_path, source=source, blob_url=blob,
                      body=markdown.render(content) if content else "")

    @app.get("/io/{name}/{io_id}", response_class=HTMLResponse)
    def improvement_order(request: Request, name: str, io_id: str, error: str = ""):
        """An improvement order's page — §6.1 of the improvement-orders spec"""
        try:
            detail = ops.show_improvement_order(io_id, name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e))
        return render(request, "improvement_order.html", io=detail,
                      project=detail["project"], error=error)

    @app.get("/inv/{name}/{inv_id}", response_class=HTMLResponse)
    def investigation_order(request: Request, name: str, inv_id: str, error: str = ""):
        """An investigation's page — §2.9 of docs/superpowers/specs/2026-09-27-investigation-orders.md"""
        try:
            detail = ops.show_investigation_order(inv_id, name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e))
        return render(request, "investigation_order.html", inv=detail,
                      project=detail["project"], error=error)

    @app.post("/io/{name}/{io_id}/review")
    def review_findings(name: str, io_id: str, key: str = Form(...),
                        decision: str = Form(...), reason: str = Form("")):
        """One finding, decided"""
        try:
            ops.review_findings(
                io_id, accept=(key,) if decision == "accept" else (),
                reject={} if decision == "accept" else {key: reason},
                decided_by="user", project_name=name)
        except ops.OpsError as e:
            # A rejection with no reason is the refusal that actually happens here
            return RedirectResponse(f"/io/{name}/{io_id}?error={e}", status_code=303)
        return RedirectResponse(f"/io/{name}/{io_id}", status_code=303)

    @app.get("/project/{name}/sessions", response_class=HTMLResponse)
    def project_sessions(request: Request, name: str):
        """The inject panel, as a fragment the project page pulls in after it renders"""
        found = ops.injectable_sessions(name)
        return templates.TemplateResponse(
            request, "_sessions.html",
            {"project_name": name, "sessions": found["sessions"],
             "error": found["error"]},
        )

    @app.get("/wo/{name}/{wo_id}", response_class=HTMLResponse)
    def work_order(request: Request, name: str, wo_id: str, debug: str = "",
                   forced: str = "", retried: str = "", events: str = ""):
        try:
            pname, path, wo = ops.find_work_order(wo_id, name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e),
                          hint=_stale_link_hint(name, wo_id), status_code=404)
        store = ProjectStore(path)
        try:
            # THE NEWEST PAGE, and how much is behind it — `jarvis wo show`'s rule (io-12b7d873)
            every = events == "all"
            events_page = store.list_events(wo_id, limit=None if every else 200,
                                            newest=True)
            events_hidden = store.count_events(wo_id) - len(events_page)
            messages = store.list_messages(wo_id)
            # Both lists: `assumptions` is the record, `unreviewed` is the ask — §4
            assumptions = ops.assumptions_with_rulings(store, wo_id)
            unreviewed = store.pending_assumptions(wo_id)
            # A worker held at a gate looks identical to an idle one from here
            store.expire_approvals()
            approvals = store.list_approvals(wo_id)
            # Why a `running` work order has nothing running (#714)
            account = _fleet_if_held([wo])
            pause = (invariants.pause_note(store, wo)
                     or invariants.fleet_hold_note(wo, account))
            # And why a `waiting_input` one is not in fact waiting on the reader
            waiting = ops.waiting_on(store, wo)
            # ...and the blocker the flag could not carry
            parked = invariants.parked_reason(store, wo)
            # How this status should READ — derived once, in the one function every other surface derives it from
            label = invariants.status_label(store, wo, account)
            validation = ops.validation_detail(store, wo_id=wo_id)
            # THE WHOLE ORDER'S LIST, not one round's
            issue_index = ops.issue_index(store, wo_id)
            # None for every order the automatic merge has never touched
            auto_merge = ops.automerge_state(store, wo)
            # Whether the pull request landed, from the timeline and never from `pr_state`
            merge_state = ops.merge_state(store, wo)
            # The re-judge control and the diagnosis behind it
            force = ops.force_validation_state(store, wo, project=pname,
                                               held=auto_merge)
            # What forcing a round just did, rebuilt from the record rather than carried in the query
            forced_lines = ops.forced_round_notice(
                store, wo, project=pname,
                round_n=int(forced)) if forced.isdigit() else []
            # The retry control, and the note a press just left (docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md)
            retry = ops.retry_state(store, wo)
            # WHAT THE OS SAVED when the last turn died (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md)
            harvest = ops.harvest_state(store, wo)
            retried_line = (ops.retry_queued_notice(int(retried))
                            if retried.isdigit()
                            and any(m["id"] == int(retried) for m in messages) else "")
            # And the same for the assumption review, on the same rule
            auto_review = ops.autoreview_state(store, wo)
            # WHETHER THIS ORDER OWES THE USER A DECISION, and where the one form goes
            review = ops.review_state(store, wo)
            # WHERE THE REST OF THIS ORDER IS
            spec = specs.spec_of(store, wo)
            # WHERE THAT SPEC CAN BE READ — §5(a)
            spec_link = specs.spec_link(store, wo)
            # How long it has been where it is
            states = ops.state_durations(store, wo_id=wo_id).as_dict()
        finally:
            store.close()
        show_debug = debug not in ("", "0", "false")
        bill = wo_bill(wo_id, pname)
        # The ceiling, what has gone against it
        cap = ops.work_order_budget(wo_id, pname)
        return render(request, "work_order.html", project=pname, wo=wo, parked=parked,
                      # What has already been dismissed here
                      seen=invariants.acknowledged(wo),
                      cap=cap,
                      pause=pause, waiting=waiting, status_label=label,
                      validation=validation, spec=spec, spec_link=spec_link,
                      auto_merge=auto_merge,
                      merge_state=merge_state,
                      issues=issue_index,
                      auto_review=auto_review, force=force, forced_lines=forced_lines,
                      retry=retry, retried_line=retried_line, harvest=harvest,
                      timeline=build_timeline(wo, events_page, messages,
                                              include_debug=show_debug),
                      events_hidden=events_hidden, events_all=every,
                      debug=show_debug, debug_count=count_debug(events_page),
                      # What was said, and what happened — two readings of one record, neither derivable from the other
                      conversation=build_conversation(events_page, messages),
                      assumptions=assumptions, unreviewed=unreviewed, review=review,
                      approvals=approvals, bill=bill, states=states,
                      turn_lines=turn_lines_by_message(bill))

    @app.get("/wo/{name}/{wo_id}/debug", response_class=HTMLResponse)
    def work_order_debug(request: Request, name: str, wo_id: str, filed: str = ""):
        """"Show me all of the above on one page" (docs/superpowers/specs/2026-09-24-order-observability.md)"""
        try:
            pname, _path, wo = ops.find_work_order(wo_id, name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e),
                          hint=_stale_link_hint(name, wo_id), status_code=404)
        return render(
            request, "debug.html", project=pname, wo=wo,
            diagnosis=_block("the diagnosis", ops.diagnose, wo_id, pname),
            # §11, under the diagnosis it acts on
            fix=_block("the fix", ops.fix, wo_id, pname),
            fix_filed=_fix_filed_notice(filed),
            live=_block("the live snapshot", ops.live_report, wo_id, pname),
            anatomy=_block("the anatomy", ops.inspect_report, wo_id, pname),
            context=_block("the context ledger", ops.context_report, wo_id, pname))

    @app.get("/api/wo/{name}/{wo_id}/live")
    def work_order_live(name: str, wo_id: str):
        """`ops.live_report` as JSON, nothing reshaped — what the debugging page polls"""
        try:
            return JSONResponse(ops.live_report(wo_id, name))
        except ops.OpsError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    @app.get("/cost", response_class=HTMLResponse)
    def cost_page(request: Request, project: str = "", window: str = "",
                  offset: str = "", since: str = "", until: str = "",
                  tz: str = ""):
        """What the fleet's work cost, dearest first — the dashboard half of `jarvis cost` (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
        try:
            # The zone FIRST: the custom form's naive datetimes are parsed in the same zone they are rendered in
            zone = ops.cost_zone(tz or None, project or None)
            picked = ops.cost_window(project=project or None, window=window or None,
                                     offset=_offset_param(offset), tz=tz or None,
                                     since=_in_zone(since, zone) if since else None,
                                     until=_in_zone(until, zone) if until else None)
            # docs/superpowers/specs/2026-10-22-cost-tab-precompute.md §Matching rule
            entry = None
            if (not project and offset in ("", "0") and not since and not until
                    and (not tz or tz == ops.cost_zone(None, None))):
                entry = cost_cache.lookup(picked)
            if entry is not None:
                picked, payloads = entry["window"], entry
            else:
                payloads = _cost_payloads(picked, project, request.method)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e))
        report, fleet, meter = payloads["report"], payloads["fleet"], payloads["meter"]
        cached_at = entry["computed_at"] if entry is not None else None
        # Its own variable and never read out of `fleet`: losing the section must not lose the window the...
        return render(request, "cost.html", active="cost", report=report,
                      units=report["units"], totals=report["totals"],
                      project=project, fleet=fleet, meter=meter, window=picked,
                      cached_at=cached_at, now=time.time(),
                      window_inputs=_window_inputs(picked),
                      projects=sorted(ops.registered_project_paths()))

    @app.get("/cost/{name}/{order_id}", response_class=HTMLResponse)
    def bill_page(request: Request, name: str, order_id: str):
        """One order's itemised bill — the page that answers "where did my tokens go\""""
        try:
            bill = ops.bill(order_id, project=name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e), status_code=404)
        turn_rows = bill.get("turn_rows") or []
        # Bars are scaled to the context window when any turn reports one
        scale = max([t.get("context_window") or 0 for t in turn_rows]
                    + [t.get("context_peak") or 0 for t in turn_rows] + [1])
        return render(request, "bill.html", active="cost", bill=bill, project=name,
                      turn_rows=turn_rows, bar_scale=scale, own_label=OWN_LABEL)

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
        """The base, and what it costs against what it is used for"""
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
        """Pinned entries ride in every worker prompt verbatim"""
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
            # Oldest first: that is the order Neo drains them, and the oldest is the one most likely to be stuck
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
            # How the panel deliberated, for the questions this page already shows
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

    @app.get("/neo/stats", response_class=HTMLResponse)
    def neo_stats_page(request: Request, project: str = "", days: int | None = None):
        """Neo's own report — §6 of docs/superpowers/specs/2026-10-01-neo-observability.md"""
        try:
            report = ops.neo_stats_report(project=project or None, days=days)
        except ops.OpsError as e:
            return render(request, "error.html", active="neo", message=str(e))
        return render(request, "neo_stats.html", active="neo", report=report,
                      project=project, days=days,
                      projects=sorted(ops.registered_project_paths()))

    @app.get("/neo/question/{question_id}", response_class=HTMLResponse)
    def neo_question_page(request: Request, question_id: int):
        """One question and its answer — where a work order's timeline sends the reader (docs/superpowers/specs/2026-08-23-the-work-order-record.md)"""
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
        """Privileged-action approvals"""
        rows = ops.list_gates(include_request=True)
        pending = [g for g in rows if g["status"] == "pending"]
        held = [g for g in rows if g["status"] == "awaiting_case"]
        dismissed = [g for g in rows if g["status"] == "dismissed"]
        decided = [g for g in rows
                   if g["status"] not in ("pending", "awaiting_case", "dismissed")]
        return render(request, "gates.html", active="gates",
                      escalated=[g for g in pending if g["escalated"]],
                      with_neo=[g for g in pending if not g["escalated"]],
                      held=held, decided=decided, dismissed=dismissed,
                      false_positive_rate=_false_positive_rate(rows),
                      abandoned=_abandoned(rows))

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request, a: str = "", b: str = "", scope: str = "",
                    node: str = "", q: str = "", refresh: str = ""):
        """What the fleet is configured to run, who changed it, and what changed (docs/superpowers/specs/2026-08-27-the-config-console.md §8)"""
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
        # Default the diff to the change that landed: the head against the version before it
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
                      wiring=wiring_groups(scope, refresh=bool(refresh)),
                      scopes=scopes, scope=scope, node=node, q=q,
                      scope_title=next(s["title"] for s in scopes if s["key"] == scope),
                      # The tree root wants the SHORT name: "os
                      scope_short="os" if scope == "os" else scope.split(".", 1)[1],
                      tree=config_tree(labels, node),
                      rows=config_rows(show, config_setters(history), scope, node, q),
                      total=len(labels), here=_config_url(scope, node, q),
                      history=history, diff=diff, diff_error=diff_error, a=a, b=b)

    @app.get("/alarms", response_class=HTMLResponse)
    def alarms_page(request: Request):
        """Turns the OS raised WHILE they were still costing money"""
        rows = ops.alarm_feed()
        live: dict[str, dict] = {}
        for a in (r for r in rows if r["live"]):
            group = live.setdefault(a["subject_id"], {**a, "alarms": []})
            group["alarms"].append(a)
        return render(request, "alarms.html", active="alarms",
                      live=list(live.values()),
                      review_queue=ops.alarm_review_queue(),
                      history=[r for r in rows if not r["live"]],
                      kinds=ALARM_KINDS)

    @app.get("/stuck", response_class=HTMLResponse)
    def stuck_page(request: Request):
        """Open orders judged on time in status (docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md)"""
        rows = ops.stuck_report()
        return render(request, "stuck.html", active="stuck",
                      over=[r for r in rows if r["stuck"]],
                      rest=[r for r in rows if not r["stuck"]])

    @app.get("/morpheus", response_class=HTMLResponse)
    def morpheus_page(request: Request):
        """Shipped PRs watched in production (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""
        return render(request, "morpheus.html", active="morpheus",
                      report=ops.morpheus_report())

    @app.get("/evolution", response_class=HTMLResponse)
    def evolution_page(request: Request, days: int = 90):
        """Is the OS getting better at running itself — and where is it still weak? (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
        return render(request, "evolution.html", active="evolution",
                      report=ops.evolution_report(None, days=days))

    @app.get("/evolution/{project}", response_class=HTMLResponse)
    def evolution_project_page(request: Request, project: str, days: int = 90):
        """The same report scoped to one project — where each project page links"""
        try:
            report = ops.evolution_report(project, days=days)
        except ops.OpsError as e:
            return render(request, "error.html", active="evolution",
                          message=str(e), status_code=404)
        return render(request, "evolution.html", active="evolution", report=report)

    @app.get("/alarms/{project}/{alarm_id}", response_class=HTMLResponse)
    def alarm_page(request: Request, project: str, alarm_id: str):
        """One alarm — where the work order's timeline and a Neo escalation both link (docs/superpowers/specs/2026-08-31-the-supervisor.md)"""
        try:
            alarm = ops.alarm_detail(alarm_id, project_name=project)
        except ops.OpsError as e:
            return render(request, "error.html", active="alarms",
                          message=str(e), status_code=404)
        return render(request, "alarm.html", active="alarms", a=alarm)

    @app.get("/api/status")
    def api_status():
        return JSONResponse(ops.os_status())

    # -- actions (same ops functions as the CLI)

    @app.post("/wo/create")
    def create_wo(project: str = Form(...), title: str = Form(...),
                  description: str = Form(""), model: str = Form(""),
                  budget: str = Form("")):
        from .. import budget as budget_mod

        try:
            wo = ops.create_work_order(
                project, title, description=description, model=model or None,
                origin="ui",
                # Empty means "whatever the project's default is", which is usually no ceiling — never zero
                budget_usd=budget_mod.parse_amount(budget) if budget.strip() else None)
        except (ValueError, ops.OpsError) as e:
            return RedirectResponse(f"/?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{project}/{wo['id']}", status_code=303)

    @app.post("/project/{name}/inject")
    def inject(name: str, session_id: str = Form(...), title: str = Form("")):
        """Hand one of the user's own Claude sessions to Jarvis"""
        try:
            res = ops.inject_session(session_id, project_name=name, title=title or None)
        except ops.OpsError as e:
            return RedirectResponse(f"/project/{name}?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{name}/{res['wo_id']}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/send")
    def send(name: str, wo_id: str, message: str = Form(...)):
        ops.send_message(wo_id, message, source="ui", project_name=name, relay=True)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/review")
    def review(name: str, wo_id: str, decision: str = Form(...),
               feedback: str = Form("")):
        ops.review_work_order(wo_id, accept=(decision == "accept"), feedback=feedback)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/budget")
    def set_wo_budget(name: str, wo_id: str, amount: str = Form(""),
                      clear: str = Form("")):
        """Set, raise or clear one work order's ceiling — and resume it if it stopped"""
        from .. import budget

        if not clear and not amount.strip():
            return RedirectResponse(
                f"/wo/{name}/{wo_id}?error=enter+an+amount+or+use+Remove+ceiling",
                status_code=303)
        try:
            parsed = None if clear else budget.parse_amount(amount)
            result = ops.set_work_order_budget(wo_id, parsed, project_name=name)
        except (ValueError, ops.OpsError) as e:
            return RedirectResponse(f"/wo/{name}/{wo_id}?error={e}", status_code=303)
        # A RAISE THAT CHANGED NOTHING STILL OWES THE USER A SENTENCE (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
        if result.get("note") and not result.get("resumed"):
            return RedirectResponse(f"/wo/{name}/{wo_id}?note={quote(result['note'])}",
                                    status_code=303)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/fo/{name}/{fo_id}/budget")
    def set_fo_budget(name: str, fo_id: str, amount: str = Form(""),
                      clear: str = Form("")):
        """The same, for a feature's whole family"""
        from .. import budget

        if not clear and not amount.strip():
            return RedirectResponse(
                f"/fo/{name}/{fo_id}?error=enter+an+amount+or+use+Remove+ceiling",
                status_code=303)
        try:
            parsed = None if clear else budget.parse_amount(amount)
            result = ops.set_feature_budget(fo_id, parsed, project_name=name)
        except (ValueError, ops.OpsError) as e:
            return RedirectResponse(f"/fo/{name}/{fo_id}?error={e}", status_code=303)
        # The same channel: `exhausted_children` is the WHOLE INSTRUCTION to the user
        stuck = result.get("exhausted_children") or []
        if stuck:
            one = len(stuck) == 1
            # The WHOLE clause agrees, not only the noun: "1 work order ... their own ceiling" was half-pluralised
            note = (f"budget raised. {len(stuck)} work order{'' if one else 's'} still "
                    f"parked on {'its own ceiling' if one else 'their own ceilings'}: "
                    f"{', '.join(stuck)} — spend the new money on one with "
                    f"`jarvis wo budget <id> <amount>`")
            return RedirectResponse(f"/fo/{name}/{fo_id}?note={quote(note)}",
                                    status_code=303)
        return RedirectResponse(f"/fo/{name}/{fo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/cancel")
    def cancel_wo(name: str, wo_id: str):
        ops.cancel(wo_id)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/done")
    def done_wo(name: str, wo_id: str):
        try:
            ops.mark_done(wo_id, project_name=name)
        except ops.OpsError as e:
            # Pending assumptions are the one refusal: they want a decision, not a close
            return RedirectResponse(f"/wo/{name}/{wo_id}?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/ack")
    def ack_wo(name: str, wo_id: str, back: str = Form("")):
        # `back` is where the user pressed the button
        home = f"/wo/{name}/{wo_id}"
        landing = "/alarms" if back == "alarms" else home
        try:
            ops.ack_attention(wo_id, project_name=name)
        except ops.OpsError as e:
            # The one case that refuses: pending assumptions want a decision, not a dismissal
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

    @app.post("/wo/{name}/{wo_id}/fix")
    def fix_wo(name: str, wo_id: str):
        """`jarvis wo fix --confirm` from the debugging page"""
        back = f"/wo/{name}/{wo_id}/debug"
        try:
            out = ops.fix(wo_id, name, confirm=True)
        except ops.OpsError:
            return RedirectResponse(f"{back}#block-fix", status_code=303)
        filed = out["filed"] or {}
        if filed.get("unreachable"):
            held = filed.get("approval")
            tag = (f"{ops.FIX_PENDING_PREFIX}{int(held)}" if held else "unreachable")
            return RedirectResponse(f"{back}?filed={tag}#block-fix", status_code=303)
        approval = filed.get("approval")
        if not approval:
            return RedirectResponse(f"{back}#block-fix", status_code=303)
        return RedirectResponse(f"{back}?filed={int(approval)}#block-fix",
                                status_code=303)

    @app.post("/wo/{name}/{wo_id}/retry")
    def retry_wo(name: str, wo_id: str, message: str = Form("")):
        """`jarvis wo retry` from the work-order page (docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md)"""
        back = f"/wo/{name}/{wo_id}"
        try:
            out = ops.retry(wo_id, message=message.strip() or None, project_name=name,
                            relay=True)
        except ops.OpsError as e:
            return RedirectResponse(
                f"{back}?{urlencode({'error': str(e)}, quote_via=quote)}#retry",
                status_code=303)
        return RedirectResponse(f"{back}?retried={out['msg_id']}#retry", status_code=303)

    @app.post("/wo/{name}/{wo_id}/validation/force")
    def force_validation(name: str, wo_id: str, reason: str = Form(...)):
        """`jarvis validation force` from the dashboard"""
        back = f"/wo/{name}/{wo_id}"
        try:
            result = ops.force_validation(wo_id, reason=reason, project_name=name)
        except ops.OpsError as e:
            return RedirectResponse(
                f"{back}?{urlencode({'error': str(e)}, quote_via=quote)}#rejudge",
                status_code=303)
        return RedirectResponse(f"{back}?forced={result['round']}#rejudge",
                                status_code=303)

    @app.post("/fo/create")
    def create_fo(project: str = Form(...), title: str = Form(...),
                  description: str = Form(""), budget: str = Form("")):
        from .. import budget as budget_mod

        try:
            fo = ops.create_feature_order(
                project, title, description=description, origin="ui",
                # A FAMILY budget: the whole rollup, not the planner alone
                budget_usd=budget_mod.parse_amount(budget) if budget.strip() else None)
        except (ValueError, ops.OpsError) as e:
            return RedirectResponse(f"/project/{project}?error={e}", status_code=303)
        return RedirectResponse(f"/fo/{project}/{fo['id']}", status_code=303)

    @app.post("/fo/{name}/{fo_id}/review")
    def review_plan(name: str, fo_id: str, decision: str = Form(...),
                    feedback: str = Form("")):
        try:
            ops.review_plan(fo_id, accept=(decision == "accept"), feedback=feedback,
                            decided_by="user", project_name=name)
        except ops.OpsError as e:
            # A rejection with no reason is the refusal that actually happens here: the planner sees only the...
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
        """Where a decision returns the reader — the page they decided from, or the surface's own fallback"""
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
        """The user's verdict on the supervisor's"""
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
        """Open or refuse a gate"""
        back = next if next.startswith("/") and not next.startswith("//") else "/gates"
        # Unknown button values fall through to `denied`
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
        """The page's only write, and it is `jarvis config set` under another name"""
        back = back if back.startswith("/config") else "/config"
        try:
            current = ops.config_get(path)["value"]
            new = config_value_of(current, value)
            if not config_type_ok(current, new):
                raise ops.OpsError(
                    f"{path} takes {config_type_name(current)} — {value!r} is not. "
                    f"`jarvis config set {path} <value>` says the same thing.")
            ops.set_config(path, new, reason=reason)
        # ValueError/TypeError: `parse_catalog` coerces with bare `int()`/`str()` and raises neither...
        except (ops.OpsError, ValueError, TypeError) as e:
            sep = "&" if "?" in back else "?"
            # `quote`, not the default `quote_plus`: the flash is read by a human off the address bar as often as...
            return RedirectResponse(
                f"{back}{sep}{urlencode({'error': str(e)}, quote_via=quote)}",
                status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/config/wiring")
    def config_wiring(lever: str = Form(...), wired: str = Form(""),
                      scope: str = Form("os"), back: str = Form("/config")):
        """Wire or unwire one thing for one scope — `ops.set_wiring` under another name"""
        back = back if back.startswith("/config") else "/config"
        try:
            ops.set_wiring(lever, wired == "1", project=wiring_project(scope))
        except (ops.OpsError, ValueError, TypeError) as e:
            sep = "&" if "?" in back else "?"
            return RedirectResponse(
                f"{back}{sep}{urlencode({'error': str(e)}, quote_via=quote)}",
                status_code=303)
        return RedirectResponse(back, status_code=303)

    # -- the brake: `jarvis pause` / `jarvis resume` (issue #843)

    @app.post("/fleet/pause")
    def pause_fleet(reason: str = Form(""), next: str = Form("")):
        """`jarvis pause`, from the dashboard"""
        central = CentralStore()
        try:
            current = fleet.load_pause(central)
        finally:
            central.close()
        if current is not None:
            return RedirectResponse(_same_site_back(
                next, "/", quote("the fleet is already paused — nothing changed")),
                status_code=303)
        ops.pause_fleet(reason=reason.strip())
        return RedirectResponse(_same_site_back(next, "/"), status_code=303)

    @app.post("/fleet/resume")
    def resume_fleet(next: str = Form("")):
        """`jarvis resume --all`: lift the pause for the whole fleet"""
        ops.resume_fleet(everything=True)
        return RedirectResponse(_same_site_back(next, "/"), status_code=303)

    @app.post("/fleet/allow")
    def allow_through(order_id: str = Form(...), next: str = Form("")):
        """`jarvis resume <wo-id>`: let one order through the pause"""
        try:
            ops.resume_fleet([order_id.strip()])
        except ops.OpsError as e:
            return RedirectResponse(_same_site_back(next, "/", quote(str(e))),
                                    status_code=303)
        return RedirectResponse(_same_site_back(next, "/"), status_code=303)

    @app.post("/inbox/ack")
    def ack(inbox_id: str = Form("")):
        ops.ack_inbox(int(inbox_id) if inbox_id else None)
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/backlog/promote/{item_id}")
    def promote(item_id: str, force: str = Form("")):
        try:
            result = ops.promote_backlog(item_id, force=bool(force))
        except ops.OpsError as e:
            return RedirectResponse(f"/backlog?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{result['project']}/{result['wo_id']}", status_code=303)

    return app
