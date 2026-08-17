"""The bill: every token an order spent, as line items that add up.

`ops.cost_report` answers "what did this cost" with a handful of totals side by side —
the worker's, Jarvis's own, the worker's subprocesses. It is correct and it is not a
bill: the reader cannot see WHERE a token went, cannot expand a figure into the calls
that made it, and cannot check that the parts sum to the whole. Asked to read one, a
user asked four questions this module exists to answer (wo-4576667e): why is the OS's
half all called "jarvis", what is the re-write tax, whose tokens are in the headline,
and where is the per-actor token count.

## One set of leaves, folded two ways

Everything here is built from a flat list of `Item`s — the indivisible facts of the
bill, each one a token count with a model, a price, an owner and a turn. Nothing else
is measured. The two views the UI renders are two FOLDS of that one list:

    by actor  worker → its turns;  jarvis → Neo, the panel's seats, digests;
              subprocesses → the programs that ran them
    by turn   turn 1 → what the worker, Jarvis and the subprocesses spent on it;
              turn 2 → …; and one line for anything outside any turn

Because both are folds of the same leaves, "add up the turns and you get the order" is
true BY CONSTRUCTION rather than by a second calculation that could disagree with the
first. `reconcile` proves it on every payload anyway, and the result rides along in
`checks` so the page can say so out loud: an accounting nobody can check is a claim.

## Where each leaf comes from

* THE WORKER's turns: `wo_turns.usage_json`, the `claude` CLI's own per-turn accounting
  (`claude_cli.derive_turn_usage`). Exact, and — since the version-2 reading — equal to
  the token for what the session transcript says the whole conversation cost.
* THE WORKER's unrecorded turns: the transcript total MINUS the recorded turns, when a
  work order has both and they disagree. That difference is precisely the turns whose
  result JSON is gone, and leaving it out is how a bill quietly loses a third of itself.
* JARVIS's own calls and the worker's SUBPROCESSES: `agent_calls`, recorded as each call
  returned (`agent_usage`) because nothing can recover them afterwards.

## Two currencies, never blended

`exact_usd` is the CLI's own figure for the lines that have one; `list_usd` re-prices
the same tokens at Anthropic list prices so that every line — including the ones with no
exact figure — can be added into one number. They are carried side by side the whole way
up, and `exact_missing` counts the leaves that could not contribute to the first, so a
partial exact total is never read as a complete one. TOKENS are the figure that always
reconciles; dollars are derived and labelled. (kn-e6bb1166's ruling, held to.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import db, usage as usage_mod

#: How many `agent_calls` rows one order's bill will read. Far above any real order (a
#: heavy one runs to dozens), and counted rather than assumed: a truncated bill would
#: understate exactly the expensive order someone is investigating, so `checks` says so.
CALL_LIMIT = 20_000

#: The actor a line belongs to. Three, because they are three different KINDS of spend
#: and the user's first question about the old line was why the OS's half looked like
#: one thing: the worker's own conversation, what Jarvis spent thinking about the order
#: while the worker slept, and what the worker's own tool calls spawned beneath it.
WORKER, JARVIS, SUBPROC = "worker", "jarvis", "subprocesses"

ACTOR_LABELS = {
    WORKER: "the worker's own session",
    JARVIS: "what Jarvis spent on this order",
    SUBPROC: "claude processes the worker spawned itself",
}
ACTOR_NOTES = {
    WORKER: "the agent's conversation — every turn it ran, subagents included",
    JARVIS: "one-shot calls the OS made because of this order: Neo answering its "
            "questions, each seat of a panel that deliberated, digests written for "
            "the dashboard",
    SUBPROC: "an eval suite, a script, a nested harness — its tokens are this order's, "
             "recorded as each call returned",
}

#: The line for spend that belongs to the order but to no turn of it. Its own line
#: rather than folded into the nearest turn (Neo, question 121): the turns view claims
#: to be exhaustive, so anything it cannot place has to be visible, not absorbed.
NO_TURN = "outside any turn"
NO_TURN_NOTE = ("spend that belongs to this order but to none of its turns — a call "
                "made before the first turn started, or a conversation with no turns "
                "on record at all")


@dataclass
class Item:
    """One indivisible charge: some tokens, on a model, for an actor, in a turn.

    `path` is where it hangs in the by-actor view — ("jarvis", "panel seat", "blast")
    puts one seat of the panel under the panel under Jarvis. `turn` is its seq in the
    by-turn view, or None for the `NO_TURN` line. Everything else on a bill is a sum of
    these; nothing is measured anywhere else.

    `inner` is the same path with the turn taken out of it, for the by-turn view: a
    worker item hangs under `worker → turn 3 → the model` when the tree is grouped by
    actor and under `turn 3 → worker → the model` when it is grouped by turn. Two
    orderings of one fact, so that neither view has to invent a segment.
    """

    path: tuple[str, ...]
    inner: tuple[str, ...] = ()
    turn: int | None = None
    model: str = ""
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    cache_1h: int = 0
    cache_5m: int = 0
    #: The CLI's own dollar figure, where the source carried one. None is not zero: it
    #: means this line can only be priced at list, and `exact_missing` will say so.
    exact_usd: float | None = None
    #: How many countable acts this stands for, and what to call one. A worker line
    #: counts TURNS and an OS line counts CALLS; a line that mixes them counts neither,
    #: and says so by carrying no unit at all rather than picking the wrong word.
    calls: int = 1
    unit: str = "call"
    note: str = ""
    #: Which reading of a result envelope produced it (`claude_cli.USAGE_SCHEMA_VERSION`).
    #: Surfaced so a re-derived order is distinguishable from a pre-fix one, which is
    #: the one thing Neo added to the ruling on question 121.
    usage_v: int | None = None


def _zero_tokens() -> dict[str, int]:
    return {c: 0 for c in usage_mod.TOKEN_CLASSES}


def _line(key: str, label: str, note: str = "") -> dict[str, Any]:
    """An empty line item. Everything on it accumulates as items are folded in."""
    return {
        "key": key, "label": label, "note": note,
        "calls": 0,
        "tokens": {**_zero_tokens(), "cache_1h": 0, "cache_5m": 0,
                   "billed_input": 0, "cached_input": 0, "total": 0},
        "cost": {"list_usd": 0.0, "exact_usd": 0.0, "by_class": _zero_class_costs()},
        "exact_missing": 0,
        "unit": None,
        "models": [],
        "usage_versions": [],
        "children": [],
    }


def _zero_class_costs() -> dict[str, float]:
    return {c: 0.0 for c in usage_mod.TOKEN_CLASSES}


def _add_item(line: dict[str, Any], item: Item) -> None:
    """Charge one item to one line — the only place a bill's numbers ever grow."""
    tokens = line["tokens"]
    for cls in usage_mod.TOKEN_CLASSES:
        tokens[cls] += getattr(item, cls)
    tokens["cache_1h"] += item.cache_1h
    tokens["cache_5m"] += item.cache_5m
    tokens["billed_input"] = tokens["input"] + tokens["cache_write"] + tokens["cache_read"]
    tokens["cached_input"] = tokens["cache_write"] + tokens["cache_read"]
    tokens["total"] = tokens["billed_input"] + tokens["output"]
    line["calls"] += item.calls
    if item.calls:
        line["unit"] = item.unit if line["unit"] in (None, item.unit) else ""
    # Priced PER ITEM, not from the folded totals: the write rate depends on a TTL split
    # that is exact for one item and only a ratio once items on different models and
    # different TTLs are added together.
    classes = usage_mod.class_costs(
        item.model or "unknown", input=item.input, cache_write=item.cache_write,
        cache_read=item.cache_read, output=item.output,
        cache_1h=item.cache_1h, cache_5m=item.cache_5m)
    for cls, value in classes.items():
        line["cost"]["by_class"][cls] += value
    line["cost"]["list_usd"] += sum(classes.values())
    if item.exact_usd is None:
        line["exact_missing"] += 1
    else:
        line["cost"]["exact_usd"] += item.exact_usd
    if item.model and item.model not in line["models"]:
        line["models"].append(item.model)
    if item.usage_v is not None and item.usage_v not in line["usage_versions"]:
        line["usage_versions"].append(item.usage_v)


def _fold(items: Sequence[Item], key: Any, label: Any, note: Any = None,
          depth: int | None = None) -> list[dict[str, Any]]:
    """Group items into a tree of lines, one level per element of `key(item)`.

    `key` returns the path of an item as a tuple; every prefix of it becomes a line, and
    every item is charged to EVERY line on its path — which is what makes a parent the
    exact sum of its children rather than a separately computed figure that can drift.
    """
    roots: list[dict[str, Any]] = []
    index: dict[tuple, dict[str, Any]] = {}
    for item in items:
        path = tuple(key(item))
        if depth is not None:
            path = path[:depth]
        for i in range(len(path)):
            prefix = path[: i + 1]
            line = index.get(prefix)
            if line is None:
                line = index[prefix] = _line(
                    "/".join(str(p) for p in prefix), label(prefix),
                    (note(prefix) if note else "") or "")
                if i == 0:
                    roots.append(line)
                else:
                    index[path[:i]]["children"].append(line)
            _add_item(line, item)
    return roots


def _total_line(items: Sequence[Item], label: str) -> dict[str, Any]:
    line = _line("total", label)
    for item in items:
        _add_item(line, item)
    return line


# -- gathering the leaves -----------------------------------------------------------


def _turn_items(turn_rows: Sequence[dict[str, Any]],
                session: Any) -> tuple[list[Item], list[str]]:
    """The worker's own spend, one item per (turn, model), plus what the turns missed.

    THE SECOND HALF IS THE POINT. A work order whose result JSONs have been pruned has
    turns on record with no usage on them; the session transcript still knows what the
    whole conversation cost. The difference between the transcript and the turns that
    ARE recorded is exactly what those turns spent, so it is charged as one item rather
    than dropped — dropping it is how a bill loses a third of itself and still looks
    tidy. Clamped at zero per class: a turn running right now is in the transcript and
    not yet in the recorded rows, and a negative charge would be a fiction either way.
    """
    items: list[Item] = []
    notes: list[str] = []
    recorded = _zero_tokens()
    for row in turn_rows:
        if not row.get("recorded"):
            continue
        models = row.get("by_model") or [{
            "model": row.get("model") or "",
            **{c: row.get(c) or 0 for c in usage_mod.TOKEN_CLASSES},
            "cost_usd": row.get("cost_usd"),
        }]
        # The turn's exact dollar figure belongs to the turn, not to each model in it,
        # so it is charged once — to the dearest model line — while the token counts
        # split. Splitting the dollars per model would invent a precision the CLI's
        # own per-model `costUSD` may not agree with once rounded.
        for i, m in enumerate(models):
            for cls in usage_mod.TOKEN_CLASSES:
                recorded[cls] += m.get(cls) or 0
            first = i == 0
            name = m.get("model") or "unknown"
            # A level per model ONLY when the turn used more than one. A turn that ran
            # on a single model would otherwise get a child that repeats it and says
            # nothing; the model is on the line either way (`models`).
            leaf: tuple[str, ...] = (name,) if len(models) > 1 else ()
            items.append(Item(
                path=(WORKER, f"turn {row['seq']}") + leaf,
                inner=(WORKER,) + leaf,
                turn=row["seq"], model=m.get("model") or "",
                input=m.get("input") or 0, cache_write=m.get("cache_write") or 0,
                cache_read=m.get("cache_read") or 0, output=m.get("output") or 0,
                # The TTL split is reported once per turn, for part of it; it rides on
                # the first model line so its ratio prices that turn's writes.
                cache_1h=(row.get("cache_1h") or 0) if first else 0,
                cache_5m=(row.get("cache_5m") or 0) if first else 0,
                exact_usd=(m.get("cost_usd") if m.get("cost_usd") is not None
                           else (row.get("cost_usd") if first else None)),
                # ONE, not the turn's API-call count: the countable act on a worker
                # line is the turn, so the actor line reads "6 turns" rather than "6
                # calls" — which is a different number and a different claim.
                calls=1 if first else 0, unit="turn",
                usage_v=row.get("usage_v") or 1,
                note=f"{row['kind']} turn",
            ))
    if not session.found:
        return items, notes
    whole = session.total
    gap = {
        "input": max(0, whole.input - recorded["input"]),
        "cache_write": max(0, whole.cache_write - recorded["cache_write"]),
        "cache_read": max(0, whole.cache_read - recorded["cache_read"]),
        "output": max(0, whole.output - recorded["output"]),
    }
    if any(gap.values()):
        # WHY a turn is missing from the recorded set decides what to call this line. A
        # turn running RIGHT NOW has not written its result JSON yet and will settle
        # into its own line within the minute; a settled turn with no usage will never
        # have one. Calling both "gone" would read as data loss on every live page.
        live = [r for r in turn_rows
                if not r.get("recorded") and r.get("state") == "running"]
        unrecorded = [r for r in turn_rows if not r.get("recorded")]
        if not turn_rows:
            label = "the conversation, from its transcript"
            note = ("no turns on record at all — this session predates turn capture, "
                    "or was never Jarvis-driven")
        elif live and len(live) == len(unrecorded):
            label = f"turn{'s' if len(live) != 1 else ''} still running"
            note = ("a turn writes its result JSON when it ends, so its spend is the "
                    "transcript's for now and moves onto its own turn line once it "
                    "settles")
        else:
            label = "turns with no result JSON left"
            note = ("estimated from Claude Code's session transcript, at list prices — "
                    "the CLI's own figure for these turns is gone")
            notes.append(
                "Some of this work order's turns no longer have the result JSON the "
                "CLI wrote, so their spend is the difference between the transcript "
                "and the turns that do — an estimate, on its own line.")
        items.append(Item(
            path=(WORKER, label), inner=(WORKER, label), turn=None,
            model=next(iter(whole.cost_by_model), "") or "",
            cache_1h=whole.cache_1h, cache_5m=whole.cache_5m,
            calls=len(unrecorded) or 1, unit="turn", note=note,
            **gap))
    return items, notes


def _call_items(rows: Sequence[dict[str, Any]],
                turn_at: Any) -> list[Item]:
    """One item per `agent_calls` row — Jarvis's own calls and the worker's subprocesses.

    The path is what the user asked to be able to expand: actor, then the KIND of work
    (Neo answering, a panel's seats, a digest), then the individual call — which for a
    panel is its seat, so each persona is its own line and the question "which seat is
    dear" has an answer. Subprocess rows are grouped by the program that ran them
    instead: one `pytest evals/llm` is forty calls, and forty rows saying `pytest` is
    not a breakdown.
    """
    from . import agent_usage

    items: list[Item] = []
    for row in rows:
        kind = row.get("kind") or "other"
        described = agent_usage.describe(kind)
        label = row.get("label") or ""
        envelope = db.from_json(row.get("usage_json"), {}) or {}
        if agent_usage.is_subprocess(kind):
            path: tuple[str, ...] = (SUBPROC, label or described)
        else:
            # A third level only when it SAYS something the kind did not. A panel's
            # rows are labelled with the seat, so each persona gets its own line (the
            # standing ruling: one row per seat, never one per question). Neo's answers
            # are labelled with nothing useful, so they are split by the question they
            # answered instead — the countable act on that line. A digest is labelled
            # with neither and gets no third level rather than one repeating the kind.
            leaf = label
            if row.get("question_id") and label in ("", "question", described):
                leaf = f"question #{row['question_id']}"
            elif leaf == described:
                leaf = ""
            path = (JARVIS, described) + ((leaf,) if leaf else ())
        note = ""
        if row.get("question_id"):
            note = f"question #{row['question_id']}"
        if not row.get("ok"):
            note = (note + " · " if note else "") + "the call failed (and was paid for)"
        items.append(Item(
            path=path, inner=path, turn=turn_at(row.get("ts") or 0),
            model=row.get("model") or "",
            input=row.get("input") or 0, cache_write=row.get("cache_write") or 0,
            cache_read=row.get("cache_read") or 0, output=row.get("output") or 0,
            cache_1h=envelope.get("cache_1h") or 0,
            cache_5m=envelope.get("cache_5m") or 0,
            exact_usd=row.get("cost_usd"), calls=1, note=note,
            usage_v=envelope.get("usage_v"),
        ))
    return items


def _turn_locator(turn_rows: Sequence[dict[str, Any]]) -> Any:
    """Which turn a timestamp belongs to: the last one that had STARTED by then.

    Ruled on question 121. It puts a Neo answer on the turn that asked the question —
    the worker asks and ends its turn, so the answer arrives while no turn is running —
    and a subprocess call on the turn that ran it. Anything earlier than the first turn
    has no turn to belong to and lands on the `NO_TURN` line, in the open.
    """
    starts = sorted((r.get("started_at") or 0, r["seq"]) for r in turn_rows)

    def locate(ts: float) -> int | None:
        found = None
        for started, seq in starts:
            if started <= ts:
                found = seq
            else:
                break
        return found

    return locate


# -- assembling one order's bill ------------------------------------------------------


def _turn_label(rows: Sequence[dict[str, Any]]) -> Any:
    by_seq = {r["seq"]: r for r in rows}

    def label(seq: int | None) -> str:
        if seq is None:
            return NO_TURN
        row = by_seq.get(seq) or {}
        return f"turn {seq}" + (f" · {row['kind']}" if row.get("kind") else "")

    return label


def compose(items: Sequence[Item], turn_rows: Sequence[dict[str, Any]],
            label: str) -> dict[str, Any]:
    """The two folds and the total, over one order's items. See the module docstring."""
    turn_label = _turn_label(turn_rows)
    by_turn = _fold(
        items,
        key=lambda i: (i.turn if i.turn is not None else NO_TURN,) + i.inner,
        label=lambda p: (turn_label(p[0] if p[0] != NO_TURN else None) if len(p) == 1
                         else _actor_label(p[-1])),
        note=lambda p: (NO_TURN_NOTE if p == (NO_TURN,) else ""),
    )
    # Turns in order, with `NO_TURN` last: it is a residue, not a step in the story.
    by_turn.sort(key=lambda line: (line["key"] == NO_TURN,
                                   _seq_of(line["key"])))
    return {
        "total": _total_line(items, label),
        "actors": _fold(items, key=lambda i: i.path,
                        label=lambda p: _actor_label(p[-1]),
                        note=lambda p: ACTOR_NOTES.get(p[-1], "") if len(p) == 1 else ""),
        "turns": by_turn,
    }


def _seq_of(key: str) -> float:
    head = key.split("/")[0]
    try:
        return float(head)
    except ValueError:
        return float("inf")


def _actor_label(segment: Any) -> str:
    return ACTOR_LABELS.get(str(segment), str(segment))


def build(target: str, project: str | None = None) -> dict[str, Any]:
    """The bill for one order, work or feature, resolved by id.

    The feature order is tried FIRST, for the reason `ops._cost_for_target` gives: the
    two id spaces cannot collide, but resolving the work order first would answer
    `bill fo-…` with the planner's own spend, which is the one figure nobody wants.
    """
    from . import ops

    try:
        name, path, fo = ops.find_feature_order(target, project)
    except ops.OpsError:
        name, wo_path, wo = ops.find_work_order(target, project)
        return for_work_order(name, wo_path, wo)
    return for_feature_order(name, path, fo)


def for_work_order(project: str, path: Path, wo: dict[str, Any],
                   index: dict[str, list[Path]] | None = None) -> dict[str, Any]:
    """One work order's bill: its turns, Jarvis's calls on it, its own subprocesses."""
    from . import ops
    from .central_store import CentralStore
    from .project_store import ProjectStore

    store = ProjectStore(path)
    try:
        turn_rows = ops._turn_rows(store, wo["id"])
    finally:
        store.close()
    central = CentralStore()
    try:
        calls = central.agent_calls(wo_id=wo["id"], limit=CALL_LIMIT)
    finally:
        central.close()
    session = usage_mod.read_session(wo.get("session_id") or "", index=index)
    worker_items, notes = _turn_items(turn_rows, session)
    items = [*worker_items, *_call_items(calls, _turn_locator(turn_rows))]
    payload = {
        "kind": "work_order",
        "scope": wo["id"], "id": wo["id"], "title": wo["title"],
        "status": wo["status"], "project": project,
        "session_found": session.found,
        "session_id": wo.get("session_id") or "",
        **compose(items, turn_rows, wo["id"]),
        "turn_rows": turn_rows,
        "notes": notes,
        **_worker_extras(session),
    }
    if len(calls) >= CALL_LIMIT:
        payload["notes"].append(
            f"Only the most recent {CALL_LIMIT} recorded calls are on this bill.")
    _annotate_turns(payload, turn_rows)
    payload["checks"] = reconcile(payload)
    payload["floor_reason"] = ops.COST_FLOOR_NOTE
    return payload


def for_feature_order(project: str, path: Path, fo: dict[str, Any],
                      index: dict[str, list[Path]] | None = None) -> dict[str, Any]:
    """A feature order's bill: its planner and every child, each a whole bill itself.

    The hierarchy the user asked for goes all the way down — the feature's total, each
    order under it, and each order's own turns — because a feature order is where "what
    did this cost" is asked and the answer is never one session's.
    """
    from . import ops
    from .project_store import ProjectStore

    store = ProjectStore(path)
    try:
        children: list[dict[str, Any]] = []
        planner_id = fo.get("plan_wo_id")
        if planner_id:
            try:
                children.append(store.get_work_order(planner_id))
            except KeyError:
                pass
        children.extend(store.feature_children(fo["id"]))
    finally:
        store.close()
    # ONE pass over Claude Code's transcript tree for the whole feature, not one per
    # child: `index_sessions` walks every project directory there is, and a feature with
    # eight children would otherwise walk it eight times to answer one page.
    index = index if index is not None else usage_mod.index_sessions()
    orders = [for_work_order(project, path, child, index=index) for child in children]
    items: list[Item] = []
    for order, child in zip(orders, children):
        for line in order["actors"]:
            # Re-charged as one item per leaf of the child's own tree, so the feature's
            # by-actor view is the same fold over the same facts one level up.
            items.extend(_relabel(line, child["id"]))
    payload = {
        "kind": "feature_order",
        "scope": fo["id"], "id": fo["id"], "title": fo["title"],
        "status": fo["status"], "project": project,
        "total": _total_line(items, fo["id"]),
        "actors": _fold(items, key=lambda i: i.path,
                        label=lambda p: _actor_label(p[-1]),
                        note=lambda p: ACTOR_NOTES.get(p[-1], "") if len(p) == 1 else ""),
        "turns": [],
        "orders": orders,
        "notes": [],
        "checks": {},
    }
    payload["checks"] = reconcile(payload)
    payload["floor_reason"] = ops.COST_FLOOR_NOTE
    return payload


def _relabel(line: dict[str, Any], wo_id: str, prefix: tuple[str, ...] = ()
             ) -> list[Item]:
    """Flatten a built line tree back into items, for folding one level up.

    A feature order's bill is its children's leaves re-folded, not their totals added:
    adding totals would give the feature a correct headline and no way to expand it,
    which is the thing this whole module exists to fix.
    """
    # The order id goes in as the SECOND segment, so a feature's by-actor view reads
    # "the workers → this order → its turns" rather than merging every child's "turn 1"
    # into one line, which is what a plain re-fold of the same keys would do.
    segment = line["key"].split("/")[-1]
    path = (*prefix, segment) if prefix else (segment, wo_id)
    children = line.get("children") or []
    if children:
        out: list[Item] = []
        for child in children:
            out.extend(_relabel(child, wo_id, path))
        return out
    tokens, cost = line["tokens"], line["cost"]
    return [Item(
        path=path, inner=path, turn=None, model=(line["models"] or [""])[0],
        input=tokens["input"], cache_write=tokens["cache_write"],
        cache_read=tokens["cache_read"], output=tokens["output"],
        cache_1h=tokens["cache_1h"], cache_5m=tokens["cache_5m"],
        exact_usd=None if line["exact_missing"] else cost["exact_usd"],
        calls=line["calls"], note=line["note"],
    )]


def _worker_extras(session: Any) -> dict[str, Any]:
    """The two facts about a worker's session that are not line items.

    Neither is an addend and both are constantly mistaken for one. SUBAGENTS are a
    SHARE of the worker's own turns (their tokens are already inside `modelUsage`), so
    they are reported as "of which", never added. The RE-WRITE TAX is a share too — the
    part of the cache-write charge that paid to re-send context a warm cache would have
    served — and the user's second question was what it even is, so the sentence that
    says it travels with the number.
    """
    total = session.total
    return {
        "subagents": {
            "count": session.subagent_count,
            "list_usd": session.subagents.list_cost_usd,
            "output": session.subagents.output,
            "billed_input": session.subagents.billed_input,
        },
        "rewrite": {
            "tokens": total.rewrite_excess,
            "list_usd": total.rewrite_cost_usd,
            "boundaries": total.resume_boundaries,
        },
        "context_peak": total.context_peak,
    }


def _annotate_turns(payload: dict[str, Any], turn_rows: Sequence[dict[str, Any]]) -> None:
    """Hang each turn's timings and context on its line, for the per-turn header."""
    by_seq = {str(r["seq"]): r for r in turn_rows}
    for line in payload["turns"]:
        row = by_seq.get(line["key"])
        if row:
            line["turn"] = row


def reconcile(payload: dict[str, Any], tolerance: float = 1e-6) -> dict[str, Any]:
    """Prove the bill adds up, and say so in the payload.

    Three claims, each checked rather than asserted: the by-turn view sums to the total,
    the by-actor view sums to the total, and every parent line is the sum of its
    children all the way down. They cannot fail while both views are folds of one item
    list — which is exactly why this is cheap to run on every payload, and why a failure
    here means the shape changed under someone, not that a number was mistyped.
    """
    problems: list[str] = []
    total = payload["total"]
    views = {name: payload.get(name) or [] for name in ("turns", "actors")}
    # A feature order's third view: its children, each a whole bill. Compared by their
    # own total lines, which is the only way the hierarchy the user asked for can be
    # trusted — a feature whose headline is not its children's sum is telling two
    # different stories on one page.
    if payload.get("orders"):
        views["orders"] = [order["total"] for order in payload["orders"]]
    for view, lines in views.items():
        if not lines:
            continue
        for cls in (*usage_mod.TOKEN_CLASSES, "total"):
            summed = sum(line["tokens"][cls] for line in lines)
            if summed != total["tokens"][cls]:
                problems.append(
                    f"{view}: {cls} sums to {summed}, order total is "
                    f"{total['tokens'][cls]}")
        summed_cost = sum(line["cost"]["list_usd"] for line in lines)
        if abs(summed_cost - total["cost"]["list_usd"]) > tolerance:
            problems.append(f"{view}: list cost sums to {summed_cost}, order total is "
                            f"{total['cost']['list_usd']}")
        for line in lines:
            problems.extend(_check_children(line))
    return {"balanced": not problems, "problems": problems}


def _check_children(line: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    children = line.get("children") or []
    if children:
        for cls in usage_mod.TOKEN_CLASSES:
            summed = sum(c["tokens"][cls] for c in children)
            if summed != line["tokens"][cls]:
                problems.append(f"{line['key']}: children sum {cls} to {summed}, "
                                f"the line says {line['tokens'][cls]}")
        for child in children:
            problems.extend(_check_children(child))
    return problems
