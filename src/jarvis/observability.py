"""Who turns debug collection on, and at what level (docs/superpowers/specs/2026-09-24-order-observability.md)"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Callable

from . import agent_usage

log = logging.getLogger(__name__)

#: The three levels
OFF, NORMAL, FULL = "off", "normal", "full"
LEVELS = (OFF, NORMAL, FULL)

#: What the fleet ships at (§10)
DEFAULT_LEVEL = NORMAL


def level_for(wo: Any = None, cfg: Any = None) -> str:
    """The level in force for one work order: its column, else the config, else `normal`"""
    stored = (wo or {}).get("observability") if hasattr(wo, "get") else None
    if stored:
        stored = str(stored).strip().lower()
        # An unrecognised stored level falls through rather than raising: this is a read path on every turn
        if stored in LEVELS:
            return stored
    level = getattr(cfg, "level", None)
    if level and str(level).strip().lower() in LEVELS:
        return str(level).strip().lower()
    return DEFAULT_LEVEL


def records_context(wo: Any = None, cfg: Any = None) -> bool:
    """The one predicate §5's writer consults"""
    return level_for(wo, cfg) != OFF


# -- the meter


#: Written as ZEROS and never left out
ZERO_USAGE = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
              "total_cost_usd": 0.0}


def metered(kind: str, *, target: str, project: str = "",
            record: Callable[..., Any] | None = None) -> Callable[[Any], Any]:
    """Wrap ONE report function at its definition: exactly one `agent_usage` row per call"""
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
    """Write the one row"""
    try:
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        write = record or agent_usage.record
        write(kind,
              # NO `usage_v`: no reading was derived here, so the stamp is ABSENT rather than current
              usage={"wall_ms": int((time.monotonic() - started) * 1000), **ZERO_USAGE},
              project=_project_of(bound.arguments.get(project)) if project else "",
              wo_id=_id_of(bound.arguments.get(target)), ok=ok)
    except Exception:  # noqa: BLE001 — an observer never fails what it observes
        log.warning("could not meter %s", kind, exc_info=True)


def _id_of(value: Any) -> str:
    """The id a report was run for, from a string, a work-order row or an object"""
    if isinstance(value, str):
        return value.strip()
    if hasattr(value, "get"):
        return str(value.get("id") or "")
    return str(getattr(value, "id", "") or "")


def _project_of(value: Any) -> str:
    """The project name, from the call's project argument — a name or a spec"""
    if isinstance(value, str):
        return value.strip()
    return str(getattr(value, "name", "") or "")
