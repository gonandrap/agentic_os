"""The bill: every token an order spent, as line items that add up"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import db, usage as usage_mod
from .project_store import COMPACT_TURN

#: How many `agent_calls` rows one order's bill will read
CALL_LIMIT = 20_000

#: How many per-API-call rows one TURN carries into the sealed bill
TURN_CALL_LIMIT = 200

#: Schema version of a SEALED payload (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §9)
PAYLOAD_VERSION = 5

#: The actor a line belongs to (docs/superpowers/specs/2026-09-24-order-observability.md)
WORKER, JARVIS, SUBPROC = "worker", "jarvis", "subprocesses"
OBSERVE = "observability"

ACTOR_LABELS = {
    WORKER: "the worker's own session",
    JARVIS: "what Jarvis spent on this order",
    SUBPROC: "claude processes the worker spawned itself",
    OBSERVE: "what looking at this order cost",
}
ACTOR_NOTES = {
    WORKER: "the agent's conversation — every turn it ran, subagents included",
    JARVIS: "one-shot calls the OS made because of this order: Neo answering its "
            "questions, each seat of a panel that deliberated, digests written for "
            "the dashboard",
    SUBPROC: "an eval suite, a script, a nested harness — its tokens are this order's, "
             "recorded as each call returned",
    # Say the 0.00 out loud, because a reviewer will otherwise read it as a bug (§10)
    OBSERVE: "money spent LOOKING at this order, not DOING it: every `jarvis watch`, "
             "`jarvis inspect`, `jarvis wo why`, context ledger read and context ledger "
             "write, one row each. Dollars of zero beside a count and a wall clock that "
             "are not zero is the honest rendering of a mechanical path — the paths are "
             "arithmetic over files that already exist, and a measured zero is the claim, "
             "not a missing figure",
}

#: WHAT IS NOT ON THIS BILL, one sentence per actor class
ABSENT_NOTES = {
    WORKER: "The worker's own session — nothing measurable. Either it was never "
            "dispatched, or Claude Code has pruned the transcript and the result JSON of "
            "every turn is gone.",
    JARVIS: "Jarvis's own calls — nothing. This order asked Neo no questions, no panel "
            "deliberated on it, and nothing was digested for it. Every question a worker "
            "asks costs one Neo call (five when the panel sits), and they would be "
            "itemised here.",
    SUBPROC: "Claude processes the worker spawned itself — nothing recorded. An eval "
             "suite or a script run through Jarvis's transport would be itemised here.",
    OBSERVE: "What looking at this order cost — not recorded. Either it ran before the "
             "meter existed or nobody has opened a report on it, and neither of those is "
             "a figure of zero spent.",
}


#: THE SUBAGENT SIDE'S ZERO, NAMED
SUBAGENT_REWRITE_ZERO = (
    "of that, subagents contributed 0 tokens across 0 boundaries — a STRUCTURAL zero and "
    "not a small one: a subagent's written volume never exceeds its own context peak, and "
    "one continuous subagent conversation has no cache read going backwards, so the "
    "arithmetic can give nothing else. Those {count} subagent(s) still wrote {written} "
    "cache-creation tokens, itemised under the turn each ran in.")


def absent_notes(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """The classes with NO line on this payload, each with the sentence that says so"""
    present = {line.get("key") for line in payload.get("actors") or []}
    return [(key, text) for key, text in ABSENT_NOTES.items() if key not in present]

#: The line for spend that belongs to the order but to no turn of it
NO_TURN = "outside any turn"
NO_TURN_NOTE = ("spend that belongs to this order but to none of its turns — a call "
                "made before the first turn started, or a conversation with no turns "
                "on record at all")

#: How `payload["own"]` is labelled, on the page and in the terminal both
OWN_LABEL = "what Jarvis spent on this order itself, not on any order below it"


@dataclass
class Item:
    """One indivisible charge: some tokens, on a model, for an actor, in a turn"""

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
    #: The CLI's own dollar figure, where the source carried one
    exact_usd: float | None = None
    #: How many countable acts this stands for, and what to call one
    calls: int = 1
    unit: str = "call"
    note: str = ""
    #: Which reading of a result envelope produced it (`claude_cli.USAGE_SCHEMA_VERSION`)
    usage_v: int | None = None


#: "no item has proposed a note yet", which is not the same as "the items disagree"
_UNSET = object()


#: What the `tokens` figure on every line of this bill counts
TOKENS_MEAN = ("every token the API billed for, of all four classes added together — "
               "fresh input + cache write + cache read + output. It is a sum over the "
               "API calls underneath, not a context size: a turn of 11 calls each "
               "re-reading a 55k conversation reads 517k, and the biggest single call "
               "is what 'context peak' reports")


def write_rate_of(line: dict[str, Any]) -> float:
    """The multiple this line's cache-write tokens were charged at"""
    rate = line.get("cost", {}).get("write_rate")
    if isinstance(rate, (int, float)) and rate:
        return float(rate)
    tokens = line["tokens"]
    return usage_mod.write_rate(tokens["cache_write"], tokens.get("cache_1h", 0),
                                tokens.get("cache_5m", 0))


def rate_note(line: dict[str, Any], fmt: Any = None) -> str:
    """Why this line's cache writes cost the multiple they cost"""
    fmt = fmt or (lambda n: f"{n:,}")
    tokens = line["tokens"]
    written = tokens["cache_write"]
    hour, five = tokens["cache_1h"], tokens["cache_5m"]
    known = hour + five
    if not written:
        return "nothing was written to the cache on this line"
    if not known:
        return ("the CLI reported no TTL split for these, so they are charged at the "
                "5-minute rate of 1.25x — a floor, not a measurement; had they been "
                "1-hour writes they would have cost 2x")
    measured = (f"{fmt(five)} at the 5-minute TTL (1.25x), {fmt(hour)} at the "
                f"one-hour TTL (2x)")
    if known < written:
        return (f"the CLI reported the TTL split for {fmt(known)} of these "
                f"{fmt(written)} tokens — {measured} — and that sample's ratio prices "
                f"the whole line")
    return f"as the CLI reported them: {measured}"


def _zero_tokens() -> dict[str, int]:
    return {c: 0 for c in usage_mod.TOKEN_CLASSES}


def _line(key: str, label: str, note: str = "") -> dict[str, Any]:
    """An empty line item"""
    return {
        "key": key, "label": label, "note": note, "_note": _UNSET,
        "calls": 0,
        "tokens": {**_zero_tokens(), "cache_1h": 0, "cache_5m": 0,
                   "billed_input": 0, "cached_input": 0, "total": 0},
        # `write_rate` is the multiple of base input price this line's cache-WRITE tokens actually cost
        "cost": {"list_usd": 0.0, "exact_usd": 0.0, "by_class": _zero_class_costs(),
                 "write_rate": usage_mod.CACHE_WRITE_RATE, "_write_weighted": 0.0},
        "exact_missing": 0,
        "unit": None,
        "models": [],
        "usage_versions": [],
        "children": [],
    }


def _zero_class_costs() -> dict[str, float]:
    return {c: 0.0 for c in usage_mod.TOKEN_CLASSES}


def _add_item(line: dict[str, Any], item: Item) -> None:
    """Charge one item to one line — the only place a bill's numbers ever grow"""
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
    # A note explains a line only while every item on it says the same thing
    line["_note"] = item.note if line["_note"] in (_UNSET, item.note) else ""
    # Priced PER ITEM, not from the folded totals: the write rate depends on a TTL split that is exact...
    classes = usage_mod.class_costs(
        item.model or "unknown", input=item.input, cache_write=item.cache_write,
        cache_read=item.cache_read, output=item.output,
        cache_1h=item.cache_1h, cache_5m=item.cache_5m)
    # The same per-item rate, kept token-weighted so the line can report the multiple it PAID rather than...
    line["cost"]["_write_weighted"] += item.cache_write * usage_mod.write_rate(
        item.cache_write, item.cache_1h, item.cache_5m)
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
    """Group items into a tree of lines, one level per element of `key(item)`"""
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
    for line in index.values():
        _settle_note(line)
        _settle_rate(line)
    return roots


def _settle_note(line: dict[str, Any]) -> None:
    """Adopt the items' agreed note, unless the fold already gave the line one"""
    proposed = line.pop("_note", _UNSET)
    if not line["note"] and isinstance(proposed, str):
        line["note"] = proposed


def _settle_rate(line: dict[str, Any]) -> None:
    """Turn the weighted accumulator into the one number a reader wants: the rate paid"""
    cost = line["cost"]
    weighted = cost.pop("_write_weighted", 0.0)
    written = line["tokens"]["cache_write"]
    cost["write_rate"] = (weighted / written) if written else usage_mod.CACHE_WRITE_RATE


def _total_line(items: Sequence[Item], label: str) -> dict[str, Any]:
    line = _line("total", label)
    for item in items:
        _add_item(line, item)
    line.pop("_note", None)
    _settle_rate(line)
    return line


# -- gathering the leaves


#: The agent that IS the session — the one whose turns Jarvis drives
LEAD = "the lead agent"
LEAD_NOTE = ("the session's own conversation, with everything its subagents spent taken "
             "out and given their own rows")
SUBAGENT_NOTE = ("spawned by the lead agent — its tokens are part of the total of the "
                 "turn it ran in, not on top of it")

#: THE RESIDUE OF A TURN, NAMED — tokens the envelope reports that neither the lead's own API calls... (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §2)
UNATTRIBUTED = "unattributed"
UNATTRIBUTED_NOTE = (
    "what no transcript accounts for: tokens this turn's envelope reports that neither "
    "the lead agent's own API calls nor its subagents' transcripts can explain. A "
    "subagent's transcript stores most of its assistant messages as mid-stream snapshots "
    "whose `output_tokens` is a streaming placeholder, and the sealed row carrying the "
    "real figure is never written — so the shortfall is mostly output and cannot honestly "
    "be charged to an agent. A RESIDUE, not a measurement")

#: §4: the lead's own transcript is gone, so its row is the turn less its subagents
LEAD_UNMEASURED_NOTE = (
    "the session's own conversation — what is LEFT of the turn after its subagents, "
    "because the lead's own transcript has been pruned. Anything its subagents "
    "under-reported is inside this figure rather than beside it, so it is a ceiling for "
    "the lead and not a measurement of it")

#: §6: said of a figure read from a transcript that holds placeholder output
PLACEHOLDER_NOTE = (
    "the sealed row carrying their real output is never written, so this figure is a "
    "FLOOR and nothing here estimates the difference")

#: THE MEASURED ZERO, said out loud (§9)
PLACEHOLDER_ZERO = (
    "every API call on this bill carries a sealed output figure — a measured zero, not a "
    "missing measurement")


def _turn_items(turn_rows: Sequence[dict[str, Any]],
                session: Any) -> tuple[list[Item], list[str]]:
    """The worker's own spend, split turn by turn and then AGENT by agent"""
    # A compaction is the OS's call, charged on the Jarvis half from `agent_calls`... (docs/superpowers/specs/2026-10-08-count-a-compaction-once.md §1)
    worker_rows = [r for r in turn_rows if r.get("kind") != COMPACT_TURN]
    items: list[Item] = []
    notes: list[str] = []
    recorded = _zero_tokens()
    subs_by_turn = _subagents_by_turn(worker_rows, session)
    stranded = {"subagent": 0, "lead": 0}
    for row in worker_rows:
        if not row.get("recorded"):
            continue
        models = row.get("by_model") or [{
            "model": row.get("model") or "",
            **{c: row.get(c) or 0 for c in usage_mod.TOKEN_CLASSES},
            "cost_usd": row.get("cost_usd"),
        }]
        for m in models:
            for cls in usage_mod.TOKEN_CLASSES:
                recorded[cls] += m.get(cls) or 0
        subs = subs_by_turn.get(row["seq"], [])
        # What the turn's subagents came to, beside what its own API calls came to
        row["subagent_cover"] = {
            cls: sum(getattr(s.usage, cls) for s in subs)
            for cls in usage_mod.TOKEN_CLASSES}
        turn_items, missed = _agent_items(row, models, subs)
        for side, count in missed.items():
            stranded[side] += count
        items.extend(turn_items)
    if stranded["subagent"]:
        notes.append(
            f"{stranded['subagent']:,} tokens a subagent's own transcript reports could "
            "not be taken out of the turn it ran in, because the turn's own total does "
            "not contain them. They stay inside the turn rather than being added to it: "
            "a bill that grew when it looked closer would not be a bill.")
    # Said about the right thing (§5): the sentence above is worded about subagents and would be a lie...
    if stranded["lead"]:
        notes.append(
            f"{stranded['lead']:,} tokens the lead agent's own API calls report are more "
            "than the turn's envelope reports for the whole turn, so they could not be "
            "charged to its row. The excess stays inside the turn rather than being "
            "added to it.")
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
        # WHY a turn is missing from the recorded set decides what to call this line
        live = [r for r in worker_rows
                if not r.get("recorded") and r.get("state") == "running"]
        unrecorded = [r for r in worker_rows if not r.get("recorded")]
        if not worker_rows:
            label = "the conversation, from its transcript"
            note = ("no turns on record at all — this session predates turn capture, "
                    "or was never Jarvis-driven")
        elif live and len(live) == len(unrecorded):
            label = f"turn{'s' if len(live) != 1 else ''} still running"
            note = ("a turn writes its result JSON when it ends, so its spend is the "
                    "transcript's for now and moves onto its own turn line once it "
                    "settles")
            # On the line that is actually low, not only in a summary block (§6)
            if whole.placeholder_calls:
                note += (f" · {whole.placeholder_calls:,} API call(s) report placeholder "
                         f"output totalling {whole.placeholder_output:,} tokens — "
                         + PLACEHOLDER_NOTE)
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


def _subagents_by_turn(turn_rows: Sequence[dict[str, Any]],
                       session: Any) -> dict[int, list[Any]]:
    """Which turn each subagent ran in — the last one that had started when it began"""
    locate = _turn_locator(turn_rows)
    recorded = {r["seq"] for r in turn_rows if r.get("recorded")}
    out: dict[int, list[Any]] = {}
    for sub in getattr(session, "each_subagent", []) or []:
        # A subagent that spent nothing gets no row
        if not sub.usage.total_tokens:
            continue
        seq = locate(sub.started_at)
        if seq in recorded:
            out.setdefault(seq, []).append(sub)
    return out


def _attach_calls(turn_rows: Sequence[dict[str, Any]], session_id: str,
                  index: dict[str, list[Path]] | None) -> None:
    """Put the LEAD agent's API calls on the turn each ran in, and fix the count"""
    calls = usage_mod.session_calls(session_id, index=index) if session_id else []
    for row in turn_rows:
        row["calls_source"] = "envelope"
    if not calls or not turn_rows:
        return
    locate = _turn_locator(turn_rows)
    buckets: dict[int, list[Any]] = {}
    for call in calls:
        seq = locate(call.ts)
        if seq is not None:
            buckets.setdefault(seq, []).append(call)
    for row in turn_rows:
        found = buckets.get(row["seq"]) or []
        if not found:
            continue
        kept, folded = found, None
        if len(found) > TURN_CALL_LIMIT:
            # The dearest calls, because "which call cost that" is what the list answers
            dearest = sorted(range(len(found)),
                             key=lambda i: found[i].total_tokens,
                             reverse=True)[:TURN_CALL_LIMIT]
            chosen = set(dearest)
            kept = [found[i] for i in sorted(chosen)]
            rest = [found[i] for i in range(len(found)) if i not in chosen]
            folded = {
                "count": len(rest),
                **{cls: sum(getattr(c, cls) for c in rest)
                   for cls in usage_mod.TOKEN_CLASSES},
                "total": sum(c.total_tokens for c in rest),
            }
        row["call_rows"] = [call.as_dict() for call in kept]
        row["calls_folded"] = folded
        row["api_calls"] = len(found)
        row["calls_source"] = "transcript"
        # What these calls come to, beside what the turn came to
        row["calls_cover"] = {
            **{cls: sum(getattr(c, cls) for c in found)
               for cls in usage_mod.TOKEN_CLASSES},
            "total": sum(c.total_tokens for c in found),
        }
        # The same cover PER MODEL, because the drawdown in `_agent_items` is per model and a flat figure... (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §2)
        by_model: dict[str, dict[str, int]] = {}
        for call in found:
            counts = by_model.setdefault(call.model or "unknown",
                                         {cls: 0 for cls in usage_mod.TOKEN_CLASSES})
            for cls in usage_mod.TOKEN_CLASSES:
                counts[cls] += getattr(call, cls)
        row["calls_cover_by_model"] = by_model
        # The context a turn actually reached, from the calls themselves
        row["context_peak"] = max(row.get("context_peak") or 0,
                                  max(c.context for c in found))
        window = row.get("context_window") or 0
        if window:
            row["context_pct"] = round(100 * row["context_peak"] / window, 1)


def _agent_items(row: dict[str, Any], models: Sequence[dict[str, Any]],
                 subs: Sequence[Any]) -> tuple[list[Item], dict[str, int]]:
    """One turn, split into the agents that ran inside it (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §2)"""
    budget = {(m.get("model") or "unknown"): {c: m.get(c) or 0
                                              for c in usage_mod.TOKEN_CLASSES}
              for m in models}
    drafts: list[tuple[str, str, dict[str, int], str]] = []
    stranded = {"subagent": 0, "lead": 0}
    for sub in subs:
        for model, counts in (sub.usage.tokens_by_model or {}).items():
            key = model if model in budget else next(iter(budget), "unknown")
            left = budget.get(key)
            if left is None:
                stranded["subagent"] += sum(counts.get(c, 0)
                                            for c in usage_mod.TOKEN_CLASSES)
                continue
            take = {}
            for cls in usage_mod.TOKEN_CLASSES:
                want = counts.get(cls, 0)
                got = min(want, left[cls])
                left[cls] -= got
                take[cls] = got
                stranded["subagent"] += want - got
            if any(take.values()):
                drafts.append((sub.label, model, take, "subagent"))
    # §2.1: with no subagent, nothing the lead's transcript misses belongs to anyone else
    cover = row.get("calls_cover_by_model") if subs else None
    if not cover:
        role = "lead" if not subs else "lead_unmeasured"
        for model, left in budget.items():
            if any(left.values()) or not drafts:
                drafts.append((LEAD, model, dict(left), role))
    else:
        for model, mine in cover.items():
            key = model if model in budget else next(iter(budget), "unknown")
            left = budget.get(key)
            if left is None:
                stranded["lead"] += sum(mine.get(c, 0) for c in usage_mod.TOKEN_CLASSES)
                continue
            take = {}
            for cls in usage_mod.TOKEN_CLASSES:
                want = mine.get(cls, 0)
                got = min(want, left[cls])
                left[cls] -= got
                take[cls] = got
                stranded["lead"] += want - got
            if any(take.values()) or not drafts:
                drafts.append((LEAD, key, take, "lead"))
        # Per model, like the lead's own rows: output is the dearest class and a residue priced in the wrong...
        for model, left in budget.items():
            if any(left.values()):
                drafts.append((UNATTRIBUTED, model, dict(left), "unattributed"))
    # Priced first, so the turn's one exact figure can be shared out by what each row is worth
    priced = [(d, sum(usage_mod.class_costs(d[1], **d[2]).values())) for d in drafts]
    total_list = sum(p for _, p in priced) or 1.0
    exact = row.get("cost_usd")
    # The agent level only when it DISTINGUISHES something: a turn whose only agent is the lead would get...
    labels = {d[0] for d, _ in priced}
    named = len(labels) > 1 or labels != {LEAD}
    items: list[Item] = []
    charged_turn = False
    for i, ((label, model, take, role), list_usd) in enumerate(priced):
        first = i == 0
        # The countable act on a worker line is the TURN
        counts = role.startswith("lead") and not charged_turn
        charged_turn = charged_turn or counts
        leaf: tuple[str, ...] = (label,) if named else ()
        items.append(Item(
            path=(WORKER, f"turn {row['seq']}") + leaf,
            inner=(WORKER,) + leaf,
            turn=row["seq"], model=model if model != "unknown" else "",
            **take,
            # The TTL split is reported once per turn, for part of it
            cache_1h=(row.get("cache_1h") or 0) if first else 0,
            cache_5m=(row.get("cache_5m") or 0) if first else 0,
            exact_usd=None if exact is None else exact * list_usd / total_list,
            calls=1 if counts else 0, unit="turn",
            usage_v=row.get("usage_v") or 1,
            note=(SUBAGENT_NOTE if role == "subagent" else
                  UNATTRIBUTED_NOTE if role == "unattributed" else
                  LEAD_UNMEASURED_NOTE if role == "lead_unmeasured" else
                  LEAD_NOTE if named else f"{row['kind']} turn"),
        ))
    return items, stranded


def _call_items(rows: Sequence[dict[str, Any]],
                turn_at: Any) -> list[Item]:
    """One item per `agent_calls` row — Jarvis's own calls and the worker's subprocesses"""
    from . import agent_usage

    items: list[Item] = []
    for row in rows:
        kind = row.get("kind") or "other"
        described = agent_usage.describe(kind)
        label = row.get("label") or ""
        envelope = db.from_json(row.get("usage_json"), {}) or {}
        if agent_usage.is_subprocess(kind):
            path: tuple[str, ...] = (SUBPROC, label or described)
        elif agent_usage.is_observability(kind):
            # Its own class, exactly as a subprocess row gets one: looking at the order is not Jarvis working on...
            path = (OBSERVE, described)
        else:
            # A third level only when it SAYS something the kind did not
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
    """Which turn a timestamp belongs to: the last one that had STARTED by then"""
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


# -- assembling one order's bill


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
    """The two folds and the total, over one order's items"""
    turn_label = _turn_label(turn_rows)
    by_turn = _fold(
        items,
        key=lambda i: (i.turn if i.turn is not None else NO_TURN,) + i.inner,
        label=lambda p: (turn_label(p[0] if p[0] != NO_TURN else None) if len(p) == 1
                         else _actor_label(p[-1])),
        note=lambda p: (NO_TURN_NOTE if p == (NO_TURN,) else ""),
    )
    # Turns in order, with `NO_TURN` last: it is a residue, not a step in the story
    by_turn.sort(key=lambda line: (line["key"] == NO_TURN,
                                   _seq_of(line["key"])))
    return {
        "total": _total_line(items, label),
        "actors": _fold(items, key=lambda i: i.path,
                        label=lambda p: _actor_label(p[-1]),
                        note=lambda p: ACTOR_NOTES.get(p[-1], "") if len(p) == 1 else ""),
        "turns": by_turn,
        "agents": _agent_view(items),
    }


def _agent_view(items: Sequence[Item]) -> list[dict[str, Any]]:
    """The worker's session by AGENT — the lead, then each subagent it spawned"""
    worker = [i for i in items if i.path and i.path[0] == WORKER]
    view = _fold(worker, key=lambda i: (i.inner[1] if len(i.inner) > 1 else LEAD,),
                 label=lambda p: str(p[-1]))
    # The lead first, then the subagents in the order they were spawned
    view.sort(key=lambda line: (line["label"] != LEAD, line["label"] == UNATTRIBUTED))
    return view


def _seq_of(key: str) -> float:
    head = key.split("/")[0]
    try:
        return float(head)
    except ValueError:
        return float("inf")


def _actor_label(segment: Any) -> str:
    return ACTOR_LABELS.get(str(segment), str(segment))


def build(target: str, project: str | None = None, *,
          live: bool = False) -> dict[str, Any]:
    """The bill for one order, work or feature, resolved by id"""
    from . import ops

    try:
        name, path, order = ops.find_feature_order(target, project)
        feature = True
    except ops.OpsError:
        name, path, order = ops.find_work_order(target, project)
        feature = False
    if not live:
        sealed = unseal(order)
        if sealed is not None:
            upgraded = _upgrade_seal(name, path, order, sealed, feature=feature)
            return upgraded if upgraded is not None else sealed
    if feature:
        return for_feature_order(name, path, order)
    return for_work_order(name, path, order)


def _upgrade_seal(project: str, path: Path, order: dict[str, Any],
                  sealed: dict[str, Any], *, feature: bool) -> dict[str, Any] | None:
    """Re-derive a seal written before this module computed a field it computes now"""
    from .project_store import ProjectStore

    if (sealed.get("payload_v") or 1) >= PAYLOAD_VERSION:
        return None
    try:
        fresh = (for_feature_order(project, path, order) if feature
                 else for_work_order(project, path, order))
        stale, now = sealed["total"]["tokens"], fresh["total"]["tokens"]
        shrank = any(now.get(cls, 0) < stale.get(cls, 0)
                     for cls in (*usage_mod.TOKEN_CLASSES, "total"))
        corrects = _corrects_a_reading(sealed, fresh)
        if shrank and not corrects:
            return None
        at = order.get("bill_sealed_at")
        if shrank:
            fresh["accuracy"] = {**fresh["accuracy"], "corrected_from": stale}
        # Stamped BEFORE it is written, so the record on disk says it was re-derived too
        fresh["accuracy"] = {**fresh["accuracy"], "sealed_at": at, "live": False,
                             "resealed_at": db.now()}
        store = ProjectStore(path)
        try:
            store.seal_bill(order["id"], db.to_json(fresh), feature=feature, at=at)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 — an upgrade that fails must not cost the reader
        return None                                        # the bill they already have
    return fresh


def _corrects_a_reading(sealed: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """Is the fresh bill smaller because the OLD one counted wrong, or because the evidence has aged?"""
    from . import claude_cli

    if _gap_causes(fresh) - _gap_causes(sealed):
        return False
    was = _turn_versions(sealed)
    now = _turn_versions(fresh)
    superseded = {v for v in (*was, *now) if v < claude_cli.USAGE_SCHEMA_VERSION}
    return bool(was & superseded) and not (now & superseded)


def _turn_versions(payload: dict[str, Any]) -> set[int]:
    """Which reading produced the WORKER TURNS of one bill, at any level of it"""
    versions = {row.get("usage_v") or 1
                for row in payload.get("turn_rows") or [] if row.get("recorded")}
    for order in payload.get("orders") or []:      # a feature order, one level down
        versions |= _turn_versions(order)
    return versions


def _call_versions(rows: Sequence[dict[str, Any]]) -> set[int]:
    """Which reading produced JARVIS'S OWN CALLS on one bill (docs/superpowers/specs/2026-09-24-order-observability.md)"""
    from . import agent_usage

    versions: set[int] = set()
    for row in rows:
        if agent_usage.is_observability(row.get("kind") or ""):
            continue
        envelope = db.from_json(row.get("usage_json"), {}) or {}
        if envelope:
            versions.add(envelope.get("usage_v") or 1)
    return versions


def unseal(order: dict[str, Any]) -> dict[str, Any] | None:
    """The bill frozen onto this order when it settled, if there is one"""
    raw = order.get("bill_json")
    if not raw:
        return None
    payload = db.from_json(raw, None)
    if not isinstance(payload, dict) or not payload.get("total"):
        return None
    payload["accuracy"] = {**(payload.get("accuracy") or {}),
                           "sealed_at": order.get("bill_sealed_at"), "live": False}
    return payload


def seal(project: str, path: Path, order: dict[str, Any], *,
         feature: bool = False, index: dict[str, list[Path]] | None = None
         ) -> dict[str, Any]:
    """Compute a settled order's bill and freeze it onto the order"""
    from .project_store import ProjectStore

    payload = (for_feature_order(project, path, order, index=index) if feature
               else for_work_order(project, path, order, index=index))
    store = ProjectStore(path)
    try:
        store.seal_bill(order["id"], db.to_json(payload), feature=feature)
    finally:
        store.close()
    payload["accuracy"]["sealed_at"] = db.now()
    payload["accuracy"]["live"] = False
    return payload


# WHAT EACH DISCLOSURE BELOW IS ABOUT, as a key that outlives its wording
_GAP_CAUSES = (
    ("transcript for the worker's session is gone", "no-transcript"),
    # One cause, said twice: a turn's sentence and a call's
    ("modelUsage fix", "read-before-modelusage"),
    ("per-turn fix", "read-before-per-turn"),
    ("no longer have the result JSON", "estimated-from-transcript"),
)


def _gap_causes(payload: dict[str, Any]) -> set[str]:
    """The causes one bill's `accuracy.gaps` disclose, whatever words they used"""
    causes = set()
    for gap in ((payload.get("accuracy") or {}).get("gaps") or []):
        causes.add(next((cause for mark, cause in _GAP_CAUSES if mark in gap), gap))
    return causes


def _accuracy(payload: dict[str, Any], turn_rows: Sequence[dict[str, Any]],
              session: Any, calls: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """What this bill could not see — the disclaimer, computed rather than assumed"""
    gaps: list[str] = []
    if turn_rows and not session.found:
        gaps.append("Claude Code's transcript for the worker's session is gone, so any "
                    "turn without a result JSON of its own could not be counted at all.")
    # Turn versions, not the bill's mixed set: an OS call's stamp is not a claim about a turn's reading...
    versions = _turn_versions({"turn_rows": turn_rows})
    if 1 in _call_versions(calls):
        # Said separately from the turn line below and worded for a CALL
        gaps.append("Some of Jarvis's own calls on this order were counted before the "
                    "modelUsage fix, so each is its lead agent's spend only and misses "
                    "whatever it spawned beneath itself — a floor, not a total.")
    if 1 in versions:
        gaps.append("Some turns were counted before the modelUsage fix and their result "
                    "JSON is gone too, so their tokens are the old, low reading — a "
                    "floor, not a total.")
    if 2 in versions:
        # The opposite disclaimer to the one above
        gaps.append("Some turns were counted before the per-turn fix and their result "
                    "JSON is gone, so each one still carries the whole session's "
                    "running total — a ceiling, not a total.")
    if any("no result JSON left" in (line.get("label") or "")
           for line in payload.get("turns") or []):
        gaps.append("Some turns no longer have the result JSON the CLI wrote; their "
                    "spend is estimated from the transcript, at list prices.")
    return {"sealed_at": None, "live": True, "complete": not gaps, "gaps": gaps}


def _cold_prefix_floor() -> int:
    """`os.cold_prefix_floor`, or the catalog's own error if none is registered"""
    from .ops import resolve_catalog

    return resolve_catalog().os.cold_prefix_floor


def for_work_order(project: str, path: Path, wo: dict[str, Any],
                   index: dict[str, list[Path]] | None = None) -> dict[str, Any]:
    """One work order's bill: its turns, Jarvis's calls on it, its own subprocesses"""
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
    session = usage_mod.read_session(wo.get("session_id") or "", _cold_prefix_floor(),
                                     index=index)
    # Per-API-call detail, sealed onto the turn rows
    _attach_calls(turn_rows, wo.get("session_id") or "", index)
    worker_items, notes = _turn_items(turn_rows, session)
    items = [*worker_items, *_call_items(calls, _turn_locator(turn_rows))]
    payload = {
        "kind": "work_order",
        "payload_v": PAYLOAD_VERSION,
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
    payload["accuracy"] = _accuracy(payload, turn_rows, session, calls)
    payload["floor_reason"] = ops.COST_FLOOR_NOTE
    return payload


def for_feature_order(project: str, path: Path, fo: dict[str, Any],
                      index: dict[str, list[Path]] | None = None) -> dict[str, Any]:
    """A feature order's bill: its planner and every child, each a whole bill itself"""
    from . import ops
    from .central_store import CentralStore
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
    # ONE pass over Claude Code's transcript tree for the whole feature
    index = index if index is not None else usage_mod.index_sessions()
    orders = [for_work_order(project, path, child, index=index) for child in children]
    # Calls Jarvis made against the PARENT row itself (docs/superpowers/specs/2026-09-23-improvement-orders.md)
    central = CentralStore()
    try:
        own_calls = central.agent_calls(wo_id=fo["id"], limit=CALL_LIMIT)
    finally:
        central.close()
    own_items = _call_items(own_calls, lambda ts: None)
    items: list[Item] = [*own_items]
    agent_items: list[Item] = []
    for order, child in zip(orders, children):
        for line in order["actors"]:
            # Re-charged as one item per leaf of the child's own tree
            items.extend(_relabel(line, child["id"]))
        for line in order.get("agents") or []:
            agent_items.extend(_relabel(line, child["id"], prefix=(child["id"],)))
    payload = {
        "kind": "feature_order",
        "payload_v": PAYLOAD_VERSION,
        "scope": fo["id"], "id": fo["id"], "title": fo["title"],
        "status": fo["status"], "project": project,
        "total": _total_line(items, fo["id"]),
        "actors": _fold(items, key=lambda i: i.path,
                        label=lambda p: _actor_label(p[-1]),
                        note=lambda p: ACTOR_NOTES.get(p[-1], "") if len(p) == 1 else ""),
        "turns": [],
        # Every agent the feature ran, one order at a time
        "agents": _fold(agent_items, key=lambda i: i.path,
                        label=lambda p: str(p[-1])),
        "orders": orders,
        # Its own calls as one line beside the orders
        "own": _total_line(own_items, fo["id"]) if own_items else None,
        "notes": [],
        "checks": {},
    }
    payload["checks"] = reconcile(payload)
    # A feature's accuracy is the worst of its children's: one order whose transcript is gone makes the...
    gaps: list[str] = []
    for order in orders:
        for gap in (order.get("accuracy") or {}).get("gaps") or []:
            entry = f"{order['id']}: {gap}"
            if entry not in gaps:
                gaps.append(entry)
    payload["accuracy"] = {"sealed_at": None, "live": True,
                           "complete": not gaps, "gaps": gaps}
    payload["floor_reason"] = ops.COST_FLOOR_NOTE
    return payload


def _relabel(line: dict[str, Any], wo_id: str, prefix: tuple[str, ...] = ()
             ) -> list[Item]:
    """Flatten a built line tree back into items, for folding one level up"""
    # The order id goes in as the SECOND segment
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
    """The facts about a worker's session that live beside the line items"""
    total = session.total
    return {
        "subagents": {
            "count": session.subagent_count,
            "list_usd": session.subagents.list_cost_usd,
            "output": session.subagents.output,
            "billed_input": session.subagents.billed_input,
        },
        # §6
        "placeholder": {
            "calls": total.placeholder_calls,
            "output": total.placeholder_output,
            "main_calls": session.main.placeholder_calls,
            "subagent_calls": session.subagents.placeholder_calls,
        },
        "rewrite": {
            "tokens": total.rewrite_excess,
            "list_usd": total.rewrite_cost_usd,
            "boundaries": total.resume_boundaries,
            "ttl_share": total.rewrite_ttl_share,
            "ttl_tokens": total.rewrite_ttl_excess,
            "ttl_boundaries": total.boundaries_ttl,
            # THE RAW OBSERVATIONS, and not a second apportionment of the tax
            "ttl_write": total.rewrite_ttl_write,
            "prefix_write": total.rewrite_prefix_write,
            "compact_write": total.rewrite_compact_write,
            "compact_boundaries": total.boundaries_compact,
            "cache_write": total.cache_write,
            # q1215: the order-level rollup, LABELLED by side
            "by_side": {
                "main": {"tokens": session.main.rewrite_excess,
                         "boundaries": session.main.resume_boundaries,
                         "cache_write": session.main.cache_write},
                "subagent": {"tokens": session.subagents.rewrite_excess,
                             "boundaries": session.subagents.resume_boundaries,
                             "cache_write": session.subagents.cache_write,
                             "count": session.subagent_count,
                             "structural_zero": (
                                 session.subagents.rewrite_excess == 0
                                 and session.subagents.resume_boundaries == 0)},
            },
        },
        "context_peak": total.context_peak,
    }


def _annotate_turns(payload: dict[str, Any], turn_rows: Sequence[dict[str, Any]]) -> None:
    """Hang each turn's timings and context on its line, for the per-turn header"""
    by_seq = {str(r["seq"]): r for r in turn_rows}
    for line in payload["turns"]:
        row = by_seq.get(line["key"])
        if row:
            line["turn"] = row


def reconcile(payload: dict[str, Any], tolerance: float = 1e-6) -> dict[str, Any]:
    """Prove the bill adds up, and say so in the payload"""
    problems: list[str] = []
    total = payload["total"]
    views = {name: payload.get(name) or [] for name in ("turns", "actors")}
    # A feature order's third view: its children, each a whole bill
    if payload.get("orders"):
        views["orders"] = [order["total"] for order in payload["orders"]]
        # Plus what Jarvis spent on the parent row itself, which belongs to no child
        if payload.get("own"):
            views["orders"].append(payload["own"])
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
    problems.extend(_check_agents(payload))
    problems.extend(_check_calls(payload))
    problems.extend(_check_lead(payload))
    return {"balanced": not problems, "problems": problems}


#: How far a turn's own figure may stand above everything found INSIDE it (#470)
CALL_COVER_SLACK = 2.0

#: ...and the flat half of it, sized to absorb one self-compaction of a large context
CALL_COVER_RESIDUE = 1_500_000


def _check_compaction_once(payload: dict[str, Any],
                           rows: Sequence[dict[str, Any]]) -> list[str]:
    """A compaction charged to the worker AND recorded as a Jarvis compaction (docs/superpowers/specs/2026-10-08-count-a-compaction-once.md §3)"""
    from . import agent_usage

    described = agent_usage.describe(agent_usage.COMPACTION)
    problems: list[str] = []
    by_key = {line.get("key"): line for line in payload.get("turns") or []}
    for row in rows:
        if row.get("kind") != COMPACT_TURN:
            continue
        seq = row.get("seq")
        line = by_key.get(str(seq))
        if not line:
            continue
        children = {child.get("key"): child for child in line.get("children") or []}
        jarvis = children.get(f"{seq}/{JARVIS}")
        compaction = any(
            grand.get("key") == f"{seq}/{JARVIS}/{described}"
            for grand in (jarvis or {}).get("children") or [])
        if f"{seq}/{WORKER}" in children and compaction:
            problems.append(
                f"turn {seq}: the compaction is charged to the worker AND recorded as "
                "a Jarvis compaction — one compaction, counted twice")
    return problems


def _check_calls(payload: dict[str, Any]) -> list[str]:
    """Per-call rows are a PARTITION of their turn: they may not exceed it, and"""
    problems: list[str] = []
    rows = payload.get("turn_rows") or []
    problems.extend(_check_compaction_once(payload, rows))
    rows = [row for row in rows if row.get("kind") != COMPACT_TURN]
    for row in rows:
        cover = row.get("calls_cover")
        # A turn with no result JSON yet
        if not cover or not row.get("recorded"):
            continue
        for cls in usage_mod.TOKEN_CLASSES:
            if cover[cls] > (row.get(cls) or 0):
                problems.append(
                    f"turn {row['seq']}: its API calls total {cover[cls]} {cls}, more "
                    f"than the turn's own {row.get(cls) or 0}")
    recorded = [row for row in rows if row.get("recorded")]
    if not recorded or not all(row.get("calls_cover") for row in recorded):
        return problems
    for row in recorded:
        subs = row.get("subagent_cover") or {}
        inside = (row["calls_cover"]["total"]
                  + sum(subs.get(cls, 0) for cls in usage_mod.TOKEN_CLASSES))
        mine = sum(row.get(cls) or 0 for cls in usage_mod.TOKEN_CLASSES)
        if inside and mine > inside * CALL_COVER_SLACK + CALL_COVER_RESIDUE:
            problems.append(
                f"turn {row['seq']}: it claims {mine} tokens but everything inside it — "
                f"{row['calls_cover']['total']} across its API calls and "
                f"{inside - row['calls_cover']['total']} across its subagents — comes "
                f"to {inside}")
    return problems


def _check_agents(payload: dict[str, Any]) -> list[str]:
    """The user's identity: every agent added up IS the worker's whole session"""
    agents = payload.get("agents") or []
    if not agents:
        return []
    worker = next((line for line in payload.get("actors") or []
                   if line["key"] == WORKER), None)
    if worker is None:
        return ["agents: there are agent rows but no worker line to match them to"]
    problems = []
    for cls in (*usage_mod.TOKEN_CLASSES, "total"):
        summed = sum(line["tokens"][cls] for line in agents)
        if summed != worker["tokens"][cls]:
            problems.append(f"agents: {cls} sums to {summed}, the worker's session is "
                            f"{worker['tokens'][cls]}")
    return problems


def _check_lead(payload: dict[str, Any]) -> list[str]:
    """A lead row may never exceed what the lead's OWN API calls report (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §8)"""
    problems: list[str] = []
    for row in payload.get("turn_rows") or []:
        cover = row.get("calls_cover_by_model")
        if not cover or not row.get("recorded"):
            continue
        line = _descendant(payload.get("turns") or [], str(row["seq"]), LEAD)
        if line is None:
            continue
        for cls in usage_mod.TOKEN_CLASSES:
            mine = sum(counts.get(cls, 0) for counts in cover.values())
            if line["tokens"][cls] > mine:
                problems.append(
                    f"turn {row['seq']}: the lead agent is charged "
                    f"{line['tokens'][cls]} {cls}, more than its own API calls report "
                    f"({mine})")
    return problems


def _descendant(roots: Sequence[dict[str, Any]], key: str,
                label: str) -> dict[str, Any] | None:
    """The first line labelled `label` anywhere under the root keyed `key`"""
    root = next((line for line in roots if line["key"] == key), None)
    stack = list(root["children"] or []) if root else []
    while stack:
        line = stack.pop(0)
        if line.get("label") == label:
            return line
        stack.extend(line.get("children") or [])
    return None


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


# -- the re-write tax as a STANDING condition

#: `inspection`'s two aggregate alarm kinds, duplicated as literals rather than imported
REWRITE_PREFIX_ALARM = "rewrite-tax-prefix"
REWRITE_TTL_ALARM = "rewrite-tax-ttl"


def cohort_command(days: int = 30) -> str:
    """The `scripts/cache_ttl_cohort.py` invocation a finding points its reader at"""
    return f"uv run python scripts/cache_ttl_cohort.py --days {days}"


#: The default window's spelling, which is what `rewrite_alarms` quotes
COHORT_COMMAND = cohort_command()

#: The break-even the TTL decision is actually taken against
TTL_DECISION_NOTE = (
    "This is a share of the BILL and is NOT the trigger for switching the write TTL. "
    "That decision is TTL-expiry tokens as a share of ALL cache writes, against a "
    f"break-even of {usage_mod.TTL_BREAK_EVEN:.1%} — a different ratio over a much "
    "larger denominator, running at about half this figure. Measure it with "
    f"`{COHORT_COMMAND}` before proposing any change to the cache TTL."
)


@dataclass
class RewriteTax:
    """One project's re-write tax over a cohort window, split by CAUSE"""

    days: int
    since: float
    orders: int
    bill_usd: float
    tax_usd: float
    tax_tokens: int
    classified_tokens: int
    ttl_tokens: int
    #: The biggest single contributor, which is the alarm's CARRIER and its exemplar — never its culprit
    worst_id: str
    worst_title: str
    worst_tax_usd: float
    #: Sealed orders in the window carrying NO re-write block at all
    unmeasured_orders: int = 0

    @property
    def ttl_share(self) -> float | None:
        """Of the tax, the fraction the cache entry EXPIRING accounts for"""
        if not self.classified_tokens:
            return None
        return self.ttl_tokens / self.classified_tokens

    @property
    def tax_share(self) -> float:
        """The whole tax as a fraction of the window's bill, both at list prices"""
        return self.tax_usd / self.bill_usd if self.bill_usd else 0.0

    @property
    def ttl_share_of_bill(self) -> float | None:
        """None, never 0.0, when nothing was classified"""
        share = self.ttl_share
        return self.tax_share * share if share is not None else None

    @property
    def prefix_share_of_bill(self) -> float | None:
        """None when unmeasured — see `ttl_share_of_bill`"""
        share = self.ttl_share
        return self.tax_share * (1 - share) if share is not None else None

    @property
    def coverage(self) -> float:
        """How much of the tax the split actually accounts for"""
        return self.classified_tokens / self.tax_tokens if self.tax_tokens else 0.0


def rewrite_tax(store: Any, *, days: int, now: float | None = None) -> RewriteTax | None:
    """One project's re-write tax over the trailing `days`, or None if nothing settled"""
    now = now if now is not None else db.now()
    since = now - days * 86_400
    orders = store.sealed_bills_since(since)
    bill_usd = tax_usd = 0.0
    tax_tokens = classified = ttl_tokens = 0
    counted = unmeasured = 0
    worst = ("", "", 0.0)
    for order in orders:
        payload = db.from_json(order.get("bill_json") or "", {}) or {}
        rewrite = payload.get("rewrite")
        if not isinstance(rewrite, dict):
            # A bill sealed before `_worker_extras` carried the block
            unmeasured += 1
            continue
        counted += 1
        total = payload.get("total") or {}
        # LIST prices on both sides
        bill_usd += float((total.get("cost") or {}).get("list_usd") or 0.0)
        this_tax = float(rewrite.get("list_usd") or 0.0)
        tax_usd += this_tax
        tokens = int(rewrite.get("tokens") or 0)
        tax_tokens += tokens
        if rewrite.get("ttl_share") is not None:
            classified += tokens
            ttl_tokens += int(rewrite.get("ttl_tokens") or 0)
        if this_tax > worst[2]:
            worst = (str(order["id"]), str(order.get("title") or ""), this_tax)
    if not counted or not worst[0]:
        return None
    return RewriteTax(days=days, since=since, orders=counted, bill_usd=bill_usd,
                      tax_usd=tax_usd, tax_tokens=tax_tokens,
                      classified_tokens=classified, ttl_tokens=ttl_tokens,
                      worst_id=worst[0], worst_title=worst[1], worst_tax_usd=worst[2],
                      unmeasured_orders=unmeasured)


def _coverage_note(tax: RewriteTax) -> str:
    """Said only when the split rests on part of the tax"""
    if tax.coverage >= 0.99:
        return ""
    return (f" The split is measured over {tax.coverage:.0%} of the tax; the rest "
            f"crossed no boundary this OS could classify.")


def _denominator_note(tax: RewriteTax) -> str:
    """The OTHER coverage, which `_coverage_note` does not cover: the split's evidence is part of the tax"""
    if not tax.unmeasured_orders:
        return ""
    return (f" That bill is the {tax.orders} settled orders carrying a re-write "
            f"measurement; {tax.unmeasured_orders} more settled in the window with no "
            f"re-write block to read, and their spend is in neither half.")


def rewrite_alarms(tax: RewriteTax, cfg: Any) -> list[tuple[str, str]]:
    """What is standing-wrong with this project's spend, one alarm per CAUSE"""
    ttl_share = tax.ttl_share
    if ttl_share is None:
        return []
    prefix_of_bill, ttl_of_bill = tax.prefix_share_of_bill, tax.ttl_share_of_bill
    assert prefix_of_bill is not None and ttl_of_bill is not None  # measured, see above
    window = f"over the last {tax.days} days ({tax.orders} settled orders)"
    worst = (f"Biggest single contributor: {tax.worst_id} "
             f"(${tax.worst_tax_usd:.2f}) — `jarvis inspect {tax.worst_id}` labels every "
             f"re-write it made by cause.")
    raised: list[tuple[str, str]] = []
    if prefix_of_bill >= cfg.alarm_rewrite_prefix_share:
        raised.append((REWRITE_PREFIX_ALARM, (
            f"{prefix_of_bill:.0%} of this project's ${tax.bill_usd:,.0f} bill "
            f"{window} went on re-sending conversations whose PROMPT PREFIX had moved — "
            f"${tax.tax_usd * (1 - ttl_share):,.2f}, and no cache TTL can buy back "
            f"any of it. The cure is whatever is changing the head of the prompt between "
            f"calls.{_coverage_note(tax)}{_denominator_note(tax)} {worst}")))
    if ttl_of_bill >= cfg.alarm_rewrite_ttl_share:
        raised.append((REWRITE_TTL_ALARM, (
            f"{ttl_of_bill:.0%} of this project's ${tax.bill_usd:,.0f} bill "
            f"{window} went on conversations re-sent because the CACHE ENTRY EXPIRED — "
            f"${tax.tax_usd * ttl_share:,.2f}, the part a longer cache TTL could have "
            f"bought back. {TTL_DECISION_NOTE}{_coverage_note(tax)}"
            f"{_denominator_note(tax)} {worst}")))
    return raised


# -- cache health: the two ratios the FLEET's cache configuration is judged on


@dataclass
class CacheWrites:
    """Cache-write tokens over a cohort window, split by what happened at each boundary (#160)"""

    orders: int = 0
    cache_write: int = 0
    ttl_write: int = 0
    prefix_write: int = 0
    boundaries: int = 0
    ttl_boundaries: int = 0
    #: The boundaries the OS compacted across, and what they wrote
    compact_write: int = 0
    compact_boundaries: int = 0
    #: Sealed orders in the window whose bill predates `PAYLOAD_VERSION` 3 and so has no raw split to read
    unmeasured_orders: int = 0

    def __add__(self, other: CacheWrites) -> CacheWrites:
        return CacheWrites(
            orders=self.orders + other.orders,
            cache_write=self.cache_write + other.cache_write,
            ttl_write=self.ttl_write + other.ttl_write,
            prefix_write=self.prefix_write + other.prefix_write,
            boundaries=self.boundaries + other.boundaries,
            ttl_boundaries=self.ttl_boundaries + other.ttl_boundaries,
            compact_write=self.compact_write + other.compact_write,
            compact_boundaries=self.compact_boundaries + other.compact_boundaries,
            unmeasured_orders=self.unmeasured_orders + other.unmeasured_orders,
        )

    @property
    def prefix_boundaries(self) -> int:
        """The boundaries left once the two EXPLAINED kinds are taken out"""
        return self.boundaries - self.ttl_boundaries - self.compact_boundaries

    @property
    def ttl_share_of_writes(self) -> float | None:
        """THE DECIDING RATIO for the write TTL: TTL-expiry writes over ALL cache writes"""
        return self.ttl_write / self.cache_write if self.cache_write else None

    @property
    def prefix_share_of_writes(self) -> float | None:
        """The same denominator, the other cause: writes spent re-sending a MOVED PREFIX"""
        return self.prefix_write / self.cache_write if self.cache_write else None

    @property
    def ttl_share_of_tax(self) -> float | None:
        """NOT A TRIGGER"""
        seen = self.ttl_write + self.prefix_write
        return self.ttl_write / seen if seen else None


def cache_writes_since(store: Any, since: float) -> CacheWrites:
    """One project's `CacheWrites` over the window, read off its sealed bills"""
    found = CacheWrites()
    for order in store.sealed_bills_since(since):
        payload = db.from_json(order.get("bill_json") or "", {}) or {}
        rewrite = payload.get("rewrite")
        if not isinstance(rewrite, dict) or "cache_write" not in rewrite:
            found.unmeasured_orders += 1
            continue
        found.orders += 1
        found.cache_write += int(rewrite.get("cache_write") or 0)
        found.ttl_write += int(rewrite.get("ttl_write") or 0)
        found.prefix_write += int(rewrite.get("prefix_write") or 0)
        found.boundaries += int(rewrite.get("boundaries") or 0)
        found.ttl_boundaries += int(rewrite.get("ttl_boundaries") or 0)
        # Absent on a version-3 seal, and zero is the right reading rather than a gap: nothing sealed before...
        found.compact_write += int(rewrite.get("compact_write") or 0)
        found.compact_boundaries += int(rewrite.get("compact_boundaries") or 0)
    return found
