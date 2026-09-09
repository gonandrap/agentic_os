"""Health probes: what counts as a work order or feature order going badly.

§2 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md. NOTHING IN
THIS MODULE RAISES A FINDING OR MAKES A MODEL CALL — it ships the list §4's sweep reads.

A leaf with no `jarvis` imports, so `catalog` can hold a `tuple[HealthProbe, ...]` the
way it already holds a `GateConfig`. The prose lives here rather than in `catalog.py`
for the reason `gates.REVIEWER_PERSONA` and `plans.PLAN_REVIEWER_PERSONA` live in the
modules that own their kinds: a persona belongs beside the thing it judges, and a
catalog full of paragraphs is a catalog nobody can read for its settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

#: The two things a probe can be pointed at. `subjects` is refused against this at
#: catalog-parse time, because a probe armed for nothing is a setting that silently
#: does nothing.
SUBJECTS = ("work_order", "feature_order")

#: A probe id is the alarm's `probe` column and a surface's grouping key, so it is
#: spelled one way only.
ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: `inspection`'s three alarm kinds, which a probe id may NOT take: the two would read
#: as one thing on `/alarms`, in `jarvis alarms` and in `alarms_across`. Duplicated as
#: literals rather than imported — `inspection` imports `catalog`, which imports this
#: module, so the import would be a cycle. `tests/test_probes.py` pins the two equal.
RESERVED_IDS = ("long-turn", "long-join", "big-rewrite")


@dataclass(frozen=True)
class HealthProbe:
    """One symptom, addressed to the supervisor.

    Frozen because the shipped list is module-level state shared by every project's
    resolved config: `resolve` returns a new tuple and never edits one in place.
    """

    id: str
    title: str
    prompt: str
    subjects: tuple[str, ...] = SUBJECTS
    enabled: bool = True


# -- the shipped list -------------------------------------------------------------------
#
# CONTENT, NOT SCAFFOLDING (§2). Each prompt says what the symptom IS, what in the
# evidence packet would show it, what would innocently look like it, and what to do with
# a symptom that is visible but unexplained. A one-line prompt produces a detector that
# fires on everything, and the `reason` it produces is what the user reads on /alarms.

DEFAULT_PROBES: tuple[HealthProbe, ...] = (
    HealthProbe(
        id="no-progress",
        title="Nothing is moving",
        prompt=(
            "Nothing has changed on this unit's record for a long time. Look for a "
            "latest turn whose sequence and state are where they were, a newest event "
            "that is old, no message in either direction, and — on a feature order — "
            "child statuses that have not moved. The instrument is nominally open and "
            "is not moving.\n"
            "What innocently looks like this: an order deliberately parked (waiting on "
            "a PR to merge, blocked on a dependency it names, waiting for the user), "
            "and one long turn that is genuinely still generating. Both have a visible "
            "reason on the record; neither is this symptom.\n"
            "If you can see the stall but not its cause, report the stall and stop. Do "
            "not guess at a cause — an invented explanation costs the reader more than "
            "a plain observation."
        ),
    ),
    HealthProbe(
        id="going-in-circles",
        title="Effort being re-spent",
        prompt=(
            "The same tool, file, test or error recurs across turns without the state "
            "changing. Look for the same test failing in consecutive turns, the same "
            "file edited and re-edited, the same command re-run, the same error text "
            "quoted in a later turn as in an earlier one. Effort is being re-spent "
            "rather than advancing.\n"
            "What innocently looks like this: an edit-run-fix loop that is converging, "
            "where the error changes each time or the work visibly grows, and a "
            "refactor that touches one file repeatedly for good reason. Repetition is "
            "only a symptom when nothing about it moves.\n"
            "Report what recurred and across which turns. If you cannot tell whether it "
            "was converging, say so rather than asserting that it was not."
        ),
        subjects=("work_order",),
    ),
    HealthProbe(
        id="waiting-on-nobody",
        title="Blocked on nothing",
        prompt=(
            "The unit is blocked on something nobody is going to resolve: a queued "
            "message nothing consumes, a question already settled elsewhere, a decision "
            "the user has already made, a dependency that can never clear, an approval "
            "nobody was asked for. Look for a stated blocker with no live counterpart "
            "anywhere in the packet.\n"
            "What innocently looks like this: a wait that is real and young — a gate "
            "under review, a question filed minutes ago, a PR that will be merged. A "
            "wait with an owner and a clock is not this symptom.\n"
            "Name what it is waiting for and say why nothing will arrive. If you cannot "
            "establish that nothing will arrive, do not report it."
        ),
    ),
    HealthProbe(
        id="failing-children",
        title="Children not converging",
        prompt=(
            "Children of this feature are failing, being superseded or being re-filed "
            "repeatedly. Look for several children failed or cancelled, a child "
            "superseded and replaced more than once, the same piece of work filed under "
            "successive ids, or validation rejecting the same unit round after round. "
            "The feature is not converging on a finished state.\n"
            "What innocently looks like this: one child that failed once and was "
            "re-filed, which is the ordinary repair path, and a large feature where a "
            "minority have failed while the rest complete.\n"
            "Report which children and what the pattern is. Say how many and what "
            "happened to them; do not rate how serious it is."
        ),
        subjects=("feature_order",),
    ),
    HealthProbe(
        id="brief-mismatch",
        title="Drifted from the ask",
        prompt=(
            "What the instrument is doing has drifted from what it was asked to do. "
            "Compare the brief quoted in the packet against what the turns, messages "
            "and events show actually happening: areas nobody asked about, a scope "
            "visibly wider or narrower than the ask, work on a different problem than "
            "the one described.\n"
            "What innocently looks like this: groundwork the brief did not enumerate "
            "(reading neighbouring code, repairing a test the change broke), and a "
            "brief written at a high level whose implementation legitimately touches "
            "more than it names.\n"
            "Quote the part of the brief and the part of the evidence that disagree. If "
            "the brief is too vague to disagree with, that is not a finding."
        ),
    ),
)


def resolve(base: Sequence[HealthProbe],
            override: Iterable[Mapping[str, Any]]) -> tuple[HealthProbe, ...]:
    """Merge an override list over `base` BY ID, returning a new tuple.

    THE MERGE RULE IS THE DECISION (§2). An entry whose id matches replaces only the
    fields it names; a new id appends; a probe the override does not name is inherited
    unchanged. Wholesale replacement was rejected because "watch for one more thing
    here" would then mean re-declaring the whole list, which silently stops tracking the
    fleet's later edits — `kn-6ca2bcd9`'s failure, one level down. A project can DISABLE
    an inherited probe and cannot delete it, so what the fleet watches for stays legible
    on every project's read.

    `override` entries are PARTIAL: `{"id": …}` plus whatever fields were named, already
    typed by `catalog._parse_probes`. That is what "named fields" means, and it is why
    this takes mappings rather than `HealthProbe`s — a `HealthProbe` cannot express
    "prompt was not mentioned".

    `base` is never mutated: the shipped `DEFAULT_PROBES` is shared by every project's
    resolved config.
    """
    out = list(base)
    at = {p.id: i for i, p in enumerate(out)}
    for entry in override:
        named = {k: v for k, v in entry.items() if k != "id"}
        pid = str(entry.get("id") or "")
        if pid in at:
            out[at[pid]] = replace(out[at[pid]], **named)
        else:
            at[pid] = len(out)
            out.append(HealthProbe(id=pid, **named))
    return tuple(out)


def armed(probes: Iterable[HealthProbe], subject_kind: str) -> tuple[HealthProbe, ...]:
    """The enabled probes that apply to one kind of subject, in list order."""
    return tuple(p for p in probes if p.enabled and subject_kind in p.subjects)


def render_checklist(probes: Sequence[HealthProbe]) -> str:
    """The checklist §4 puts in the supervisor's system prompt.

    Empty in, empty out — and that is load-bearing: `supervisor.build_system_prompt`
    appends nothing at all when there are no probes, so the cost review's prompt prefix
    stays byte-identical to what it was before this feature existed.

    The id is rendered beside the title because a finding names its probe by id (§4's
    output contract); the ORDER is the resolved list's, so a reader of
    `jarvis supervisor probes` sees the list in the order the model saw it.
    """
    if not probes:
        return ""
    lines = [
        "# The symptom checklist",
        "",
        "Check the subject against EACH symptom below, independently. Report only the "
        "ones the evidence you were given actually supports, and name each one by the "
        "id in its heading.",
    ]
    for probe in probes:
        lines += ["", f"## {probe.id} — {probe.title}", probe.prompt]
    return "\n".join(lines)
