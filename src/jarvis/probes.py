"""Health probes: what counts as a work order or feature order going badly (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

#: The two things a probe can be pointed at
SUBJECTS = ("work_order", "feature_order")

#: A probe id is the alarm's `probe` column and a surface's grouping key, so it is spelled one way only
ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: `inspection`'s alarm kinds, which a probe id may NOT take: the two would read as one thing on...
RESERVED_IDS = ("long-turn", "stalled-turn", "slow-model-response",
                "long-join", "big-rewrite",
                "rewrite-tax-prefix", "rewrite-tax-ttl",
                "cache-1h-dispatched", "cache-1h-foreign",
                "compaction-chained", "compaction-unproven")


@dataclass(frozen=True)
class HealthProbe:
    """One symptom, addressed to the supervisor"""

    id: str
    title: str
    prompt: str
    subjects: tuple[str, ...] = SUBJECTS
    enabled: bool = True


# -- the shipped list

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
            "file edited and re-edited, the same command re-run, the same error quoted "
            "twice. Effort is being re-spent rather than advancing.\n"
            "What innocently looks like this: an edit-run-fix loop that is converging, "
            "where the error changes each time or the work visibly grows, and a "
            "refactor legitimately touching one file repeatedly. Repetition is only a "
            "symptom when nothing about it moves.\n"
            "A turn that made NO API call is not this symptom: the work never started "
            "rather than ran twice. The OS raises that one itself.\n"
            "Report what recurred and across which turns. If you cannot tell whether it "
            "was converging, say so rather than assert it was not."
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
    """Merge an override list over `base` BY ID, returning a new tuple"""
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
    """The enabled probes that apply to one kind of subject, in list order"""
    return tuple(p for p in probes if p.enabled and subject_kind in p.subjects)


#: The sweep's reply shape (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
SWEEP_CONTRACT = """# Your output, which REPLACES the shape described above

You are not reviewing a cost alarm. There is no `decision` to make here and no alarm to
ack, escalate or propose against: you are checking one unit against the checklist above.
Ignore the output shape given earlier in this prompt and answer ONLY in this one.

Output STRICT JSON, nothing else:
  {"findings": [{"probe": "<the id from a heading above>",
                 "reason": "<one line: what is wrong, for the user>",
                 "evidence": "<what in the packet you were given says so>"}]}

`{"findings": []}` is the healthy answer and the common one — report a symptom only when
the evidence you were given actually supports it. Name no probe that is not in the
checklist above."""


def render_checklist(probes: Sequence[HealthProbe]) -> str:
    """The checklist §4 puts in the supervisor's system prompt (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
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
    lines += ["", SWEEP_CONTRACT]
    return "\n".join(lines)
