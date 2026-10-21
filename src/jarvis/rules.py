"""The registry the OS uses to recognise its own recurring gaps — grammar and evaluator (docs/superpowers/specs/2026-09-27-self-evolution.md §3.)"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from . import remedies

# -- vocabulary

#: A detector's lifecycle
DRY_RUN = "dry_run"
ARMED = "armed"
RETRACTED = "retracted"
STATUSES = (DRY_RUN, ARMED, RETRACTED)

#: The two modes a fire can happen in — a `rule_fires.mode` value
MODES = (DRY_RUN, ARMED)

#: Who wrote the rule
SOURCES = ("builtin", "io", "user")

#: What a detector can be evaluated against
SUBJECTS = ("work_order", "feature_order")
DEFAULT_SUBJECT = "work_order"

#: `rule_fires.outcome`
RECORDED = "recorded"      # a dry run: the condition held, nothing was proposed
PROPOSED = "proposed"      # an armed fire that raised an alarm
APPLIED = "applied"        # one whose remedy the gate then let run
REFUSED = "refused"        # the remedy path declined — allow-list, grant, precondition
UNREADABLE = "unreadable"  # something could not be READ; nothing was decided
CLEARED = "cleared"        # the fire closed because the condition no longer holds
FIRE_OUTCOMES = (RECORDED, PROPOSED, APPLIED, REFUSED, UNREADABLE, CLEARED)

#: `rule_recurrences.verdict`: WHICH HALF OF THE RULE did not hold when a gap the OS already has a...
MISSED = "missed"
REMEDY_FAILED = "remedy_failed"
NOT_ARMED = "not_armed"
UNREADABLE_RECURRENCE = UNREADABLE
RECURRENCE_VERDICTS = (NOT_ARMED, MISSED, REMEDY_FAILED, UNREADABLE_RECURRENCE)

#: How deep and how wide a condition may be
MAX_DEPTH = 4
MAX_NODES = 32

#: Every snapshot text stored on a fire is bounded at this
FACTS_CHARS = 4000


def bound(text: str, limit: int = FACTS_CHARS) -> str:
    """`text`, truncated to `limit`, SAYING SO when it was"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" […truncated at {limit} characters]"


# -- the grammar

ALL = "all"
ANY = "any"
NOT = "not"
COMBINATORS = (ALL, ANY, NOT)

EQ = "eq"
NE = "ne"
IN = "in"
NOT_IN = "not_in"
GTE = "gte"
LTE = "lte"
EXISTS = "exists"
ABSENT = "absent"
COUNT_GTE = "count_gte"
OPERATORS = (EQ, NE, IN, NOT_IN, GTE, LTE, EXISTS, ABSENT, COUNT_GTE)

# THERE IS DELIBERATELY NO REGEX OPERATOR

#: The four types a fact can have
STR = "str"
NUM = "num"
BOOL = "bool"
MAP = "map"


@dataclass(frozen=True)
class FactField:
    """One thing a condition may ask about an order"""

    name: str
    type: str
    source: str
    note: str = ""


def _fields(*rows: tuple[str, str, str, str]) -> dict[str, FactField]:
    return {name: FactField(name, type_, source, note)
            for name, type_, source, note in rows}


#: THE CLOSED TABLE
FACT_FIELDS: dict[str, FactField] = _fields(
    # The `work_orders` row itself
    ("status", STR, "work_order", "the work order's status column"),
    ("kind", STR, "work_order", "`worker`, `planner`, `analyst` — the WO_KINDS set"),
    ("hidden", BOOL, "work_order", "true when the user decluttered it out of listings"),
    ("needs_attention", BOOL, "work_order", "the attention FLAG, not the reason"),
    ("attention_reason", STR, "work_order",
     "one of `invariants.true_blockers`' own sentences. Nothing else may write it, so a "
     "condition quoting one is quoting that function"),

    # `ops.state_durations`
    ("seconds_in_status", NUM, "state_durations", "since the last status change"),
    ("seconds_in_status_active", NUM, "state_durations",
     "since the last status change, minus every interval `holds` says the order was not "
     "permitted to run"),
    ("seconds_since_activity", NUM, "state_durations", "since the newest timeline event"),
    ("lifetime_seconds", NUM, "state_durations", "since the order was created"),
    ("last_activity_kind", STR, "state_durations", "the newest timeline event's kind"),

    # `holds.held` / `holds.by_cause`
    ("hold_cause", STR, "holds",
     "the merged open hold's cause — vocabulary `holds.HOLD_CAUSES`"),
    ("hold_seconds", NUM, "holds", "how long that hold has been open"),

    # The `automerge.HELD_*` set: why the validated auto-merge did not merge
    ("automerge_code", STR, "automerge", "one of the `automerge.HELD_*` constants"),

    ("waiting_on", STR, "waiting_on", "`ops.waiting_on`'s slug for what it is waiting on"),

    # `ProjectStore.latest_validation_round`
    ("round_no", NUM, "validation_round", "the newest round's number"),
    ("round_outcome", STR, "validation_round", "`passed`, `rejected` or `escalated`"),
    ("rounds_left", NUM, "validation_round", "rounds remaining before `max_rounds`"),
    ("judged_head_sha", STR, "validation_round", "the commit that round judged"),

    # The `validation_failed` PAYLOADS, by cause (docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md)
    ("validation_failure_causes", MAP, "validation_failures",
     "`validation_failed` event causes to their counts — `transport`, `killed`, the auth "
     "cause"),

    # The RECORDED pull-request columns
    ("pr_url", STR, "pull_request", "the recorded column; never a live `gh` call"),
    ("pr_state", STR, "pull_request", "the recorded column; never a live `gh` call"),

    # `ProjectStore.count_events` — `{event kind: count}`
    ("event_counts", MAP, "event_counts", "timeline event kinds to their counts"),
    # The `invariant` timeline events
    ("invariant_events", MAP, "invariant_events", "invariant names to their counts"),

    ("depends_on_count", NUM, "dependencies", "edges this order waits on"),
    ("dead_dependency_count", NUM, "dependencies",
     "edges that can never clear — `invariants.dead_dependencies`"),

    ("neo_question_status", STR, "neo_question", "the row's status; `invariants.awaiting_neo`"),
    ("neo_question_attempts", NUM, "neo_question", "attempts already spent on it"),

    ("budget_usd", NUM, "budget", "the ceiling, or absent when there is none"),
    ("spent_usd", NUM, "budget", "the whole bill the ceiling governs"),
)

#: Which operators each field type admits, enforced at PARSE time rather than at evaluation time
_OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    STR: (EQ, NE, IN, NOT_IN, EXISTS, ABSENT),
    NUM: (EQ, NE, IN, NOT_IN, GTE, LTE, EXISTS, ABSENT),
    BOOL: (EQ, NE, IN, NOT_IN, EXISTS, ABSENT),
    MAP: (COUNT_GTE, EXISTS, ABSENT),
}


class RulesError(Exception):
    """A condition that cannot be accepted"""

    def __init__(self, problems: Iterable[str]):
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("; ".join(self.problems))


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, (int, float)):
        return NUM
    if isinstance(value, str):
        return STR
    return type(value).__name__


def _value_matches_type(value: Any, expected: str) -> bool:
    """Whether a literal may stand beside a field of type `expected`"""
    if expected == BOOL:
        return isinstance(value, bool)
    if expected == NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == STR:
        return isinstance(value, str)
    return False


def _parse_leaf(node: Mapping[str, Any], path: str,
                problems: list[str]) -> dict[str, Any] | None:
    """One `{"field", "op", "value"}` node, normalised, or None when it is unusable"""
    extra = sorted(set(node) - {"field", "op", "value"})
    if extra:
        problems.append(f"{path}: unknown key(s) {', '.join(repr(k) for k in extra)} — "
                        f"a leaf is `field`, `op` and `value`")
    name = node.get("field")
    if not isinstance(name, str) or name not in FACT_FIELDS:
        problems.append(f"{path}.field: unknown field {name!r} — `FACT_FIELDS` is closed, "
                        f"and a field nobody implemented is refused here rather than "
                        f"evaluating to a silent false")
        return None
    spec = FACT_FIELDS[name]
    op = node.get("op")
    if not isinstance(op, str) or op not in OPERATORS:
        problems.append(f"{path}.op: unknown operator {op!r} — one of "
                        f"{', '.join(OPERATORS)}")
        return None
    if op not in _OPS_BY_TYPE[spec.type]:
        problems.append(
            f"{path}.op: `{op}` cannot be applied to `{name}`, which is a {spec.type} "
            f"field — it admits {', '.join(_OPS_BY_TYPE[spec.type])}")
        return None

    value = node.get("value")
    if op in (EXISTS, ABSENT):
        # `value` must be absent or null: a value here means the author believed the operator compared...
        if "value" in node and value is not None:
            problems.append(f"{path}.value: `{op}` takes no value")
        value = None
    elif op in (EQ, NE):
        if not _value_matches_type(value, spec.type):
            problems.append(f"{path}.value: `{name}` is a {spec.type} field, got "
                            f"{_type_name(value)}")
            return None
    elif op in (IN, NOT_IN):
        if not isinstance(value, list) or not value:
            problems.append(f"{path}.value: `{op}` takes a non-empty list of "
                            f"{spec.type} values")
            return None
        bad = [i for i, v in enumerate(value) if not _value_matches_type(v, spec.type)]
        if bad:
            problems.append(f"{path}.value: `{name}` is a {spec.type} field, but "
                            f"{', '.join(f'value[{i}]' for i in bad)} is not")
            return None
    elif op in (GTE, LTE):
        if not _value_matches_type(value, NUM):
            problems.append(f"{path}.value: `{op}` takes a number, got "
                            f"{_type_name(value)}")
            return None
    elif op == COUNT_GTE:
        if (not isinstance(value, Mapping) or not isinstance(value.get("kind"), str)
                or not value.get("kind")
                or not _value_matches_type(value.get("value"), NUM)):
            problems.append(f"{path}.value: `count_gte` takes "
                            f'{{"kind": <str>, "value": <int>}}')
            return None
        value = {"kind": value["kind"], "value": int(value["value"])}

    return {"field": name, "op": op, "value": value}


def _parse_node(node: Any, path: str, depth: int, problems: list[str],
                counter: list[int]) -> Any:
    counter[0] += 1
    if counter[0] == MAX_NODES + 1:
        problems.append(f"the condition has more than {MAX_NODES} nodes — a document "
                        f"that wide is one nobody reads before arming it")
    if depth > MAX_DEPTH:
        problems.append(f"{path}: nested deeper than {MAX_DEPTH} — a condition must be "
                        f"reviewable in one reading")
        return None
    if not isinstance(node, Mapping):
        problems.append(f"{path or 'the condition'}: must be a JSON object, got "
                        f"{_type_name(node)}")
        return None

    used = [k for k in node if k in COMBINATORS]
    if used:
        if len(node) > 1:
            problems.append(f"{path or 'the condition'}: a combinator node has exactly "
                            f"one key, got {', '.join(sorted(map(str, node)))}")
            return None
        key = used[0]
        here = f"{path}.{key}" if path else key
        child = node[key]
        if key == NOT:
            # `not` takes ONE node, never a list
            parsed = _parse_node(child, here, depth + 1, problems, counter)
            return None if parsed is None else {NOT: parsed}
        if not isinstance(child, list) or not child:
            problems.append(f"{here}: `{key}` takes a non-empty list of conditions — an "
                            f"empty one is vacuously true and fires on everything")
            return None
        parsed_children = [
            _parse_node(item, f"{here}[{i}]", depth + 1, problems, counter)
            for i, item in enumerate(child)]
        if any(c is None for c in parsed_children):
            return None
        return {key: parsed_children}

    if "field" in node or "op" in node:
        return _parse_leaf(node, path or "the condition", problems)

    problems.append(f"{path or 'the condition'}: not a leaf and not a combinator — "
                    f"expected one of {', '.join(COMBINATORS)} or a "
                    f"`field`/`op`/`value` leaf, got {', '.join(sorted(map(str, node)))}")
    return None


def parse_condition(raw: Any) -> dict[str, Any]:
    """Validate a condition and return it NORMALISED"""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise RulesError([f"the condition is not valid JSON: {e}"]) from e

    problems: list[str] = []
    parsed = _parse_node(raw, "", 1, problems, [0])
    if problems:
        raise RulesError(problems)
    if parsed is None:  # pragma: no cover - unreachable: None always files a problem
        raise RulesError(["the condition could not be parsed"])
    return parsed


def _leaves(cond: Any):
    """Every leaf of a PARSED condition, in document order"""
    if NOT in cond:
        yield from _leaves(cond[NOT])
    elif ALL in cond or ANY in cond:
        for child in cond.get(ALL) or cond.get(ANY):
            yield from _leaves(child)
    else:
        yield cond


def fields_used(cond: Mapping[str, Any]) -> frozenset[str]:
    """The `FACT_FIELDS` names this condition reads"""
    return frozenset(leaf["field"] for leaf in _leaves(cond))


def sources_used(cond: Mapping[str, Any]) -> frozenset[str]:
    """The `FactField.source` slugs this condition reads"""
    return frozenset(FACT_FIELDS[name].source for name in fields_used(cond))


# -- the facts, and the evaluator


@dataclass(frozen=True)
class Facts:
    """One order's recorded facts, as a condition sees them"""

    project: str
    order_id: str
    order_kind: str
    values: Mapping[str, Any]
    now: float


@dataclass(frozen=True)
class LeafResult:
    """One leaf's verdict, and whether its field was recorded at all"""

    field: str
    op: str
    value: Any
    present: bool
    result: bool


@dataclass(frozen=True)
class Evaluation:
    """What the condition decided, and everything needed to explain it"""

    matched: bool
    absent: tuple[str, ...]
    leaves: tuple[LeafResult, ...]


def _eval_leaf(leaf: Mapping[str, Any], facts_: Facts) -> LeafResult:
    """ABSENT IS NOT FALSE, and this function is where that rule lives"""
    name, op, value = leaf["field"], leaf["op"], leaf["value"]
    present = name in facts_.values
    if op == ABSENT:
        return LeafResult(name, op, value, present, not present)
    if op == EXISTS:
        return LeafResult(name, op, value, present, present)
    if not present:
        return LeafResult(name, op, value, False, False)

    actual = facts_.values[name]
    if op == EQ:
        result = _value_matches_type(actual, _type_name(value)) and actual == value
    elif op == NE:
        result = not (_value_matches_type(actual, _type_name(value)) and actual == value)
    elif op == IN:
        result = any(actual == v and _type_name(actual) == _type_name(v) for v in value)
    elif op == NOT_IN:
        result = not any(actual == v and _type_name(actual) == _type_name(v)
                         for v in value)
    elif op in (GTE, LTE):
        if not _value_matches_type(actual, NUM):
            # A recorded value of the wrong type is a defect in the reader, not in the rule
            result = False
        else:
            result = actual >= value if op == GTE else actual <= value
    elif op == COUNT_GTE:
        counts = actual if isinstance(actual, Mapping) else {}
        result = int(counts.get(value["kind"], 0) or 0) >= value["value"]
    else:  # pragma: no cover - `OPERATORS` is closed and parse-checked
        result = False
    return LeafResult(name, op, value, True, result)


def _eval_node(cond: Mapping[str, Any], facts_: Facts,
               out: list[LeafResult]) -> bool:
    if NOT in cond:
        return not _eval_node(cond[NOT], facts_, out)
    if ALL in cond:
        # Every branch is evaluated, no short circuit: the leaf results are the dry run's whole output
        return all([_eval_node(c, facts_, out) for c in cond[ALL]])
    if ANY in cond:
        return any([_eval_node(c, facts_, out) for c in cond[ANY]])
    leaf = _eval_leaf(cond, facts_)
    out.append(leaf)
    return leaf.result


def evaluate(cond: Mapping[str, Any], facts_: Facts) -> Evaluation:
    """Decide a parsed condition against one order's facts"""
    leaves: list[LeafResult] = []
    matched = _eval_node(cond, facts_, leaves)
    absent = tuple(dict.fromkeys(leaf.field for leaf in leaves if not leaf.present))
    return Evaluation(matched=matched, absent=absent, leaves=tuple(leaves))


def matches(cond: Mapping[str, Any], facts_: Facts) -> bool:
    """Whether this condition holds"""
    return evaluate(cond, facts_).matched


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return ", ".join(_render_value(v) for v in value)
    return str(value)


def _render_leaf(leaf: Mapping[str, Any]) -> str:
    name, op, value = leaf["field"], leaf["op"], leaf["value"]
    if op == EQ:
        return f"`{name}` is {_render_value(value)}"
    if op == NE:
        return f"`{name}` is not {_render_value(value)}"
    if op == IN:
        return f"`{name}` is one of {_render_value(value)}"
    if op == NOT_IN:
        return f"`{name}` is none of {_render_value(value)}"
    if op == GTE:
        return f"`{name}` is at least {_render_value(value)}"
    if op == LTE:
        return f"`{name}` is at most {_render_value(value)}"
    if op == EXISTS:
        return f"`{name}` is recorded"
    if op == ABSENT:
        return f"`{name}` is not recorded"
    return (f"`{name}` counts at least {value['value']} of "
            f"{_render_value(value['kind'])}")


def render_condition(cond: Mapping[str, Any]) -> str:
    """The condition as prose, for `jarvis rules show`"""
    if NOT in cond:
        return f"not ({render_condition(cond[NOT])})"
    if ALL in cond:
        return " and ".join(render_condition(c) for c in cond[ALL])
    if ANY in cond:
        return "(" + " or ".join(render_condition(c) for c in cond[ANY]) + ")"
    return _render_leaf(cond)


def explain(cond: Mapping[str, Any], evaluation: Evaluation) -> str:
    """The sentence `jarvis rules dry-run` prints"""
    if evaluation.matched:
        return f"matched: {render_condition(cond)}"
    missing = list(dict.fromkeys(leaf.field for leaf in evaluation.leaves
                                 if not leaf.present and not leaf.result))
    if missing:
        names = " and ".join(f"`{name}` is not recorded on this order"
                             for name in missing)
        return f"did not match because {names}"
    failed = [leaf for leaf in evaluation.leaves if not leaf.result]
    if failed:
        return ("did not match because " + " and ".join(
            "it is not the case that " + _render_leaf(
                {"field": leaf.field, "op": leaf.op, "value": leaf.value})
            for leaf in failed))
    return f"did not match: {render_condition(cond)}"


def facts(store: Any, wo: Mapping[str, Any], *, now: float,
          sources: Any = None) -> Facts:
    """Build one order's `Facts`"""
    from . import ops

    return ops.rule_facts(store, dict(wo), now=now, sources=sources)


# -- the lookup contract


@dataclass(frozen=True)
class Resolution:
    """One thing the registry says could be done about one order, right now"""

    detector: Mapping[str, Any]
    remedy_rule: Mapping[str, Any]
    primitive: str
    params: Mapping[str, Any]
    argument: str
    #: `""` means possible
    can_apply: str | None


def _retired(row: Mapping[str, Any]) -> bool:
    """Whether a remedy row is out of service"""
    return bool(row.get("retired_at")) or row.get("status") == RETRACTED


#: Weakest first
_STRENGTH = {RETRACTED: 0, DRY_RUN: 1, ARMED: 2}


def effective_status(detector: Mapping[str, Any],
                     remedy_rule: Mapping[str, Any]) -> str:
    """The status of a rule as a PAIR: the weaker of detector and remedy row"""
    det = RETRACTED if _retired(detector) else str(detector.get("status") or DRY_RUN)
    rem = RETRACTED if _retired(remedy_rule) else str(
        remedy_rule.get("status") or DRY_RUN)
    return det if _STRENGTH.get(det, 0) <= _STRENGTH.get(rem, 0) else rem


# -- the recurrence lookup and verdict (spec §8)


def recurrence(detectors: Iterable[Mapping[str, Any]], gap_class: str, *,
               project: str = "") -> Mapping[str, Any] | None:
    """The ONE live detector for `gap_class` in `project`'s scope, or `None`"""
    live = [row for row in detectors
            if not _retired(row)
            and str(row.get("gap_class") or "") == gap_class
            and str(row.get("project") or "") in ("", project)]
    if not live:
        return None
    return min(live, key=lambda row: (0 if row.get("project") else 1,
                                      float(row.get("ts") or 0.0),
                                      str(row.get("id") or "")))


def recurrence_verdict(detector: Mapping[str, Any],
                       fire: Mapping[str, Any] | None) -> str:
    """Which half of the rule did not hold"""
    if _retired(detector) or str(detector.get("status") or DRY_RUN) != ARMED:
        return NOT_ARMED
    outcome = str((fire or {}).get("outcome") or "")
    if outcome in (PROPOSED, REFUSED, APPLIED):
        return REMEDY_FAILED
    if outcome == UNREADABLE:
        return UNREADABLE_RECURRENCE
    return MISSED


def recurrence_comment(*, order_id: str, gap_class: str, verdict: str,
                       detector_id: str) -> str:
    """The comment that goes on the ORIGINAL public issue"""
    return (
        "**This gap recurred: the OS already had a rule for it and the rule did not "
        "hold.**\n\n"
        f"- gap class: `{gap_class}`\n"
        f"- detector: `{detector_id}`\n"
        f"- verdict: `{verdict}`\n"
        f"- seen again on: `{order_id}`\n\n"
        "Filed by Jarvis against the original issue rather than as a new one, so the "
        "gap stays one thread.\n")


def readable_detectors(
    detectors: Iterable[Mapping[str, Any]],
) -> tuple[list[tuple[Mapping[str, Any], dict[str, Any]]],
           list[tuple[str, RulesError]]]:
    """Split detector rows into `(row, parsed condition)` pairs and the unreadable ones"""
    readable: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    unreadable: list[tuple[str, RulesError]] = []
    for row in detectors:
        try:
            readable.append((row, parse_condition(row.get("condition"))))
        except RulesError as e:
            unreadable.append((str(row.get("id") or ""), e))
    return readable, unreadable


def _rows_for(remedy_rules: Any, detector_id: str) -> list[Mapping[str, Any]]:
    """The remedy rows belonging to one detector"""
    if isinstance(remedy_rules, Mapping):
        return list(remedy_rules.get(detector_id) or ())
    return [r for r in remedy_rules if r.get("detector_id") == detector_id]


def _declares_can_apply(primitive: str) -> bool:
    """Whether anything can answer "could this remedy run right now" for this primitive"""
    remedy = remedies.REMEDIES.get(primitive)
    return remedy is not None and getattr(remedy, "can_apply", True) is not None


def resolve(
    detectors: Iterable[Mapping[str, Any]],
    remedy_rules: Any,
    facts_: Facts,
    *,
    precondition: Callable[[str, Mapping[str, Any]], str] | None = None,
) -> tuple[Resolution, ...]:
    """What the registry says could be done about this order right now"""
    out: list[Resolution] = []
    readable, _unreadable = readable_detectors(detectors)
    for row, cond in readable:
        if not matches(cond, facts_):
            continue
        for remedy_row in _rows_for(remedy_rules, str(row.get("id") or "")):
            if _retired(remedy_row):
                continue
            primitive = str(remedy_row.get("primitive") or "")
            params = remedy_row.get("params") or {}
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = {}
            can_apply: str | None = None
            if precondition is not None and _declares_can_apply(primitive):
                can_apply = precondition(primitive, params)
            out.append(Resolution(
                detector=row, remedy_rule=remedy_row, primitive=primitive,
                params=params, argument=str(remedy_row.get("argument") or ""),
                can_apply=can_apply))
    return tuple(out)


#: The refusal when a fix order names no order its detector can be proved against (docs/superpowers/specs/2026-09-27-self-evolution.md)
NO_TRIGGER_REFUSAL = (
    "gap_class '{gap}' has no triggering order to prove its detector against: "
    "trigger_wo_id is empty and the improvement-order refs resolve to no work order in "
    "this project. Re-file with `jarvis wo create --trigger-wo-id <wo-id>`, naming the "
    "order the investigation was filed from."
)

#: The refusal when a fix order NAMES a triggering order this project's records do not have (docs/superpowers/specs/2026-09-27-self-evolution.md)
TRIGGER_MISSING_REFUSAL = (
    "gap_class '{gap}' names triggering order '{missing}' through `--trigger-wo-id`, and "
    "this project's records do not have it. That id is the submitter's own input, not a "
    "record the OS lost, so this is a REFUSAL and not an unreadable record: nothing has "
    "shown the detector fires, because there is no order to fire it against. Re-file with "
    "`jarvis wo create --trigger-wo-id <wo-id>`, naming an order this project has."
)


def note_unproven(sink: list[str] | None, note: str) -> None:
    """Record one check that PASSED WITHOUT BEING EVALUATED, for the packet to state"""
    if sink is not None:
        sink.append(note)


def _check_4_note(remedy_row: Mapping[str, Any], primitive: str, why: str) -> str:
    """One sentence for check 4 passed unevaluated, shared by both arms that can produce it here"""
    return (f"check 4, that remedy rule '{remedy_row.get('id') or ''}'s `{primitive}` "
            f"can actually run against the triggering order, was not run: {why}.")

# Stage numbers, and the LARGEST wins when several detectors each fail differently
_STAGE_NO_DETECTOR = 1
_STAGE_UNREADABLE = 2
_STAGE_NO_REMEDY = 3
_STAGE_BAD_PRIMITIVE = 4
_STAGE_NO_MATCH = 5
_STAGE_CANNOT_APPLY = 6


def precheck(
    detectors: Iterable[Mapping[str, Any]],
    remedy_rules: Any,
    wo: Mapping[str, Any],
    trigger_facts: Facts | None,
    *,
    precondition: Callable[[str, Mapping[str, Any]], str] | None = None,
    trigger_unreadable: str = "",
    trigger_missing: str = "",
    unproven: list[str] | None = None,
) -> str | None:
    """Why this fix order may NOT be submitted for validation yet, or `None` (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
    gap = str((wo or {}).get("gap_class") or "").strip()
    if not gap:
        return None
    named_remedy = str((wo or {}).get("remedy_rule_id") or "")

    live = [row for row in (detectors or ()) if not _retired(row)]
    if not live:
        return (f"gap_class '{gap}' has no registered detector: nothing in the registry "
                f"recognises this gap, so the rule this fix order exists to add has not "
                f"been written down. Register the detector, then name it with "
                f"`jarvis wo create --detector-id <detector-id>` and submit again.")

    readable, unreadable = readable_detectors(live)
    best: tuple[int, str] | None = None

    def refuse(stage: int, message: str) -> None:
        nonlocal best
        if best is None or stage > best[0]:
            best = (stage, message)

    for det_id, error in unreadable:
        # An unreadable stored condition is a REFUSAL and says so
        refuse(_STAGE_UNREADABLE,
               f"detector '{det_id}' has a stored condition this release cannot parse "
               f"({error}), so it cannot be proved against anything. Re-register it with "
               f"a condition this release understands, then submit again.")

    for row, cond in readable:
        det_id = str(row.get("id") or "")
        candidates = [r for r in _rows_for(remedy_rules, det_id) if not _retired(r)]
        if named_remedy:
            candidates = [r for r in candidates if str(r.get("id") or "") == named_remedy]
            if not candidates:
                refuse(_STAGE_NO_REMEDY,
                       f"remedy rule '{named_remedy}' is not a live rule under detector "
                       f"'{det_id}': the order names a remedy the registry does not have. "
                       f"Register it, or drop `--remedy-rule-id`, then submit again.")
                continue
        if not candidates:
            refuse(_STAGE_NO_REMEDY,
                   f"detector '{det_id}' has no live remedy rule: a detector with nothing "
                   f"to do about the gap cannot close it. Register one naming a primitive "
                   f"from the closed registry "
                   f"({', '.join(sorted(remedies.REMEDIES))}), then submit again.")
            continue
        usable = [r for r in candidates
                  if str(r.get("primitive") or "") in remedies.REMEDIES]
        if not usable:
            # NAME THE PRIMITIVE THAT DOES NOT EXIST — the spec asks for that word specifically
            refuse(_STAGE_BAD_PRIMITIVE, "; ".join(
                f"remedy rule '{r.get('id') or ''}' names primitive "
                f"'{r.get('primitive') or ''}', which does not exist — a rule names one "
                f"of {', '.join(sorted(remedies.REMEDIES))}, and the registry is closed"
                for r in candidates))
            continue

        if trigger_missing:
            # NAMED AND NOT THERE: the submitter's own input, so a refusal (§7.2)
            refuse(_STAGE_NO_MATCH,
                   TRIGGER_MISSING_REFUSAL.format(gap=gap, missing=trigger_missing))
            continue
        if trigger_unreadable:
            # Checks 1 and 2 passed and check 3 cannot be run: UNREADABLE, not a refusal
            note_unproven(unproven,
                      f"check 3, that detector '{det_id}'s condition actually fires on "
                      f"the triggering order's recorded state, was not run: "
                      f"{trigger_unreadable}.")
            return None
        if trigger_facts is None:
            refuse(_STAGE_NO_MATCH, NO_TRIGGER_REFUSAL.format(gap=gap))
            continue
        evaluation = evaluate(cond, trigger_facts)
        if not evaluation.matched:
            refuse(_STAGE_NO_MATCH,
                   f"detector '{det_id}' did not fire on {trigger_facts.order_id}, the "
                   f"order it is proved against: {explain(cond, evaluation)}.")
            continue

        for remedy_row in usable:
            primitive = str(remedy_row.get("primitive") or "")
            params = remedy_row.get("params") or {}
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except ValueError:
                    params = {}
            sentence: str | None = None
            if precondition is None:
                # (iv) Nothing was injected to ask — `resolve`'s storeless callers and every unit test
                note_unproven(unproven, _check_4_note(
                    remedy_row, primitive,
                    "this caller injected no `can_apply` seam, so nothing could answer"))
            elif not _declares_can_apply(primitive):
                # (iii) The primitive declares none — the other half of kn-b7deec8f
                note_unproven(unproven, _check_4_note(
                    remedy_row, primitive,
                    f"primitive '{primitive}' declares no `can_apply`"))
            else:
                sentence = precondition(primitive, params)
            if sentence:
                # VERBATIM
                refuse(_STAGE_CANNOT_APPLY, sentence)
                continue
            return None

    return best[1] if best else None


def validate_params(primitive: str, params: Mapping[str, Any]) -> list[str]:
    """Why this remedy row may not be inserted, or `[]`"""
    problems: list[str] = []
    remedy = remedies.REMEDIES.get(primitive)
    if remedy is None:
        return [f"unknown primitive {primitive!r} — a rule names one of "
                f"{', '.join(sorted(remedies.REMEDIES))}, and the registry is closed"]

    schema = _param_specs(getattr(remedy, "params", None))
    if not schema:
        return problems

    for name, value in dict(params).items():
        if name not in schema:
            problems.append(f"unknown parameter {name!r} for `{primitive}`")
            continue
        expected = schema[name][0]
        if expected and not _value_matches_type(value, expected):
            problems.append(f"`{primitive}` parameter {name!r} must be {expected}, got "
                            f"{_type_name(value)}")
    for name, (_expected, required) in schema.items():
        if required and name not in params:
            problems.append(f"`{primitive}` requires the {name!r} parameter")
    return problems


def _param_specs(schema: Any) -> dict[str, tuple[str, bool]]:
    """One primitive's parameter schema as `name -> (type, required)`, whatever shape it arrived in"""
    if not schema:
        return {}
    if isinstance(schema, Mapping):
        out: dict[str, tuple[str, bool]] = {}
        for name, spec in schema.items():
            if isinstance(spec, Mapping):
                out[str(name)] = (str(spec.get("type") or ""),
                                  bool(spec.get("required")))
            else:
                out[str(name)] = (str(getattr(spec, "type", "") or ""),
                                  bool(getattr(spec, "required", False)))
        return out
    try:
        items = list(schema)
    except TypeError:  # pragma: no cover - a schema that is neither map nor iterable
        return {}
    out = {}
    for spec in items:
        name = getattr(spec, "name", None)
        if not name:
            continue
        out[str(name)] = (str(getattr(spec, "type", "") or ""),
                          bool(getattr(spec, "required", False)))
    return out


# -- the seeds

#: `detectors.seed_version` is INTEGER, so this is an int and not `gate_rules`' string
SEED_VERSION = 2

#: `invariants.VALIDATION_STUCK_BLOCKER`, INLINED
VALIDATION_STUCK_BLOCKER = ("the review could not be satisfied — the work needs your "
                            "judgement")

#: Where the issues that discovered these gaps live
_TRACKER = "https://github.com/gonandrap/agentic_os/issues"


def seed_id(prefix: str, body: Any) -> str:
    """A stable id for a seeded row"""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha1(f"{prefix}|{canonical}".encode()).hexdigest()
    return f"{prefix}-{digest[:10]}"


# The gaps of §1, each one a real incident with an issue behind it
_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "gap_class": "stale-panel-hold",
        "issue_url": f"{_TRACKER}/786",
        "summary": ("a `needs_review` order still flagged as unsatisfiable by the panel "
                    "when its newest round has since PASSED (issues #786, #813)"),
        "condition": {"all": [
            {"field": "status", "op": "eq", "value": "needs_review"},
            {"field": "needs_attention", "op": "eq", "value": True},
            # The literal is `invariants.VALIDATION_STUCK_BLOCKER`, inlined above for the leaf reason given there
            {"field": "attention_reason", "op": "eq",
             "value": VALIDATION_STUCK_BLOCKER},
            {"field": "round_outcome", "op": "eq", "value": "passed"},
            {"field": "seconds_in_status", "op": "gte", "value": 3600},
        ]},
        "remedies": [
            {"primitive": "lower_attention", "params": {},
             "argument": ("The attention flag on this order says the review could not be "
                          "satisfied, but the newest validation round PASSED and the "
                          "order has stood like this for over an hour. The flag is "
                          "stale: it is asking the user for a judgement the panel has "
                          "already made. Lower it and leave the order where it is.")},
            {"primitive": "drop_hold", "params": {},
             "argument": ("The validation hold on this order outlived the round that "
                          "opened it — that round PASSED. Nothing is waiting on the "
                          "panel any more, so the hold is reporting a wait that is over "
                          "and is what every surface shows the user instead of the real "
                          "state.")},
        ],
    },
    {
        "gap_class": "unreachable-neo-question",
        "issue_url": f"{_TRACKER}/788",
        "summary": ("a `waiting_input` order whose Neo question row is `failed` with no "
                    "attempt spent — the question was never actually asked (issue #788)"),
        "condition": {"all": [
            {"field": "status", "op": "eq", "value": "waiting_input"},
            {"field": "neo_question_status", "op": "eq", "value": "failed"},
            {"field": "neo_question_attempts", "op": "lte", "value": 0},
        ]},
        "remedies": [
            {"primitive": "retry_neo_question", "params": {},
             "argument": ("This order is waiting on a Neo question that is recorded "
                          "`failed` with ZERO attempts spent: the reviewer was never "
                          "reached, so nothing was decided and no answer exists. Ask it "
                          "again. The one thing that must not happen is the failure "
                          "being read as an answer — the OS never fabricates a default "
                          "answer from a failure to get one, and an unreachable reviewer "
                          "is exactly that failure.")},
        ],
    },
    {
        "gap_class": "catch-up-round-burn",
        "issue_url": f"{_TRACKER}/806",
        "summary": ("a parked order whose auto-merge is held on `sha_moved` while its "
                    "newest round PASSED — the judged head is behind the PR head "
                    "(issue #806)"),
        "condition": {"all": [
            {"field": "status", "op": "eq", "value": "waiting_pr_merge"},
            # `automerge.HELD_SHA_MOVED`
            {"field": "automerge_code", "op": "eq", "value": "sha_moved"},
            {"field": "round_outcome", "op": "eq", "value": "passed"},
            {"field": "judged_head_sha", "op": "exists"},
        ]},
        "remedies": [
            {"primitive": "carry_verdict", "params": {},
             "argument": ("The pull request's head moved after the panel judged it, so "
                          "the auto-merge is held on `sha_moved` even though the round "
                          "PASSED. Carry the existing verdict forward to the new head "
                          "using the CATCH-UP PROOF THAT ALREADY SHIPS — the one the "
                          "auto-merge path uses to decide a moved head is a catch-up and "
                          "not new work. Never a second implementation of it: two "
                          "proofs of the same property drift, and the one that drifts is "
                          "the one deciding whether unreviewed code merges "
                          "(kn-907c9a61).")},
        ],
    },
    {
        "gap_class": "red-base-inherited",
        "issue_url": f"{_TRACKER}/793",
        "summary": ("a parked order whose auto-merge is held on `base_red` — it inherited "
                    "a broken base branch and will wait for ever (issue #793)"),
        "condition": {"all": [
            {"field": "status", "op": "eq", "value": "waiting_pr_merge"},
            # `automerge.HELD_BASE_RED`, quoted for the leaf reason and pinned by test
            {"field": "automerge_code", "op": "eq", "value": "base_red"},
            {"field": "seconds_in_status", "op": "gte", "value": 1800},
        ]},
        "remedies": [
            {"primitive": "update_branch", "params": {},
             "argument": ("The auto-merge on this order is held because the BASE branch "
                          "was red when its checks ran — a failure it inherited and did "
                          "not cause. Base has since moved; update the branch so the "
                          "checks run against what is actually on it. Nothing about the "
                          "order's own change is in question, and without this it waits "
                          "for a green it can never reach on its own.")},
        ],
    },
    {
        "gap_class": "overtaken-release-order",
        "issue_url": f"{_TRACKER}/784",
        "summary": ("an open release order whose release has already landed, which "
                    "INV-WORK-LANDED has already detected (issue #784)"),
        # THE SHAPE TO COPY
        "condition": {"all": [
            {"field": "status", "op": "in", "value": ["pending", "running"]},
            {"field": "invariant_events", "op": "count_gte",
             "value": {"kind": "INV-WORK-LANDED", "value": 1}},
            {"field": "seconds_since_activity", "op": "gte", "value": 3600},
        ]},
        "remedies": [
            # `invariants.true_blockers` owns `attention_reason`
            {"primitive": "raise_attention",
             "params": {"reason_key": "overtaken-release-order"},
             "argument": ("The release this order would ship has already landed: close "
                          "it with `jarvis wo done`. "
                          "INV-WORK-LANDED has already recorded that the work this order "
                          "would ship is on the default branch: it was overtaken, and it "
                          "has done nothing for an hour. Nothing is wrong and nothing "
                          "needs fixing — it needs CLOSING, by the user, with `jarvis wo "
                          "done`. Say exactly that on the attention line so the user "
                          "types one command instead of opening the order to work out "
                          "why it is idle.")},
        ],
    },
    {
        # Spec §5: docs/superpowers/specs/ 2026-10-14-a-killed-validation-panel-opens-an-investigation.md
        "gap_class": "killed-validation-panel",
        "issue_url": f"{_TRACKER}/1039",
        "summary": ("a round whose validation seats were KILLED BY A SIGNAL more than "
                    "once on the same order — the kill is recurring, not a blip "
                    "(issue #1039)"),
        # THE CAUSE, not the event kind: transport
        "condition": {"all": [
            {"field": "validation_failure_causes", "op": "count_gte",
             "value": {"kind": "killed", "value": 2}},
        ]},
        "remedies": [
            {"primitive": "file_work_order", "params": {},
             "argument": ("The validation seats on this order have been killed by a "
                          "signal more than once: four concurrent processes ending at "
                          "the same elapsed time with nothing on either stream is one "
                          "EXTERNAL event — a daemon restart or deploy, a process-group "
                          "kill, the OOM killer, an outer timeout — and not four model "
                          "failures. A retry cannot name what killed them and cannot "
                          "stop it recurring, which is why the remedy here writes code "
                          "rather than flipping a state. The investigation already filed "
                          "on the order carries the per-seat facts; what this orders is "
                          "the fix for whatever it finds.")},
        ],
    },
)


def seed_rows() -> list[dict[str, Any]]:
    """The builtin detectors with their remedy rows, as store rows"""
    rows: list[dict[str, Any]] = []
    for seed in _SEEDS:
        condition = json.dumps(seed["condition"], sort_keys=True,
                               separators=(",", ":"))
        detector_id = seed_id("dt", [seed["gap_class"], condition])
        detector = {
            "id": detector_id,
            "gap_class": seed["gap_class"],
            "project": "",                 # fleet-wide: these are OS gaps, not a project's
            "subjects": DEFAULT_SUBJECT,
            "condition": condition,
            "summary": seed["summary"],
            "status": DRY_RUN,             # always; see §3.3
            "source": "builtin",
            "issue_url": seed["issue_url"],
            "seed_version": SEED_VERSION,
        }
        remedy_rows = [{
            "id": seed_id("rm", [detector_id, r["primitive"],
                                 json.dumps(r["params"], sort_keys=True)]),
            "detector_id": detector_id,
            "primitive": r["primitive"],
            "params": json.dumps(r["params"], sort_keys=True, separators=(",", ":")),
            "argument": r["argument"],
            "status": DRY_RUN,
        } for r in seed["remedies"]]
        rows.append({"detector": detector, "remedies": remedy_rows})
    return rows
