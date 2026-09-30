"""The sealed autopsy: one order's anatomy frozen onto its row, so it outlives the files.

`inspection.read_session` answers "where did this order's time and context go" by walking
the transcripts Claude Code wrote — the session file, the subagent files beside it, the
result envelopes. Claude Code prunes all of them on its own schedule, so an order
inspected long after it settled reports a clock that got SHORTER as its evidence aged,
which is `bill`'s kn-3629fa87 one level up. This module freezes the reading at settle.

## The seal is not the rendering, and that is the trap this module exists to avoid

`Anatomy.as_dict` is a RENDER: `Turn.as_dict` emits `api_calls: len(self.calls)` and a
folded `usage`, and carries neither `Turn.calls` nor `active_ended`. Rehydrate from it and
`observed` goes False, `usage` empty, `context_peak` 0 — `supervisor` would print "NO API
CALL WAS EVER MADE — it cost nothing." for a turn that made 32 (issue #227, recreated
inside the persistence meant to fix it). So `to_seal` and `from_seal` are a dedicated
round-trippable pair over the dataclass FIELDS, and `as_dict` stays the SINGLE render
contract, computed FROM the rehydrated object.

## Seal only what expires

HOLDS ARE NOT SEALED: `holds.held` reads the OS's own per-project database, which never
expires, and sealing would freeze an episode that was OPEN at seal time. `from_seal` takes
`spans=` exactly as `read_session` does and re-runs `inspection._attach_holds`.
`wo_turns.usage_json` and `wo_turns.context_json` are not sealed either: already durable.
The module legends (`hold_causes`, `PARTS`, `subagent_depth_read`, `param_caps`) are
re-derived at render, so a raised cap is reflected on an old seal. And span `params` are
not sealed AT EITHER LEVEL here: tool parameters are what the `full` level buys, which is
§6 of docs/specs/2026-09-27-order-autopsy-durability.md and not this module's.

## Nothing is ever silently dropped

Every cap folds rather than truncates, and a fold carries NUMBERS that reconcile against
the uncapped reading: folded span seconds per tool name (so they add back to both
`Turn.tools` and `Turn.blocked`), and folded calls as per-model sums with a message count,
a context peak, a TTL split and a cache-write total — the four figures `Turn.usage`,
`Turn.context_peak`, `Anatomy.cache_ttl` and `Anatomy.rewrite_excess` would otherwise
misreport. `from_seal` never invents a synthetic call to stand in for a folded one: a
fabricated row would read as an API call that happened.

## It ships dark

`records_autopsy` returns False for everybody. §5 of the spec above replaces its body with
the real level check; until it lands this writer seals nothing fleet-wide, and that is the
user's ruling rather than a trade-off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from . import db, inspection
from . import usage as usage_mod
from .bill import TURN_CALL_LIMIT  # ONE definition of "how many calls a seal keeps"

#: Schema version of a SEALED payload. Bump it when a field is added that a reader of an
#: old seal would otherwise never get; `_upgrade_seal` re-derives such a seal ONCE, from
#: the stored payload alone.
#:
#: 1 — the original payload.
PAYLOAD_VERSION = 1

#: How many tool spans one TURN carries into a seal, the dearest by seconds. A long turn
#: can run to thousands of spans and a seal is stored, not derived on demand; the rest are
#: folded into `spans_folded` with their seconds per tool name, never dropped.
TURN_SPAN_LIMIT = 500

#: How many subagents one TURN carries. Depth was already bounded by
#: `inspection.SUBAGENT_DEPTH_READ`; breadth was not.
SUBAGENT_LIMIT = 20

#: The whole payload's ceiling, in bytes of JSON. Reached only by a pathological order,
#: and then the DROP ORDER below applies — announced, never silent.
PAYLOAD_CEILING = 1_000_000

NORMAL, FULL = "normal", "full"
#: A seal written before `autopsy_level` existed. A THIRD state: without it "no params" is
#: ambiguous between "sealed at `normal`" and "this order ran no tools".
UNKNOWN = "unknown"

#: What the ceiling gives up, in order. The two params rungs ship present and unreachable:
#: params are only sealed at `full`, which is §6.
_DROP_ORDER = ("params", "subagent_params", "subagent_spans", "spans_over_limit")

#: Per-turn keys that hold folded NUMBERS rather than sealed objects — carried across an
#: upgrade verbatim, because the stored payload is the only place they exist.
_FOLD_KEYS = ("spans_folded", "calls_folded", "subagents_folded")


def records_autopsy(wo: dict[str, Any], cfg: Any) -> bool:
    """Whether this order's autopsy is sealed at all — §3: it ships CLOSED, §5 opens it."""
    return False


def level_of(payload: dict[str, Any]) -> str:
    """The level a stored payload was sealed at, or `UNKNOWN` for one that predates it."""
    return str(payload.get("autopsy_level") or UNKNOWN)


# -- sealing ---------------------------------------------------------------------------


def _span(span: inspection.ToolSpan) -> dict[str, Any]:
    # Boundaries only: `params` belong to §6's `full` level and are never sealed here.
    return {"name": span.name, "tool_id": span.tool_id, "started": span.started,
            "ended": span.ended, "detail": span.detail}


def _call(call: usage_mod.Call) -> dict[str, Any]:
    return {"ts": call.ts, "model": call.model, "input": call.input,
            "cache_write": call.cache_write, "cache_read": call.cache_read,
            "output": call.output, "cache_1h": call.cache_1h, "cache_5m": call.cache_5m}


def _write(write: inspection.Write) -> dict[str, Any]:
    return {"ts": write.ts, "written": write.written, "read": write.read,
            "gap": write.gap, "cause": write.cause, "model": write.model,
            "ttl": write.ttl}


def _prompt(prompt: inspection.Prompt) -> dict[str, Any]:
    return {"ts": prompt.ts, "kind": prompt.kind, "quote": prompt.quote,
            "source": prompt.source}


def _fold_spans(spans: Sequence[inspection.ToolSpan]) -> dict[str, Any]:
    """The folded spans as numbers: count, total seconds, and seconds per tool NAME.

    Per name and not per join-ness: `inspection.JOIN_TOOLS` derives the second from the
    first, so one set of numbers reconciles against `Turn.tools` AND `Turn.blocked`.
    """
    by_name: dict[str, float] = {}
    for span in spans:
        by_name[span.name] = by_name.get(span.name, 0.0) + span.seconds
    return {"count": len(spans), "seconds": sum(by_name.values()),
            "seconds_by_name": by_name}


def _merge_folds(first: dict[str, Any] | None,
                 second: dict[str, Any]) -> dict[str, Any]:
    if not first:
        return second
    by_name = dict(first["seconds_by_name"])
    for name, seconds in second["seconds_by_name"].items():
        by_name[name] = by_name.get(name, 0.0) + seconds
    return {"count": first["count"] + second["count"],
            "seconds": first["seconds"] + second["seconds"],
            "seconds_by_name": by_name}


def _fold_calls(calls: Sequence[usage_mod.Call]) -> dict[str, Any]:
    """The folded calls as the four numbers a fold would otherwise falsify.

    `usage.priced` prices per model, so the per-model sums carry a message count; the
    context peak is the MAX of the folded calls and never a sum, because a context is what
    one call carried.
    """
    by_model: dict[str, dict[str, int]] = {}
    ttl = {"cache_1h": 0, "cache_5m": 0, "unknown": 0}
    for call in calls:
        row = by_model.setdefault(call.model, {"messages": 0, "input": 0,
                                               "cache_write": 0, "cache_read": 0,
                                               "output": 0, "cache_1h": 0,
                                               "cache_5m": 0})
        row["messages"] += 1
        for field_name in ("input", "cache_write", "cache_read", "output",
                           "cache_1h", "cache_5m"):
            row[field_name] += getattr(call, field_name)
        ttl["cache_1h"] += call.cache_1h
        ttl["cache_5m"] += call.cache_5m
        ttl["unknown"] += max(0, call.cache_write - call.cache_1h - call.cache_5m)
    return {"count": len(calls), "by_model": by_model, "cache_ttl": ttl,
            "context_peak": max((c.context for c in calls), default=0),
            "cache_write": sum(c.cache_write for c in calls)}


def _turn(turn: inspection.Turn) -> dict[str, Any]:
    spans = sorted(turn.spans, key=lambda s: s.seconds, reverse=True)
    kept_spans, folded_spans = spans[:TURN_SPAN_LIMIT], spans[TURN_SPAN_LIMIT:]
    calls = sorted(turn.calls, key=lambda c: c.total_tokens, reverse=True)
    kept_calls, folded_calls = calls[:TURN_CALL_LIMIT], calls[TURN_CALL_LIMIT:]
    payload: dict[str, Any] = {
        "seq": turn.seq, "part": turn.part, "started": turn.started,
        "ended": turn.ended, "active_ended": turn.active_ended,
        "awaiting_since": turn.awaiting_since,
        "triggers": [_prompt(p) for p in turn.triggers],
        # Back in transcript order: the seal is read as a story, not as a ranking.
        "spans": [_span(s) for s in sorted(kept_spans, key=lambda s: s.started)],
        "calls": [_call(c) for c in sorted(kept_calls, key=lambda c: c.ts)],
        "subagents": [_subagent(s) for s in turn.subagents[:SUBAGENT_LIMIT]],
    }
    if folded_spans:
        payload["spans_folded"] = _fold_spans(folded_spans)
    if folded_calls:
        payload["calls_folded"] = _fold_calls(folded_calls)
    if len(turn.subagents) > SUBAGENT_LIMIT:
        payload["subagents_folded"] = {"count": len(turn.subagents) - SUBAGENT_LIMIT}
    return payload


def _subagent(sub: inspection.SubagentAnatomy) -> dict[str, Any]:
    return {"task_id": sub.task_id, "label": sub.label, "deeper": sub.deeper,
            "turns": [_turn(t) for t in sub.turns],
            "writes": [_write(w) for w in sub.writes]}


def to_seal(anatomy: inspection.Anatomy, *, level: str) -> dict[str, Any]:
    """One reading, as the payload frozen onto `work_orders.autopsy_json`.

    `level` is the CALLER'S argument and is recorded from day one: with no gate this
    child's `seal_autopsies` can only pass `normal`, and §5 is what makes `full` reachable.
    """
    payload = {
        "payload_v": PAYLOAD_VERSION,
        "autopsy_level": level,
        "session_id": anatomy.session_id,
        "found": anatomy.found,
        # The floors this reading was taken at: a seal taken at 20000 holds NO write
        # below 20000, ever, and a report that cannot say so is not reproducible.
        "write_floor": anatomy.write_floor,
        "join_floor": anatomy.join_floor,
        "unmatched_os_turns": list(anatomy.unmatched_os_turns),
        "writes": [_write(w) for w in anatomy.writes],
        "unattached_subagents": [_subagent(s) for s in anatomy.unattached_subagents],
        "turns": [_turn(t) for t in anatomy.turns],
    }
    return _fit(payload)


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Bring one payload under `PAYLOAD_CEILING`, announcing what it gave up.

    The drop order is `_DROP_ORDER` and each rung is recorded in `dropped_for_size`, the
    way `params_truncated` and `params_dropped` already announce a truncation.
    """
    dropped: list[str] = []
    for rung in _DROP_ORDER:
        if len(db.to_json(payload)) <= PAYLOAD_CEILING:
            break
        if _RUNGS[rung](payload):
            dropped.append(rung)
    if dropped:
        payload["dropped_for_size"] = dropped
    return payload


def _every_turn(payload: dict[str, Any]) -> list[dict[str, Any]]:
    turns = list(payload.get("turns") or [])
    for sub in payload.get("unattached_subagents") or []:
        turns += list(sub.get("turns") or [])
    return turns


def _subagent_turns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for turn in _every_turn(payload)
            for sub in turn.get("subagents") or []
            for t in sub.get("turns") or []]


def _drop_params(turns: Sequence[dict[str, Any]]) -> bool:
    """Rung 1 and 2: the tool parameters. Present and unreachable at `normal` — §6."""
    gone = False
    for turn in turns:
        for span in turn.get("spans") or []:
            for key in ("params", "params_truncated", "params_dropped"):
                gone = span.pop(key, None) is not None or gone
    return gone


def _fold_stored_spans(turn: dict[str, Any]) -> bool:
    """Fold every span this turn still carries into its own numbers."""
    spans = turn.get("spans") or []
    if not spans:
        return False
    fold = {"count": len(spans), "seconds": 0.0, "seconds_by_name": {}}
    for span in spans:
        seconds = max(0.0, span["ended"] - span["started"]) if (
            span["ended"] > span["started"]) else 0.0
        fold["seconds"] += seconds
        fold["seconds_by_name"][span["name"]] = (
            fold["seconds_by_name"].get(span["name"], 0.0) + seconds)
    turn["spans"] = []
    turn["spans_folded"] = _merge_folds(turn.get("spans_folded"), fold)
    return True


_RUNGS = {
    "params": lambda p: _drop_params(_every_turn(p)),
    "subagent_params": lambda p: _drop_params(_subagent_turns(p)),
    "subagent_spans": lambda p: any([_fold_stored_spans(t)
                                     for t in _subagent_turns(p)]),
    "spans_over_limit": lambda p: any([_fold_stored_spans(t) for t in _every_turn(p)]),
}


# -- rehydrating -----------------------------------------------------------------------


def _read_span(row: dict[str, Any]) -> inspection.ToolSpan:
    return inspection.ToolSpan(name=row["name"], tool_id=row["tool_id"],
                               started=row["started"], ended=row["ended"],
                               detail=row.get("detail", ""))


def _read_call(row: dict[str, Any]) -> usage_mod.Call:
    return usage_mod.Call(**{k: row[k] for k in ("ts", "model", "input", "cache_write",
                                                 "cache_read", "output", "cache_1h",
                                                 "cache_5m")})


def _read_write(row: dict[str, Any]) -> inspection.Write:
    return inspection.Write(ts=row["ts"], written=row["written"], read=row["read"],
                            gap=row["gap"], cause=row["cause"],
                            model=row.get("model", ""),
                            ttl=row.get("ttl", inspection.TTL_5M))


def _read_turn(row: dict[str, Any]) -> inspection.Turn:
    return inspection.Turn(
        seq=row["seq"], started=row["started"], ended=row["ended"],
        part=row.get("part", 1), active_ended=row.get("active_ended", 0.0),
        awaiting_since=row.get("awaiting_since", 0.0),
        triggers=[inspection.Prompt(ts=p["ts"], kind=p["kind"], quote=p["quote"],
                                    source=p.get("source", "sdk"))
                  for p in row.get("triggers") or []],
        spans=[_read_span(s) for s in row.get("spans") or []],
        # NO synthetic call for a folded one: a fabricated row would be read as an API
        # call that happened. The fold's numbers are in the payload and stay there.
        calls=[_read_call(c) for c in row.get("calls") or []],
        subagents=[_read_subagent(s) for s in row.get("subagents") or []])


def _read_subagent(row: dict[str, Any]) -> inspection.SubagentAnatomy:
    return inspection.SubagentAnatomy(
        task_id=row["task_id"], label=row.get("label", ""),
        deeper=row.get("deeper", 0),
        turns=[_read_turn(t) for t in row.get("turns") or []],
        writes=[_read_write(w) for w in row.get("writes") or []])


def from_seal(payload: dict[str, Any], *,
              spans: Sequence[inspection.Hold] = ()) -> inspection.Anatomy:
    """A stored payload back as an `Anatomy`, ready to render through `as_dict`.

    `spans` is `holds.held` for the order, passed in exactly as `read_session` takes it
    and for the same reason: the holds live in the OS's own database, which never expires,
    so they are re-attached live rather than frozen.
    """
    anatomy = inspection.Anatomy(
        session_id=payload.get("session_id", ""),
        found=bool(payload.get("found")),
        write_floor=payload.get("write_floor",
                                inspection.DEFAULT_INSPECT_REPORT_WRITE_FLOOR),
        join_floor=payload.get("join_floor",
                               inspection.DEFAULT_INSPECT_REPORT_JOIN_FLOOR),
        turns=[_read_turn(t) for t in payload.get("turns") or []],
        writes=[_read_write(w) for w in payload.get("writes") or []],
        unattached_subagents=[_read_subagent(s)
                              for s in payload.get("unattached_subagents") or []],
        unmatched_os_turns=list(payload.get("unmatched_os_turns") or []),
        holds=list(spans))
    inspection._attach_holds(anatomy.turns, anatomy.holds)
    return anatomy


# -- the seal on the row ---------------------------------------------------------------


def unseal(order: dict[str, Any]) -> dict[str, Any] | None:
    """The autopsy frozen onto this order when it settled, if there is one."""
    raw = order.get("autopsy_json")
    if not raw:
        return None
    payload = db.from_json(raw, None)
    if not isinstance(payload, dict) or "turns" not in payload:
        return None
    payload["sealed_at"] = order.get("autopsy_sealed_at")
    payload["autopsy_level"] = level_of(payload)
    return payload


def seal(project: str, path: Path, order: dict[str, Any], *,
         index: dict[str, list[Path]] | None = None,
         level: str = NORMAL) -> dict[str, Any]:
    """Read a settled order's session and freeze the reading onto the order. Idempotent.

    The argument construction is `ops.inspect_report`'s, deliberately: one definition of
    "this order's anatomy" — `inspection.read_session` and NOT `usage.read_session`, two
    different functions sharing a name.
    """
    from . import holds, ops
    from .project_store import ProjectStore

    cfg = ops.inspect_config(project)
    store = ProjectStore(path)
    try:
        session = order.get("session_id") or ""
        spans = holds.held(store, order["id"])
        anatomy = (inspection.read_session(
                       session, cfg,
                       index=index if index is not None else usage_mod.index_sessions(),
                       spans=spans, turn_starts=store.turn_starts(order["id"]))
                   if session
                   else inspection.Anatomy(session_id="", holds=list(spans),
                                           write_floor=cfg.report_write_floor,
                                           join_floor=cfg.report_join_floor))
        payload = to_seal(anatomy, level=level)
        store.seal_autopsy(order["id"], db.to_json(payload))
    finally:
        store.close()
    return payload


# -- the versioned re-seal -------------------------------------------------------------


def _upgrade_seal(project: str, path: Path, order: dict[str, Any],
                  sealed: dict[str, Any]) -> dict[str, Any] | None:
    """Re-derive a seal written before this module sealed a field it seals now.

    FROM THE STORED PAYLOAD ONLY, and that is the one place a naive copy of
    `bill._upgrade_seal` is wrong: a bill re-reads its sources, but an autopsy's sources
    are transcripts that Claude Code has probably pruned by the time a version is bumped,
    and a fresh reading of a pruned transcript would overwrite good data with `found:
    false`. A caller that DOES have a fresh reading to offer asks `adopts_fresh_reading`.

    `autopsy_sealed_at` is preserved — when the order settled is not changed by
    re-deriving its payload — and `resealed_at` records that the re-derivation happened.
    Returns None when there is nothing to upgrade or the upgrade cannot be done, and the
    old seal stands. Never raises: a seal that cannot be upgraded is still a seal.
    """
    from .project_store import ProjectStore

    if (sealed.get("payload_v") or 1) >= PAYLOAD_VERSION:
        return None
    try:
        fresh = to_seal(from_seal(sealed), level=level_of(sealed))
        # The folds are numbers no rehydration can recover, so they come across verbatim.
        for old, new in zip(sealed.get("turns") or [], fresh["turns"]):
            for key in _FOLD_KEYS:
                if key in old:
                    new[key] = old[key]
        for key in ("dropped_for_size",):
            if key in sealed:
                fresh[key] = sealed[key]
        at = order.get("autopsy_sealed_at")
        fresh["resealed_at"] = db.now()
        store = ProjectStore(path)
        try:
            store.seal_autopsy(order["id"], db.to_json(fresh), at=at)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 — an upgrade that fails must not cost the reader
        return None                                    # the autopsy they already have
    return fresh


def _counts(payload: dict[str, Any]) -> dict[str, int]:
    """What one SEALED payload holds, folded remainders included."""
    turns = payload.get("turns") or []
    spans = sum(len(t.get("spans") or []) + (t.get("spans_folded") or {}).get("count", 0)
                for t in turns)
    calls = sum(len(t.get("calls") or []) + (t.get("calls_folded") or {}).get("count", 0)
                for t in turns)
    return {"turns": len(turns), "spans": spans, "calls": calls,
            "writes": len(payload.get("writes") or [])}


def _fresh_counts(anatomy: inspection.Anatomy) -> dict[str, int]:
    return {"turns": len(anatomy.turns), "spans": len(anatomy.spans),
            "calls": sum(len(t.calls) for t in anatomy.turns),
            "writes": len(anatomy.writes)}


def adopts_fresh_reading(sealed: dict[str, Any],
                         fresh: inspection.Anatomy) -> bool:
    """May a FRESH reading replace this seal? By STRUCTURED COUNTS, at the SAME FLOORS.

    An autopsy has NO monotone quantity, which is why this is not `bill`'s "never shrink":
    a parser fix can yield fewer spans and a raised floor fewer writes. So a fresh reading
    is adopted only when it found the transcript AND its turns, spans, calls and writes are
    EACH at least the seal's, taken at the floors the seal was taken at — a fresh read at a
    higher floor showing fewer writes is a floor artefact and is refused.

    Never by rendered prose (`bill._corrects_a_reading`'s lesson): an English sentence gets
    rewritten, and a set difference over sentences made every pre-edit seal look like a
    reading that had just lost evidence.
    """
    if not fresh.found:
        return False
    if (fresh.write_floor, fresh.join_floor) != (sealed.get("write_floor"),
                                                 sealed.get("join_floor")):
        return False
    was, now = _counts(sealed), _fresh_counts(fresh)
    return all(now[key] >= was[key] for key in was)
