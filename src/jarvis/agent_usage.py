"""What the OS itself spends on Claude, recorded against the work order that caused it.

A work order's bill is not only its worker's turns. Every question the worker asked Neo,
every seat of the panel that deliberated on it, every digest written so the dashboard can
render it short — each is a `claude -p` call Jarvis made, and paid for, BECAUSE of that
work order. Until this module existed none of it was counted anywhere: `run_headless`
parsed the CLI's result JSON for its `result` string and dropped the `usage` object beside
it, so `jarvis cost` reported the worker and stayed silent about the OS.

## Why it has to be written down, not derived later

Worker turns can be recovered after the fact — Jarvis mints the session id, Claude Code
keeps a transcript, and `usage.py` reads it back. None of that holds here. An OS call is a
one-shot `claude -p` with no id Jarvis chose, no work order named anywhere in it, and a
transcript under a slugified `$JARVIS_HOME` shared with every other OS call ever made.
There is nothing to attribute after the fact, so the envelope is persisted at the moment
the call returns or it is lost.

## The seam

Every call site takes a `record=` parameter defaulting to `record` here, the same shape as
`neo.drain_queue`'s existing `answer=` seam: it keeps the store out of `panel._run_seat`
(which runs on a pool thread, where a sqlite connection from another thread would be a
bug) and lets a test assert on what would have been written without a database at all.

## It never raises

Accounting is an observer. A work order must not fail, and Neo must not stop answering,
because the OS could not write down what an answer cost — so every failure here is logged
and swallowed. The cost of that choice is a missing row, and a missing row is visible:
`agent_calls` counts what it has, and the number it reports is a floor.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import claude_cli
from .central_store import CentralStore

log = logging.getLogger(__name__)

#: What kind of OS work a call was, as stored in `agent_calls.kind`. Open by design —
#: an unknown kind records fine and shows up in the report under its own name — but the
#: ones the OS emits today are named here so a reader of the schema knows what to expect,
#: and so the UI can put a word to each.
KIND_LABELS = {
    "neo_answer": "Neo answering",
    "panel_seat": "panel seat",
    "digest": "dashboard digest",
}


def describe(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " "))


def record(kind: str, *, usage: Any = None, project: str = "", wo_id: str = "",
           label: str = "", model: str = "", question_id: int | None = None,
           ok: bool = True, store: CentralStore | None = None) -> int | None:
    """Persist one OS-side Claude call. Returns the row id, or None if nothing was written.

    `usage` takes either a `claude_cli.derive_turn_usage` envelope or the
    `HeadlessResult` it came on, because both are what a call site has to hand.

    A call with no work order (`wo_id=""`) is still recorded: it is OS overhead that
    belongs in the fleet total even though no single work order caused it.
    """
    if isinstance(usage, claude_cli.HeadlessResult):
        model = model or usage.model
        usage = usage.usage
    if usage is not None and not isinstance(usage, dict):
        usage = None
    own = store is None
    try:
        store = store or CentralStore()
        return store.add_agent_call(kind, project=project, wo_id=wo_id, label=label,
                                    model=model, question_id=question_id, ok=ok,
                                    usage=usage)
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
    """`record` with everything but the usage already bound.

    For the transports that hand their accounting to a callback rather than returning it
    — `structured.request`, and so `digest.summarise` — where the call site knows which
    work order it is spending for but the transport does not.
    """
    def sink(usage: Any) -> None:
        record(kind, usage=usage, project=project, wo_id=wo_id, label=label,
               model=model, question_id=question_id)

    return sink
