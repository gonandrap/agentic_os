"""Who turns debug collection on, and at what level.

§10 of docs/specs/2026-09-24-order-observability.md. That section keeps TWO THINGS APART
and so does this module's scope: the GATE (this file — whether Jarvis COLLECTS debug data)
and the METER (what looking at an order COSTS, recorded through `agent_usage` at every
level and never switchable off). Conflating them is what makes the rest of the feature
read as a contradiction.

THE GATE GOVERNS TWO WRITES: §5's per-turn ingredient row on `wo_turns.context_json`
(`context.record`, its only consumer) and the sealed autopsy (`autopsy.records_autopsy`,
§5 of docs/specs/2026-09-27-order-autopsy-durability.md). It governs no read — so `off`
stops the autopsy being sealed and does NOT disable `jarvis watch`, `jarvis inspect`,
`jarvis wo why` or the debug page, which are arithmetic over files Claude Code already
wrote, collect nothing, and would cost the user the very view they opened. What a user
notices at `off` is that the order has no context ledger and no sealed autopsy afterwards,
so `jarvis wo context` reports it as not recorded.

`FULL` AND `NORMAL` BOTH RECORD THE CONTEXT ROW, and differ by exactly one thing: the tool
parameters a `full` seal retains (§6 of docs/specs/2026-09-27-order-autopsy-durability.md).
The autopsy READING itself — every turn, its tools, its token classes and its context
total, delta, peak and composition — is read-time arithmetic over the transcript (§§3, 4,
6, 7) and is shown for every order at every level, `off` included; what `off` withholds is
the SEAL that makes it survive the transcript.

A LEAF: it imports `agent_usage` (a leaf itself, and the meter's only seam) and nothing
from `ops`, `bill` or `dispatch` — every one of which the metered functions live in or
reach, so an import in that direction would be a cycle.

Not even importing `catalog`, either: the only thing wanted from a config object is
its `level`, read by `getattr`, so the default literal lives in `catalog.ObservabilityConfig`
and the dependency runs in neither direction (catalog importing this module would be a
cycle the moment this module wanted a typed config).
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Callable

from . import agent_usage

log = logging.getLogger(__name__)

#: The three levels. `off` = no per-turn ingredient row, no sealed autopsy, no read gated.
OFF, NORMAL, FULL = "off", "normal", "full"
LEVELS = (OFF, NORMAL, FULL)

#: What the fleet ships at (§10). Recording one row per turn is cheap arithmetic, and an
#: order with no ledger cannot be debugged after the fact.
DEFAULT_LEVEL = NORMAL


def level_for(wo: Any = None, cfg: Any = None) -> str:
    """The level in force for one work order: its column, else the config, else `normal`.

    ONE CONFIG OBJECT, never two. A `ProjectSpec.observability` is already resolved
    against `os.observability` by catalog's field-level inheritance (`_parse_observability`,
    the same shape `_parse_inspect` uses), so this reads the project answer and no caller
    reconciles a project against a fleet.

    An EMPTY order column (NULL, or "") means "this order has no answer" — `budget_usd`'s
    precedent — and falls through. It is NOT `off`; every row written before the column
    existed says that, and reading it as `off` would retroactively silence the fleet.
    """
    stored = (wo or {}).get("observability") if hasattr(wo, "get") else None
    if stored:
        stored = str(stored).strip().lower()
        # An unrecognised stored level falls through rather than raising: this is a read
        # path on every turn, and a bad row must not take the turn down with it.
        if stored in LEVELS:
            return stored
    level = getattr(cfg, "level", None)
    if level and str(level).strip().lower() in LEVELS:
        return str(level).strip().lower()
    return DEFAULT_LEVEL


def records_context(wo: Any = None, cfg: Any = None) -> bool:
    """The one predicate §5's writer consults. `full` and `normal` both record."""
    return level_for(wo, cfg) != OFF


# -- the meter -------------------------------------------------------------------------
#
# SEPARATE FROM EVERYTHING ABOVE, and nothing below reads the gate. The gate decides
# whether Jarvis COLLECTS; the meter records what LOOKING at an order cost, at every level
# including `off`, and has no off switch — a meter the user can disable cannot answer the
# question the meter exists to answer (§10).
#
# The five kinds live in `agent_usage` beside `WORKER_SUBPROCESS`; see their comment for
# why a measured zero is the point.


#: Written as ZEROS and never left out. `add_agent_call` would default an absent figure to
#: 0 in the token columns anyway, but the envelope in `usage_json` is what a reader opens,
#: and there an absent key says "no reading was taken" while a 0 says "the reading was
#: zero" — which is the only claim this feature makes about its own cost (§10, and §2's
#: standing rule that absent is not zero).
ZERO_USAGE = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
              "total_cost_usd": 0.0}


def metered(kind: str, *, target: str, project: str = "",
            record: Callable[..., Any] | None = None) -> Callable[[Any], Any]:
    """Wrap ONE report function at its definition: exactly one `agent_usage` row per call.

    `target` and `project` name PARAMETERS of the wrapped function. The target is the id
    the report was run for and becomes `wo_id` — an `fo-` id there is already precedent
    (`bill.for_feature_order` reads `agent_calls` by the feature's own id). A parameter
    holding a work-order dict or a project spec is read for its `id` / `name`, so a call
    site keeps the signature it has.

    WALL CLOCK ONLY, as `wall_ms` in the envelope, per Neo's ruling on question 767: it
    lives in `agent_calls.usage_json` and no bill payload block is added for it.

    IT NEVER FAILS THE REPORT IT MEASURES. The row is written after the call returns; if
    the call RAISES the row is still written, marked `ok=False`, and the exception
    propagates untouched. Every failure inside the meter is swallowed and logged —
    accounting is an observer (`agent_usage`'s own docstring) — so a report returns the
    same payload byte for byte whether or not the meter worked.

    `record` defaults to `agent_usage.record`, looked up when the wrapped function RUNS
    rather than bound here: this decorator is applied at import, so a default bound at
    decoration time could never be substituted, and the seam exists precisely so a test
    can assert on what would be written with no database (the `agent_usage` convention).
    """
    def decorate(fn: Any) -> Any:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            ok = True
            try:
                return fn(*args, **kwargs)
            except BaseException:
                ok = False
                raise
            finally:
                _meter(kind, signature, args, kwargs, target, project,
                       started, ok, record)

        return wrapper

    return decorate


def _meter(kind: str, signature: Any, args: Any, kwargs: Any, target: str, project: str,
           started: float, ok: bool, record: Callable[..., Any] | None) -> None:
    """Write the one row. Swallows everything: see `metered`."""
    try:
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        write = record or agent_usage.record
        write(kind,
              # NO `usage_v`: no reading was derived here, so the stamp is ABSENT rather
              # than current — stamping one would be a claim about a `derive_turn_usage`
              # that never ran, indistinguishable to a reader of `usage_json` from a real
              # one. `bill._call_versions` skips these rows for the same reason.
              usage={"wall_ms": int((time.monotonic() - started) * 1000), **ZERO_USAGE},
              project=_project_of(bound.arguments.get(project)) if project else "",
              wo_id=_id_of(bound.arguments.get(target)), ok=ok)
    except Exception:  # noqa: BLE001 — an observer never fails what it observes
        log.warning("could not meter %s", kind, exc_info=True)


def _id_of(value: Any) -> str:
    """The id a report was run for, from a string, a work-order row or an object."""
    if isinstance(value, str):
        return value.strip()
    if hasattr(value, "get"):
        return str(value.get("id") or "")
    return str(getattr(value, "id", "") or "")


def _project_of(value: Any) -> str:
    """The project name, from the call's project argument — a name or a spec."""
    if isinstance(value, str):
        return value.strip()
    return str(getattr(value, "name", "") or "")
