"""What the OS itself spends on Claude, recorded against the work order that caused it.

A work order's bill is not only its worker's turns. Every question the worker asked Neo,
every seat of the panel that deliberated on it, every digest written so the dashboard can
render it short — each is a `claude -p` call Jarvis made, and paid for, BECAUSE of that
work order. Until this module existed none of it was counted anywhere: `run_headless`
parsed the CLI's result JSON for its `result` string and dropped the `usage` object beside
it, so `jarvis cost` reported the worker and stayed silent about the OS.

## The other direction: what the worker spends BELOW itself

A work order's bill is not only its worker's turns for a second reason, pointing the
opposite way. When one of the worker's own tool calls spawns further `claude` processes —
an eval suite, a script, a nested agent harness — each gets a session id Jarvis never
minted, writes its transcript under the slugified cwd it happened to run in (for the LLM
evals, a pytest tmp dir nowhere near the worktree), and names no work order anywhere. So
the same rule applies to them as to the OS's own calls, and for the same reason; they are
recorded here under `WORKER_SUBPROCESS`, and reported as their own class rather than
folded in with Jarvis's overhead. `claude_cli.run_headless_result` is where the recording
happens, keyed on `JARVIS_WO_ID` being present in the environment.

This is necessarily incomplete: a bare `claude -p` run from a shell does not come through
Jarvis's transport and cannot be caught at all. That is why `ops.cost_report` marks its
figures a FLOOR unconditionally rather than guessing whether any escaped.

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
import os
from pathlib import Path
from typing import Any, Callable

from . import claude_cli
from .central_store import CentralStore

log = logging.getLogger(__name__)

#: The kind recorded for a `claude` process a WORKER spawned beneath itself — an eval
#: suite, a script, anything one of its own tool calls ran. It is stored in the same
#: table as the OS's kinds and reported as a SEPARATE CLASS, because it is a different
#: shape of spend: `neo_answer` is Jarvis thinking on the work order's behalf, this is
#: the work order's own work one process further down the tree, and a work order that
#: ran an eval suite spends nothing like one that asked Neo four questions. Collapsing
#: the two would destroy exactly the distinction the report exists to show (issue #103).
WORKER_SUBPROCESS = "worker_subprocess"

#: Kinds that belong to the WORKER rather than to the OS. A set rather than one name so
#: a future descendant kind joins the class without the report having to learn about it.
SUBPROCESS_KINDS = frozenset({WORKER_SUBPROCESS})

#: What kind of OS work a call was, as stored in `agent_calls.kind`. Open by design —
#: an unknown kind records fine and shows up in the report under its own name — but the
#: ones the OS emits today are named here so a reader of the schema knows what to expect,
#: and so the UI can put a word to each.
KIND_LABELS = {
    "neo_answer": "Neo answering",
    "panel_seat": "panel seat",
    "digest": "dashboard digest",
    WORKER_SUBPROCESS: "worker subprocess",
}

#: Where accounting rows go when that must differ from `$JARVIS_HOME`. Set at dispatch
#: beside `JARVIS_WO_ID`, and read HERE ONLY: it opens the usage-row write path and
#: nothing else — no other central-store write, no notification path — so it cannot
#: become a second route by which a sandboxed process reaches live state.
#:
#: It exists because the two homes genuinely diverge in the one case that motivated all
#: of this. The repo-root test-isolation gate redirects `JARVIS_HOME` for every suite,
#: `evals/` included — but the LLM-graded evals are opt-in precisely BECAUSE they call
#: the real model and bill real tokens, and that gate already carves them out for the
#: `claude` binary itself for the same reason. Without this,
#: `JARVIS_EVALS_LLM=1 uv run pytest evals/llm` — the command in issue #103 — spends
#: real money into a tmp directory deleted at teardown. `testing.gate_environment`
#: redirects this variable too, and lifts the redirect only when the run is BOTH an LLM
#: eval and inside a work order, so an ad-hoc eval run stays fully sandboxed.
SPEND_HOME_ENV = "JARVIS_SPEND_HOME"


def spend_db_path() -> Path | None:
    """The `os.db` accounting rows go to, or None to use the ambient `$JARVIS_HOME`.

    None rather than a computed default on purpose: `CentralStore(None)` already resolves
    `paths.central_db_path()`, and going through it keeps the ordinary case — every OS
    call the daemon makes — on exactly the path it has always taken.
    """
    home = os.environ.get(SPEND_HOME_ENV, "").strip()
    return Path(home).expanduser() / "os.db" if home else None


def describe(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.replace("_", " "))


def is_subprocess(kind: str) -> bool:
    """Does this `agent_calls.kind` belong to the worker rather than to the OS?"""
    return kind in SUBPROCESS_KINDS


def record(kind: str, *, usage: Any = None, project: str = "", wo_id: str = "",
           label: str = "", model: str = "", question_id: int | None = None,
           ok: bool = True, session_id: str = "",
           store: CentralStore | None = None) -> int | None:
    """Persist one OS-side Claude call. Returns the row id, or None if nothing was written.

    `usage` takes either a `claude_cli.derive_turn_usage` envelope or the
    `HeadlessResult` it came on, because both are what a call site has to hand.

    A call with no work order (`wo_id=""`) is still recorded: it is OS overhead that
    belongs in the fleet total even though no single work order caused it.
    """
    if isinstance(usage, claude_cli.HeadlessResult):
        model = model or usage.model
        # The session the CLI minted for this call, taken here because this is the last
        # place it exists: a one-shot `claude -p` returns it once and Jarvis keeps no
        # other handle on it. It is what lets an OS call be opened up per API call the
        # way a worker turn is — most are a single call, but a TOOLED one loops just as
        # a worker does, and that is the one worth being able to expand.
        session_id = session_id or usage.session_id
        usage = usage.usage
    if usage is not None and not isinstance(usage, dict):
        usage = None
    own = store is None
    try:
        # `spend_db_path()` is None in every ordinary case, which is the daemon writing
        # its own calls to `$JARVIS_HOME`. It is set only inside a work order's process
        # tree; see `SPEND_HOME_ENV` for why that is not the same home.
        store = store or CentralStore(spend_db_path())
        return store.add_agent_call(kind, project=project, wo_id=wo_id, label=label,
                                    model=model, question_id=question_id, ok=ok,
                                    session_id=session_id, usage=usage)
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
