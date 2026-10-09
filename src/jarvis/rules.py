"""The registry the OS uses to recognise its own recurring gaps — grammar and evaluator.

docs/superpowers/specs/2026-09-27-self-evolution.md §3. This module is the LEAF of that
feature: the condition grammar, the pure evaluator, the `Facts` shape, the lookup
contract `resolve`, and the five seed rows. It holds no store, fires nothing and acts on
nothing.

## Why a registry at all, rather than another invariant

The OS already detects things about itself: `invariants.py` is a closed set of checks,
each one a function, each one shipped in a release. That is the right shape for a rule
nobody argues with — a completed work order's pull request must have merged — and the
wrong shape for the thing this feature is about. The five gaps in §1 were all found the
same way: a work order sat in a state nobody was looking at, somebody eventually noticed,
filed an issue, and the fix took a release. The knowledge that "a `needs_review` order
whose newest round has since PASSED is stuck, not judged" existed the moment issue #786
was written; it reached the running fleet weeks later.

A rule in a TABLE closes that distance. It is data, it can be written by an improvement
order's accepted finding, it starts in `dry_run` where being wrong is free, and a person
arms it once its hit history says it is right. What it may never become is a second
invariant engine — **a detector may not duplicate an invariant.** Where an invariant
already detects a condition, the rule keys off the invariant's own timeline event and
contributes the REMEDY. Seed rule 5 (`overtaken-release-order`) is that shape, and it is
the shape to copy.

## Why the grammar is a JSON document and not an expression

A condition is data: combinators, a closed field table, a closed operator table. It is
not a code string, not a Python path, not a lambda, and nothing evaluates it with `eval`
in any spelling. Two reasons, and the second is the one that decided it.

A condition must be reviewable by a person in ONE reading — `jarvis rules show` renders
it as prose, and a reviewer arming a rule is authorising what it will eventually DO. And
`gate_rules.validate_pattern` is the cautionary tale: that registry admitted regexes,
which meant a reviewer could answer a false positive with `git.*` and switch a gate off
without anybody reading it as that. A whole function exists there to fence a power that
was granted before anyone asked what it cost. This registry does not admit regexes and
does not admit free-text operators, so it needs no fence.

The field table is likewise CLOSED, and that is the engine's only extension point that
needs code. A field nobody implemented is a write-time refusal, never a silent `None`
that makes a condition vacuously false.

## Absent is not false

The single rule most likely to be got wrong by anyone editing the evaluator. A field
missing from `Facts.values` makes every operator FALSE except `absent`, and the
evaluator records WHICH fields were missing so `jarvis rules dry-run` can say "this did
not match because `hold_cause` is not recorded on this order" rather than merely "no".
A condition that matches only because a field is missing is the commonest way a rule
over-fires, and the dry-run output is where that gets caught.

## What this module is NOT

No store, no model, no clock it was not handed, no firing, no arming. `facts()` is
DECLARED here and its body belongs to the evaluation pass — the signature is the contract
between two sections built in parallel, neither able to see the other's text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from . import remedies

# -- vocabulary -----------------------------------------------------------------------

#: A detector's lifecycle. `dry_run -> armed`, `armed -> dry_run` (the disarm interlock),
#: and either of them `-> retracted`, which is terminal. NOTHING IN THIS MODULE WRITES
#: ANY OF THEM: arming is a person's act and lives with the store methods (spec §3.3).
DRY_RUN = "dry_run"
ARMED = "armed"
RETRACTED = "retracted"
STATUSES = (DRY_RUN, ARMED, RETRACTED)

#: The two modes a fire can happen in — a `rule_fires.mode` value. `retracted` is absent
#: on purpose: a retracted detector is not evaluated, so it has no mode.
MODES = (DRY_RUN, ARMED)

#: Who wrote the rule. `io` is an improvement order's accepted finding, which is the
#: route this whole feature exists to open; `builtin` is `seed_rows` below.
SOURCES = ("builtin", "io", "user")

#: What a detector can be evaluated against. `remedies.Remedy.subjects` uses the same two
#: words for the same two things, and they must stay the same words — a remedy declaring
#: `feature_order` and a detector spelling it otherwise silently never pairs.
SUBJECTS = ("work_order", "feature_order")
DEFAULT_SUBJECT = "work_order"

#: `rule_fires.outcome`. SIX values that must not be collapsed into "it fired" (spec
#: §3.1). The enum ships complete from this section even though only the firing pass ever
#: writes three of them: a value a later child adds to a column a shipped release already
#: reads is a migration, and there is no reason to buy one.
RECORDED = "recorded"      # a dry run: the condition held, nothing was proposed
PROPOSED = "proposed"      # an armed fire that raised an alarm
APPLIED = "applied"        # one whose remedy the gate then let run
REFUSED = "refused"        # the remedy path declined — allow-list, grant, precondition
UNREADABLE = "unreadable"  # something could not be READ; nothing was decided
CLEARED = "cleared"        # the fire closed because the condition no longer holds
FIRE_OUTCOMES = (RECORDED, PROPOSED, APPLIED, REFUSED, UNREADABLE, CLEARED)

#: `rule_recurrences.verdict`: WHICH HALF OF THE RULE did not hold when a gap the OS
#: already has a detector for happened again (spec §8). Four values, not the three the
#: spec's own DDL comment listed — Neo question 974 added `unreadable`, because an armed
#: detector whose fire says "nothing could be READ" decided nothing, and calling that
#: `missed` would claim the condition was evaluated and came back false. The pinned
#: ruling is that the OS never fabricates a default answer from a failure to find one,
#: and a verdict is the thing a finding is filed against, so the wrong one sends the fix
#: at the wrong half.
#:
#: `UNREADABLE_RECURRENCE` reuses the `UNREADABLE` string on purpose: one word means one
#: thing across both columns, and a reader comparing a fire's outcome with a recurrence's
#: verdict should not have to translate. The NAME differs only because `UNREADABLE` is
#: already bound to the fire outcome in this module's namespace.
MISSED = "missed"
REMEDY_FAILED = "remedy_failed"
NOT_ARMED = "not_armed"
UNREADABLE_RECURRENCE = UNREADABLE
RECURRENCE_VERDICTS = (NOT_ARMED, MISSED, REMEDY_FAILED, UNREADABLE_RECURRENCE)

#: How deep and how wide a condition may be. Not a performance limit — the documents are
#: tiny. It is a REVIEWABILITY limit: a person arming a rule is authorising what it will
#: do, and a nine-level document is one nobody reads before clicking.
MAX_DEPTH = 4
MAX_NODES = 32

#: Every snapshot text stored on a fire is bounded at this, with the cap recorded in the
#: text, the way every other payload in this codebase is (`supervisor._clip`,
#: `live.PARAMS_CAP`). A truncated payload that does not say it was truncated is read as
#: the whole thing.
FACTS_CHARS = 4000


def bound(text: str, limit: int = FACTS_CHARS) -> str:
    """`text`, truncated to `limit`, SAYING SO when it was.

    The marker names the number rather than just `[…]`: a reader deciding whether the
    missing tail matters needs to know how much of it there is, and a reader debugging a
    detector needs to know the cut was a cap and not the source.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" […truncated at {limit} characters]"


# -- the grammar ----------------------------------------------------------------------

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

# THERE IS DELIBERATELY NO REGEX OPERATOR, no free-text operator, no expression
# evaluator, and no `eval` in any spelling — not `eval`, not `exec`, not `compile`, not
# an `ast.literal_eval` over something a rule author wrote.
#
# `gate_rules.validate_pattern` is why. That registry DID admit regexes, and a whole
# validation function had to be written afterwards to stop a reviewer answering a false
# positive with `.*` and switching a gate off without anyone reading it as that. The
# power was granted before the question "what does this cost when it is wrong" was asked.
# Here the answer is that the question cannot arise: nothing in `OPERATORS` can express
# more than a comparison against a closed field.

#: The four types a fact can have. `map` is a `{key: int}` counter — the only compound
#: type, and `count_gte` is the only operator over it.
STR = "str"
NUM = "num"
BOOL = "bool"
MAP = "map"


@dataclass(frozen=True)
class FactField:
    """One thing a condition may ask about an order.

    `source` is a SLUG naming which reader supplies the value, not the reader itself:
    this module is a leaf and may not import `ops`, `holds`, `invariants` or a store. The
    slug is what lets the evaluation pass build a snapshot LAZILY — `sources_used(cond)`
    answers "which readers does this condition actually need", so a rule asking only
    about `status` never costs a `latest_validation_round` query.

    `note` carries the part that cannot be expressed in the type: the vocabulary a `str`
    field draws from, and where a value must NOT come from.
    """

    name: str
    type: str
    source: str
    note: str = ""


def _fields(*rows: tuple[str, str, str, str]) -> dict[str, FactField]:
    return {name: FactField(name, type_, source, note)
            for name, type_, source, note in rows}


#: THE CLOSED TABLE. A field not here is a write-time refusal (spec §3.2) — never a
#: silent `None` that makes a condition vacuously false. Adding one is a reviewed diff
#: here PLUS a reader in the evaluation pass, in that order, and that friction is the
#: point: it is the only extension point of the engine that needs code.
FACT_FIELDS: dict[str, FactField] = _fields(
    # The `work_orders` row itself. The cheapest source there is — the caller already
    # holds the row — which is why most conditions lead with `status`.
    ("status", STR, "work_order", "the work order's status column"),
    ("kind", STR, "work_order", "`worker`, `planner`, `analyst` — the WO_KINDS set"),
    ("hidden", BOOL, "work_order", "true when the user decluttered it out of listings"),
    ("needs_attention", BOOL, "work_order", "the attention FLAG, not the reason"),
    ("attention_reason", STR, "work_order",
     "one of `invariants.true_blockers`' own sentences. Nothing else may write it, so a "
     "condition quoting one is quoting that function"),

    # `ops.state_durations`. Everything here is derived from the timeline, so it is the
    # source that makes "has been like this for an hour" expressible at all.
    ("seconds_in_status", NUM, "state_durations", "since the last status change"),
    ("seconds_in_status_active", NUM, "state_durations",
     "since the last status change, minus every interval `holds` says the order was not "
     "permitted to run"),
    ("seconds_since_activity", NUM, "state_durations", "since the newest timeline event"),
    ("lifetime_seconds", NUM, "state_durations", "since the order was created"),
    ("last_activity_kind", STR, "state_durations", "the newest timeline event's kind"),

    # `holds.held` / `holds.by_cause`. The VOCABULARY is `holds.HOLD_CAUSES` — named
    # here rather than imported, because this module is a leaf and `holds` sits above it.
    # A condition naming a cause that table does not have is legal grammar and will
    # simply never match; the dry run is where that shows up.
    ("hold_cause", STR, "holds",
     "the merged open hold's cause — vocabulary `holds.HOLD_CAUSES`"),
    ("hold_seconds", NUM, "holds", "how long that hold has been open"),

    # The `automerge.HELD_*` set: why the validated auto-merge did not merge.
    ("automerge_code", STR, "automerge", "one of the `automerge.HELD_*` constants"),

    ("waiting_on", STR, "waiting_on", "`ops.waiting_on`'s slug for what it is waiting on"),

    # `ProjectStore.latest_validation_round`.
    ("round_no", NUM, "validation_round", "the newest round's number"),
    ("round_outcome", STR, "validation_round", "`passed`, `rejected` or `escalated`"),
    ("rounds_left", NUM, "validation_round", "rounds remaining before `max_rounds`"),
    ("judged_head_sha", STR, "validation_round", "the commit that round judged"),

    # The RECORDED pull-request columns. NEVER A LIVE `gh` CALL: a condition is evaluated
    # for every open order on every sweep, and a rule engine that reaches the network per
    # order per tick is a rate limit with extra steps. The recorded columns are refreshed
    # by the poller that already owns that job.
    ("pr_url", STR, "pull_request", "the recorded column; never a live `gh` call"),
    ("pr_state", STR, "pull_request", "the recorded column; never a live `gh` call"),

    # `ProjectStore.count_events` — `{event kind: count}`.
    ("event_counts", MAP, "event_counts", "timeline event kinds to their counts"),
    # The `invariant` timeline events — THE ROUTE a detector takes when an invariant
    # already detects the condition. See seed rule 5.
    ("invariant_events", MAP, "invariant_events", "invariant names to their counts"),

    ("depends_on_count", NUM, "dependencies", "edges this order waits on"),
    ("dead_dependency_count", NUM, "dependencies",
     "edges that can never clear — `invariants.dead_dependencies`"),

    ("neo_question_status", STR, "neo_question", "the row's status; `invariants.awaiting_neo`"),
    ("neo_question_attempts", NUM, "neo_question", "attempts already spent on it"),

    ("budget_usd", NUM, "budget", "the ceiling, or absent when there is none"),
    ("spent_usd", NUM, "budget", "the whole bill the ceiling governs"),
)

#: Which operators each field type admits, enforced at PARSE time rather than at
#: evaluation time. A condition that can never be true is a bug in the rule, and the
#: moment to report it is when somebody writes it, not six hours into a dry run that
#: never fires.
_OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    STR: (EQ, NE, IN, NOT_IN, EXISTS, ABSENT),
    NUM: (EQ, NE, IN, NOT_IN, GTE, LTE, EXISTS, ABSENT),
    BOOL: (EQ, NE, IN, NOT_IN, EXISTS, ABSENT),
    MAP: (COUNT_GTE, EXISTS, ABSENT),
}


class RulesError(Exception):
    """A condition that cannot be accepted. Carries EVERY problem, not the first.

    The house shape of `plans.PlanError` and `findings` (spec §3.2), and for the same
    reason: both readers of this parser — a person pasting a condition into `jarvis
    rules`, and a model proposing one from an improvement order's finding — re-submit per
    error message, so stopping at the first problem costs a round trip per line that
    could have been reported together.
    """

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
    """Whether a literal may stand beside a field of type `expected`.

    `bool` is checked BEFORE `num` everywhere in this module, because Python says
    `isinstance(True, int)` and a rule reading `{"field": "hidden", "op": "eq", "value":
    1}` would otherwise be accepted and then read by a person as something it is not.
    """
    if expected == BOOL:
        return isinstance(value, bool)
    if expected == NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == STR:
        return isinstance(value, str)
    return False


def _parse_leaf(node: Mapping[str, Any], path: str,
                problems: list[str]) -> dict[str, Any] | None:
    """One `{"field", "op", "value"}` node, normalised, or None when it is unusable.

    Returns None rather than raising so the walk continues and the caller collects the
    REST of the document's problems too.
    """
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
        # `value` must be absent or null: a value here means the author believed the
        # operator compared something, and accepting it would confirm that belief.
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
            # `not` takes ONE node, never a list. A list would need an implied
            # combinator between its members, and an implied combinator is exactly the
            # kind of thing a reviewer reads past.
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
    """Validate a condition and return it NORMALISED. Raises `RulesError`.

    `raw` is either the JSON text as the `detectors.condition` column stores it, or an
    already-decoded object. Both, because this runs in two places: on INSERT, so nothing
    unvalidated reaches the table, and again on READ, because a row written by an older
    release may name a field a later one removed. A row that fails on read is reported
    `UNREADABLE` and never evaluated — see `readable_detectors`.

    Every problem is reported at once, each naming a JSON path (`all[1].field`) so the
    author can find it in a document they did not necessarily lay out.
    """
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
    """Every leaf of a PARSED condition, in document order."""
    if NOT in cond:
        yield from _leaves(cond[NOT])
    elif ALL in cond or ANY in cond:
        for child in cond.get(ALL) or cond.get(ANY):
            yield from _leaves(child)
    else:
        yield cond


def fields_used(cond: Mapping[str, Any]) -> frozenset[str]:
    """The `FACT_FIELDS` names this condition reads."""
    return frozenset(leaf["field"] for leaf in _leaves(cond))


def sources_used(cond: Mapping[str, Any]) -> frozenset[str]:
    """The `FactField.source` slugs this condition reads.

    THIS IS WHAT LETS THE EVALUATION PASS BUILD THE SNAPSHOT LAZILY. Without it every
    sweep would derive every field of every open order — a `latest_validation_round`
    query, a hold merge and a `count_events` per order per tick — to answer a condition
    that asked about `status`. Exposed as a function rather than re-derived from the
    field names at the call site, so the field-to-reader mapping lives in exactly one
    place: `FACT_FIELDS`.
    """
    return frozenset(FACT_FIELDS[name].source for name in fields_used(cond))


# -- the facts, and the evaluator ------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """One order's recorded facts, as a condition sees them.

    `values` holds ONLY the fields actually recorded for this order. A field that is not
    there is not `None` and not `False` — it is NOT RECORDED, which is a third thing, and
    the evaluator treats it as one.

    `now` is handed in rather than read, so the same `Facts` gives the same answer for
    ever. A dry run replayed tomorrow against yesterday's snapshot must produce
    yesterday's verdict, or the dry-run history is not evidence.
    """

    project: str
    order_id: str
    order_kind: str
    values: Mapping[str, Any]
    now: float


@dataclass(frozen=True)
class LeafResult:
    """One leaf's verdict, and whether its field was recorded at all.

    `present` is separate from `result` because `False` answers two different questions
    — "the value did not match" and "there was no value" — and `explain` must be able to
    tell the reader which one happened.
    """

    field: str
    op: str
    value: Any
    present: bool
    result: bool


@dataclass(frozen=True)
class Evaluation:
    """What the condition decided, and everything needed to explain it."""

    matched: bool
    absent: tuple[str, ...]
    leaves: tuple[LeafResult, ...]


def _eval_leaf(leaf: Mapping[str, Any], facts_: Facts) -> LeafResult:
    """ABSENT IS NOT FALSE, and this function is where that rule lives.

    A field missing from `Facts.values` makes every operator FALSE except `absent`. It is
    never coerced to `None` and compared, and it is never coerced to `False`: a condition
    that matched because a field was missing would fire on exactly the orders nobody has
    recorded anything about, which is the commonest way a rule over-fires (spec §3.2).
    """
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
            # A recorded value of the wrong type is a defect in the reader, not in the
            # rule. FALSE rather than an exception: one bad column must not stop the
            # sweep for every other order.
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
        # Every branch is evaluated, no short circuit: the leaf results are the dry run's
        # whole output, and "it stopped looking after the first false" makes the
        # explanation say less than the reader needs.
        return all([_eval_node(c, facts_, out) for c in cond[ALL]])
    if ANY in cond:
        return any([_eval_node(c, facts_, out) for c in cond[ANY]])
    leaf = _eval_leaf(cond, facts_)
    out.append(leaf)
    return leaf.result


def evaluate(cond: Mapping[str, Any], facts_: Facts) -> Evaluation:
    """Decide a parsed condition against one order's facts. PURE.

    No DB, no model, no clock it was not handed, nothing written. That is what makes
    `jarvis rules dry-run` safe to run against a live order and what makes a dry-run
    history comparable across days.
    """
    leaves: list[LeafResult] = []
    matched = _eval_node(cond, facts_, leaves)
    absent = tuple(dict.fromkeys(leaf.field for leaf in leaves if not leaf.present))
    return Evaluation(matched=matched, absent=absent, leaves=tuple(leaves))


def matches(cond: Mapping[str, Any], facts_: Facts) -> bool:
    """Whether this condition holds. `evaluate(...).matched`, for callers that only
    need the verdict."""
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
    """The condition as prose, for `jarvis rules show`.

    A reviewer arming a rule is authorising what it will eventually DO, and nobody reads
    JSON as a claim about the world. One renderer, here, rather than in the CLI: the
    dashboard renders the same sentence and the two must not drift.
    """
    if NOT in cond:
        return f"not ({render_condition(cond[NOT])})"
    if ALL in cond:
        return " and ".join(render_condition(c) for c in cond[ALL])
    if ANY in cond:
        return "(" + " or ".join(render_condition(c) for c in cond[ANY]) + ")"
    return _render_leaf(cond)


def explain(cond: Mapping[str, Any], evaluation: Evaluation) -> str:
    """The sentence `jarvis rules dry-run` prints.

    THE ABSENCE CLAUSE IS THE WHOLE POINT. "No" tells a rule author nothing; "did not
    match because `hold_cause` is not recorded on this order" tells them their rule asks
    about something this order does not have, which is the difference between a rule that
    is wrong and a rule that is aimed at the wrong orders. Spec §3.2 names this as the
    commonest way a rule over-fires and names the dry-run output as where it is caught.
    """
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


def facts(store: Any, wo: Mapping[str, Any], *, now: float) -> Facts:
    """Build one order's `Facts`. DECLARED HERE, BODY OWNED BY THE EVALUATION PASS.

    The signature is this section's and the body is a sibling section's (spec §3, the
    evaluation-and-firing pass): the two are built in parallel and neither worker can see
    the other's text, so the contract is written down where the grammar is and the
    implementation follows. Deliberate, not an oversight.

    THE CONTRACT the implementation must keep:

    * every key of `values` is a name in `FACT_FIELDS`, and a field the order does not
      record is ABSENT FROM `values` — never present as `None`, never `False`. The
      evaluator treats the three as three different things;
    * each field comes from the reader its `FactField.source` slug names: `work_order`
      the row itself, `state_durations` `ops.state_durations`, `holds` the merged open
      hold, `automerge` the `HELD_*` code, `waiting_on` `ops.waiting_on`,
      `validation_round` `ProjectStore.latest_validation_round`, `pull_request` the
      RECORDED columns and never a live `gh` call, `event_counts`
      `ProjectStore.count_events`, `invariant_events` the `invariant` timeline events,
      `dependencies` `invariants.dead_dependencies`, `neo_question`
      `invariants.awaiting_neo`, `budget` the budget columns;
    * it is built ONCE per order per sweep and handed to every detector. Re-deriving it
      per detector turns one query into one query per rule in the table;
    * the caller reads the EXPENSIVE sources lazily — the union of `sources_used(cond)`
      over the detectors that will actually be evaluated — so a table full of conditions
      about `status` costs one row read;
    * `now` is passed in, never read here, for the reason `Facts.now` gives.
    """
    raise NotImplementedError(
        "rules.facts is declared by the grammar section and implemented by the "
        "evaluation pass (spec §3, the evaluation-and-firing section), which owns the "
        "readers behind every FactField.source slug")


# -- the lookup contract ----------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """One thing the registry says could be done about one order, right now.

    The answer to "what does the registry say about this order", and the shape `jarvis wo
    fix` resolves a named blocker through instead of through a closed match table
    (kn-6c252734, kn-85265170). It carries the whole chain — which detector, which remedy
    row, the primitive and its parameters, and the remedy row's `argument` in the words a
    gate request would say — because every surface that renders a proposal needs all of
    it and none of them may re-derive any of it.
    """

    detector: Mapping[str, Any]
    remedy_rule: Mapping[str, Any]
    primitive: str
    params: Mapping[str, Any]
    argument: str
    #: `""` means possible. A SENTENCE means not possible, and it is the refusal's own
    #: words. `None` means NOT DETERMINABLE — nobody asked, or nothing could answer.
    #:
    #: THE THIRD VALUE IS NOT A CONVENIENCE. Collapsing it into `""` would state that the
    #: remedy can be applied, which is an affirmative this code never made: the pinned
    #: ruling is that the OS never fabricates a default answer from a failure to find one
    #: (Neo, question 905). A caller that shows "ready to apply" because nobody checked is
    #: exactly that failure.
    can_apply: str | None


def _retired(row: Mapping[str, Any]) -> bool:
    """Whether a remedy row is out of service.

    Two spellings because the table records both: `retired_at` is the timestamp the
    retraction wrote, `status` is the lifecycle column. Either one is enough — a row that
    is retired by one and not the other is a bug somewhere else, and resolving it INTO a
    proposal is the worst way to find out.
    """
    return bool(row.get("retired_at")) or row.get("status") == RETRACTED


#: Weakest first. A pair is only as live as its weaker half.
_STRENGTH = {RETRACTED: 0, DRY_RUN: 1, ARMED: 2}


def effective_status(detector: Mapping[str, Any],
                     remedy_rule: Mapping[str, Any]) -> str:
    """The status of a rule as a PAIR: the weaker of detector and remedy row.

    `retracted` < `dry_run` < `armed`, so a retracted half makes the pair dead and a
    `dry_run` half can never be acted on armed. Every caller downstream reads the pair
    through this and never the remedy row alone — the precedence is stated once, here,
    so nothing re-derives it and gets it backwards (spec §3.3).

    Pure: it takes row mappings and touches no store.
    """
    det = RETRACTED if _retired(detector) else str(detector.get("status") or DRY_RUN)
    rem = RETRACTED if _retired(remedy_rule) else str(
        remedy_rule.get("status") or DRY_RUN)
    return det if _STRENGTH.get(det, 0) <= _STRENGTH.get(rem, 0) else rem


# -- the recurrence lookup and verdict (spec §8) --------------------------------------
#
# When an investigation lands on a `gap_class` a detector ALREADY exists for, the thing
# that happened is not "a new bug": either the condition missed the symptom, or it
# matched and the remedy failed, or the rule was still in `dry_run` and never acted.
# Both functions below are PURE — they take rows and return words — so the ledger write
# and the tracker act (`ops.record_recurrence`) can be tested against a derived verdict
# instead of a guessed one.


def recurrence(detectors: Iterable[Mapping[str, Any]], gap_class: str, *,
               project: str = "") -> Mapping[str, Any] | None:
    """The ONE live detector for `gap_class` in `project`'s scope, or `None`. PURE.

    `None` means no rule for this gap exists, which is a DIFFERENT finding from a rule
    that failed: the caller must file a new gap, never a recurrence with an invented
    verdict.

    Scope is `CentralStore.list_detectors`' scope, restated because this function is also
    handed rows a caller read some other way: a `project` of `''` is fleet-wide and
    always in scope, a named one matches only itself. Retired rows are skipped by both
    spellings the table records, `_retired`'s reason.

    TIE-BREAK, and it is a RULING rather than a derivation (the work order's lead, on this
    section): a PROJECT-SCOPED detector beats a fleet-wide one, because it was learned
    from this project's own evidence; among equally scoped rows the OLDEST `ts` wins,
    because the recurrence links back to the ORIGINAL issue and the original detector is
    the one whose `issue_url` and `fix_wo_id` carry that thread. Crediting the newest
    would start a second thread about the same gap, which is the one thing §8 exists to
    prevent.
    """
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
    """Which half of the rule did not hold. PURE, and derived from the FIRE RECORD.

    `fire` is the newest fire this detector has on the order in question, cleared or
    not, or `None` when it has none.

    - A detector that is not `armed` gives `not_armed` whatever the fire says: a
      `dry_run` rule acts on nothing, so neither its condition nor its remedy can be
      blamed, and the recurrence is evidence FOR arming it. A retired row lands here too
      and cannot be blamed either, though `recurrence` never returns one.
    - `armed` with no fire is `missed`: the condition did not match what happened, and
      the finding is about the condition.
    - `armed` with a `proposed`, `refused` or `applied` fire is `remedy_failed`. `applied`
      is in that set on Neo question 974's ruling: the remedy RAN and the gap recurred
      anyway, so the remedy is still the half that failed — a remedy that leaves the gap
      standing is not a remedy that worked.
    - `armed` with an `unreadable` fire gives `unreadable`, its OWN verdict and never
      `missed`. Calling it `missed` would claim a decision that was never read, which the
      pinned ruling against fabricating a default answer from a failure forbids. It is
      also NOT evidence for arming: nothing was evaluated.
    - `armed` with a `recorded` or `cleared` fire is treated as NO USABLE FIRE, so
      `missed`. A `recorded` fire is a dry-run artefact that cannot exist under an armed
      detector, and a `cleared` fire is a condition that stopped holding — neither says
      the rule acted on what is happening now.
    """
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
    """The comment that goes on the ORIGINAL public issue. THE REDACTION BOUNDARY.

    §8's "redact before it leaves the OS": the tracker is PUBLIC, so this carries the
    order id, the gap class, the verdict and the detector id, AND NOTHING ELSE. There is
    no fifth parameter — not the order's title, not its description, not a fire's
    `detail` (which is text a worker wrote), not an attention reason, not the condition in
    prose — so there is no path any of them could reach a public comment through. The
    full local account lives in `rule_recurrences.filed_note`, which is not published.
    """
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
    """Split detector rows into `(row, parsed condition)` pairs and the unreadable ones.

    A stored condition is parsed again ON READ, not only on insert: a row written by an
    older release may name a field a later one removed, and evaluating it would silently
    answer a question the engine no longer understands. Such a row is SKIPPED and
    REPORTED — the caller records a `UNREADABLE` fire, which exists precisely so the
    silence is visible (spec §3.1).

    Exposed separately from `resolve`, which uses it internally, because `resolve` returns
    resolutions and an unreadable detector produces none — the only way a caller can
    report what it could not read is to be handed it.
    """
    readable: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    unreadable: list[tuple[str, RulesError]] = []
    for row in detectors:
        try:
            readable.append((row, parse_condition(row.get("condition"))))
        except RulesError as e:
            unreadable.append((str(row.get("id") or ""), e))
    return readable, unreadable


def _rows_for(remedy_rules: Any, detector_id: str) -> list[Mapping[str, Any]]:
    """The remedy rows belonging to one detector.

    CANONICAL SHAPE: a mapping `detector_id -> [row, …]`, which is what
    `CentralStore.remedy_rules_for` answers with and what avoids a scan per detector. A
    flat iterable of rows each carrying `detector_id` is also accepted, because a caller
    that read the whole table in one query should not have to group it just to call this.
    """
    if isinstance(remedy_rules, Mapping):
        return list(remedy_rules.get(detector_id) or ())
    return [r for r in remedy_rules if r.get("detector_id") == detector_id]


def _declares_can_apply(primitive: str) -> bool:
    """Whether anything can answer "could this remedy run right now" for this primitive.

    A primitive absent from `remedies.REMEDIES` declares nothing — there is no registry
    entry to ask. The `getattr` is forward-compatible on purpose: the primitives section
    may give a `Remedy` an explicit `can_apply`, and one set to `None` there means the
    same "nobody can answer" without an edit here.
    """
    remedy = remedies.REMEDIES.get(primitive)
    return remedy is not None and getattr(remedy, "can_apply", True) is not None


def resolve(
    detectors: Iterable[Mapping[str, Any]],
    remedy_rules: Any,
    facts_: Facts,
    *,
    precondition: Callable[[str, Mapping[str, Any]], str] | None = None,
) -> tuple[Resolution, ...]:
    """What the registry says could be done about this order right now. PURE.

    One `Resolution` per live remedy row of every detector whose condition PARSES and
    MATCHES. It reads no store, reaches no network and acts on nothing.

    `precondition` is the seam for the part that DOES need a store: a caller holding one
    injects `(primitive, params) -> str` and gets the refusal's own words back on
    `Resolution.can_apply`. Injected rather than imported because a leaf that reached for
    a store would make the whole evaluation impure and untestable in one step.

    A detector whose stored condition cannot be parsed is skipped SILENTLY here — use
    `readable_detectors` to report it, which is what `UNREADABLE` is for.
    """
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


def validate_params(primitive: str, params: Mapping[str, Any]) -> list[str]:
    """Why this remedy row may not be inserted, or `[]`. Run at INSERT time.

    The name check is unconditional and is the one that matters: **the database grows
    RULES, never PRIMITIVES** (kn-6c252734). A rule NAMES a primitive from the closed
    `remedies.REMEDIES` registry; it can never introduce one, because a registry that can
    be extended by a row is not closed and `remedies`' whole argument rests on its being
    closed.

    The schema check is written to SWITCH ITSELF ON. `Remedy` carries no `params` schema
    today — that lands with the primitives section — so `getattr` returns None and only
    the name is checked. The day a schema appears, unknown parameters, missing required
    ones and wrong types start being refused here with no edit to this function.
    """
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
    """One primitive's parameter schema as `name -> (type, required)`, whatever shape it
    arrived in. `{}` when the primitive declares none.

    TOLERANT ON PURPOSE. The primitives section spells `Remedy.params` as
    `tuple[Param, ...]` — objects carrying `.name`, `.type` and `.required` — while the
    obvious reading of an insert-time schema is a mapping of name to spec dict. That
    section is on a PARALLEL branch and its exact spelling is not settled, and neither
    worker can see the other's text; a schema check that raises `AttributeError` the day
    the other shape lands is strictly worse than one that adapts to both. Anything this
    cannot read at all is treated as "no schema declared", which leaves the unconditional
    name check — the one that matters — doing its job.
    """
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


# -- the seeds ---------------------------------------------------------------------------

#: `detectors.seed_version` is INTEGER, so this is an int and not `gate_rules`' string.
#: Bumping it is how a later release adds seeds to an `os.db` that already has some: the
#: seeding pass returns early on a matching version, so reusing a number leaves the new
#: rows out of every database that has the old ones.
SEED_VERSION = 1

#: `invariants.VALIDATION_STUCK_BLOCKER`, INLINED. This module is a leaf — `invariants`
#: sits well above it and importing it would make the grammar depend on the whole
#: detection stack — and the seed rule below has to quote the sentence exactly, because
#: `attention_reason` holds one of `invariants.true_blockers`' own sentences and nothing
#: else. `tests/test_rules.py::test_the_inlined_blocker_sentence_tracks_invariants`
#: imports both and asserts they are equal, which is what keeps the copy honest.
VALIDATION_STUCK_BLOCKER = ("the review could not be satisfied — the work needs your "
                            "judgement")

#: Where the issues that discovered these gaps live.
_TRACKER = "https://github.com/gonandrap/agentic_os/issues"


def seed_id(prefix: str, body: Any) -> str:
    """A stable id for a seeded row.

    Derived from the content so that re-seeding is idempotent across upgrades AND cannot
    resurrect a rule the user retracted: the row already exists, so the insert is ignored
    rather than replayed. A random id would quietly restore a retired detector on every
    release, which is the sort of bug nobody finds until it matters — and here it is
    worse than in `gate_rules`, because an armed detector resurrected this way is a rule
    ACTING on orders after a person decided it should not.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha1(f"{prefix}|{canonical}".encode()).hexdigest()
    return f"{prefix}-{digest[:10]}"


# The five gaps of §1, each one a real incident with an issue behind it. They are DATA,
# they live beside the grammar rather than beside the evaluation pass, and that placement
# is the grammar's acceptance criterion: all five must be expressible without extending
# it (spec §3.2/§3.5). A criterion checked anywhere else is not a criterion.
#
# Every one ships in `dry_run`. There is no argument that writes an armed detector — a
# person arms a rule, once its hit history says it is right (spec §3.3).
_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "gap_class": "stale-panel-hold",
        "issue_url": f"{_TRACKER}/786",
        "summary": ("a `needs_review` order still flagged as unsatisfiable by the panel "
                    "when its newest round has since PASSED (issues #786, #813)"),
        "condition": {"all": [
            {"field": "status", "op": "eq", "value": "needs_review"},
            {"field": "needs_attention", "op": "eq", "value": True},
            # The literal is `invariants.VALIDATION_STUCK_BLOCKER`, inlined above for the
            # leaf reason given there. Quoting the sentence rather than re-deriving the
            # predicate is deliberate: `true_blockers` owns that reason, and a detector
            # that re-derived "the panel gave up" would be a second implementation of it
            # that drifts the first time the first one changes.
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
            # `automerge.HELD_SHA_MOVED`. Quoted rather than imported for the leaf reason;
            # pinned by `test_the_quoted_automerge_codes_are_the_real_ones`.
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
            # `automerge.HELD_BASE_RED`, quoted for the leaf reason and pinned by test.
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
        # THE SHAPE TO COPY. `INV-WORK-LANDED` already detects that the work landed, so
        # this condition keys off that invariant's own timeline EVENT rather than
        # re-deriving the predicate. The spec's hard rule: a detector may not duplicate an
        # invariant — where one already detects, the rule contributes the REMEDY.
        "condition": {"all": [
            {"field": "status", "op": "in", "value": ["pending", "running"]},
            {"field": "invariant_events", "op": "count_gte",
             "value": {"kind": "INV-WORK-LANDED", "value": 1}},
            {"field": "seconds_since_activity", "op": "gte", "value": 3600},
        ]},
        "remedies": [
            # `invariants.true_blockers` owns `attention_reason`, and NOTHING may write a
            # reason that function cannot re-derive — every surface renders that column as
            # the OS's account of why an order needs a person, and a sentence only one
            # writer knows about is an account nobody can check. So this reason has to
            # become one of `true_blockers`' own. Flagged here rather than fixed here:
            # the primitives section owns `raise_attention` and owns making that true.
            # §4: `raise_attention` RENDERS its reason from a TEMPLATE KEY and never
            # relays one. The closed tuple of keys lives in `remedies.py` and is that
            # section's to define; this is the key this seed row claims, and §5.3
            # reconciles it against the real tuple. The words a reviewer reads are the
            # `argument` below — the field for words — not a parameter.
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
)


def seed_rows() -> list[dict[str, Any]]:
    """The five builtin detectors with their remedy rows, as store rows.

    `[{"detector": {...}, "remedies": [{...}]}]` — nested rather than two flat lists
    because a remedy row carries a foreign key to its detector and the seeding pass would
    otherwise have to re-derive the pairing it was just handed.

    Ids are content-derived (`seed_id`), so re-seeding is idempotent and cannot resurrect
    a retracted rule. Nothing here calls this: seeding is invoked by the section that owns
    the store methods.
    """
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
