"""What the OS itself spends on Claude, recorded against the work order that caused it (docs/superpowers/specs/2026-09-25-attribution-is-not-a-callers-choice.md)"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from . import claude_cli
from .central_store import CentralStore

log = logging.getLogger(__name__)

#: The kind recorded for a `claude` process a WORKER spawned beneath itself (#103)
WORKER_SUBPROCESS = "worker_subprocess"

#: The `/compact` the OS sends before a prompt whose cache has expired (`worker_session.compact`)
COMPACTION = "compaction"

#: Kinds that belong to the WORKER rather than to the OS
SUBPROCESS_KINDS = frozenset({WORKER_SUBPROCESS})

#: What the user spent LOOKING at an order (docs/superpowers/specs/2026-09-24-order-observability.md)
OBSERVE_LIVE = "observe_live"
OBSERVE_INSPECT = "observe_inspect"
OBSERVE_CONTEXT = "observe_context"
OBSERVE_WHY = "observe_why"
OBSERVE_CONTEXT_WRITE = "observe_context_write"

OBSERVABILITY_KINDS = frozenset({OBSERVE_LIVE, OBSERVE_INSPECT, OBSERVE_CONTEXT,
                                 OBSERVE_WHY, OBSERVE_CONTEXT_WRITE})

#: What Neo COSTS — §4 of docs/superpowers/specs/2026-10-01-neo-observability.md
NEO_KINDS = frozenset({"neo_answer", "panel_seat", "validation_seat",
                       "stakes_classifier", "digest", "supervisor", "health"})

#: What kind of OS work a call was, as stored in `agent_calls.kind`
KIND_LABELS = {
    "neo_answer": "Neo answering",
    "panel_seat": "panel seat",
    "validation_seat": "validation seat",
    "digest": "dashboard digest",
    "supervisor": "supervisor reviewing an alarm",
    # A SEPARATE KIND FROM `supervisor` (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
    "health": "supervisor health review",
    # A KIND OF ITS OWN AND NOT A LABEL ON `neo_answer`
    "stakes_classifier": "classifying an assumption's stakes",
    COMPACTION: "compacting a cold conversation",
    WORKER_SUBPROCESS: "worker subprocess",
    OBSERVE_LIVE: "watching the turn in flight",
    OBSERVE_INSPECT: "inspecting where the time went",
    OBSERVE_CONTEXT: "reading the context ledger",
    OBSERVE_WHY: "diagnosing why the order is not moving",
    OBSERVE_CONTEXT_WRITE: "recording the context ledger",
}

#: Where accounting rows go when that must differ from `$JARVIS_HOME` (#103)
SPEND_HOME_ENV = "JARVIS_SPEND_HOME"


def spend_db_path() -> Path | None:
    """The `os.db` accounting rows go to, or None to use the ambient `$JARVIS_HOME`"""
    home = os.environ.get(SPEND_HOME_ENV, "").strip()
    return Path(home).expanduser() / "os.db" if home else None


def describe(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " "))


def is_subprocess(kind: str) -> bool:
    """Does this `agent_calls.kind` belong to the worker rather than to the OS?"""
    return kind in SUBPROCESS_KINDS


def is_observability(kind: str) -> bool:
    """Was this row the user LOOKING at the order rather than the OS working on it?"""
    return kind in OBSERVABILITY_KINDS


def record(kind: str, *, usage: Any = None, project: str = "", wo_id: str = "",
           label: str = "", model: str = "", question_id: int | None = None,
           ok: bool = True, session_id: str = "",
           store: CentralStore | None = None) -> int | None:
    """Persist one OS-side Claude call"""
    prompt_chars = system_prompt_chars = 0
    #: None, never 0: a sub-millisecond call rounds to 0 and the report must not print "0 ms" for a call... (docs/superpowers/specs/2026-10-01-neo-observability.md)
    latency_ms: int | None = None
    if isinstance(usage, claude_cli.HeadlessResult):
        model = model or usage.model
        # How big the OS's own input was (spec §3, docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
        prompt_chars = usage.prompt_chars
        system_prompt_chars = usage.system_prompt_chars
        # Off the dataclass here, off the envelope below — `prompt_chars`' rule exactly
        latency_ms = usage.latency_ms or None
        # The session the CLI minted for this call
        session_id = session_id or usage.session_id
        usage = usage.usage
    if usage is not None and not isinstance(usage, dict):
        usage = None
    if isinstance(usage, dict) and not prompt_chars and not system_prompt_chars:
        prompt_chars = int(usage.get("prompt_chars") or 0)
        system_prompt_chars = int(usage.get("system_prompt_chars") or 0)
    if isinstance(usage, dict) and latency_ms is None:
        latency_ms = int(usage.get("latency_ms") or 0) or None
    own = store is None
    try:
        # `spend_db_path()` is None in every ordinary case
        store = store or CentralStore(spend_db_path())
        return store.add_agent_call(kind, project=project, wo_id=wo_id, label=label,
                                    model=model, question_id=question_id, ok=ok,
                                    session_id=session_id, usage=usage,
                                    prompt_chars=prompt_chars,
                                    system_prompt_chars=system_prompt_chars,
                                    latency_ms=latency_ms)
    except Exception:  # noqa: BLE001 — see the module docstring: never raise
        log.warning("could not record %s usage for %s", kind, wo_id or "the OS",
                    exc_info=True)
        return None
    finally:
        if own and isinstance(store, CentralStore):
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def recorder(kind: str, *, project: str = "", wo_id: str = "", label: str = "",
             model: str = "", question_id: int | None = None,
             record: Callable[..., Any] = record) -> Callable[[Any], None]:
    """`record` with everything but the usage already bound"""
    def sink(usage: Any) -> None:
        record(kind, usage=usage, project=project, wo_id=wo_id, label=label,
               model=model, question_id=question_id)

    return sink
