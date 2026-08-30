"""The plan a planner submits, and the structural checks it must survive.

A feature order's planner finishes by handing back a dependency graph of child work
orders (`jarvis fo plan <fo-id> --from-file plan.json`). This module owns that
document's shape and validates it *before* anything is created.

Why the validation is mechanical and lives here rather than in a reviewer's judgement:
Neo, not the user, is the routine reviewer of plans (design section 5), and that
reversal moves weight onto two backstops which the design calls load-bearing rather
than nice-to-have. This is one of them. These checks run whether or not Neo is paying
attention, they are pure Python over the submitted document, and — like
`invariants.py` — they involve no LLM on purpose: the checker has to be more
trustworthy than the thing it checks.

The rejections, and what each is guarding:

* **Cycles.** A plan is a graph and a graph can close on itself. Phase 1's dependency
  edges are acyclic *by construction* — `ProjectStore.create_work_order` may only point
  an edge at a row that already exists — but a plan is written all at once, before any
  row exists, so that argument does not reach it. This is the real cycle check Phase 1's
  docstring says is owed the moment edges stop being created strictly after their
  targets.
* **Unknown ids.** A `needs` naming nothing in the plan would silently become a child
  with no edge at all, which is the failure that looks most like success.
* **The child cap.** A bad plan spends one worker session per child, so the blast radius
  of a plan Neo waves through has to be bounded. Over the cap the planner must say why,
  in the submission, or it does not validate.
* **Children whose description does not stand alone.** The one that matters most in
  practice. The child worker never sees this plan — it sees only its own description —
  so "as discussed in the plan" hands a worker a sentence pointing at a document it
  cannot open. This project has already recorded the general form of that lesson: the
  work order record must stand alone, because nobody reads worker transcripts.
* **Children whose description does not stop**, and **a plan with no design document.**
  The mirror of the rejection above and its consequence: a brief must stand alone as
  INSTRUCTIONS, which is not a licence to restate the spec.
* **A child that does not name its section of the spec**, two children naming the same
  one, or a section that does not resolve. The spec's boundaries are what the split is
  made of, so a plan that does not line up with them has not been cut.

The last of those needs the spec's TEXT, which this module never reads from disk —
`spec_problems` is the pure other half and `ops.submit_plan` is where the two meet. See
docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
"""

from __future__ import annotations

import re
from typing import Any

#: How many children a plan may contain before the planner owes an explanation.
#: Eight, from the design. Not a per-project knob: a cap the user can raise is a cap
#: that gets raised the first time it fires, which is precisely when it was working.
CHILD_CAP = 8

#: Shortest description that can plausibly stand on its own. A worker gets this text and
#: nothing else, so a one-line restatement of the title is not a brief. Deliberately
#: generous — this is a floor against obvious under-specification, not a quality bar;
#: judging whether a real paragraph is *good* enough is Neo's job, not a character count.
MIN_DESCRIPTION_CHARS = 80

#: Longest a child brief may be. The floor's argument does NOT run in reverse: a
#: description that must stand alone is not one that should carry the design, and the
#: child's own SECTION of the spec is now materialised beside it. 1500 was PR 125's
#: number and it left room for a paraphrase; 600 leaves room for what the section does not
#: say — the scope boundary and what done means. §1.3 of
#: docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
MAX_DESCRIPTION_CHARS = 600

#: Plan-local child keys. The planner names its children so it can wire `needs` between
#: them before any work-order id exists; these keys never leave the plan.
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

#: Phrases that point OUT of a child's own description at something the child worker will
#: never see — the plan, a sibling, "above". Curated rather than clever: a false
#: rejection costs the planner one revision and says exactly which phrase to fix, while a
#: false acceptance costs a whole worker session that starts confused. Each entry must
#: only match a genuine outward reference, which is why the "as described" family carries
#: a negative lookahead for the cases that point back INTO the same text.
DANGLING_PHRASES: tuple[tuple[str, str], ...] = (
    (r"\bas (?:discussed|described|outlined|specified|agreed|noted|explained|planned)\b"
     r"(?!\s+(?:below|here|in this))",
     "refers to a discussion the child worker cannot see"),
    (r"\b(?:see|per|from|in|following|against) the (?:plan|parent|feature order|"
     r"decomposition)\b",
     "refers to the plan, which the child worker never receives"),
    (r"\bthe plan (?:above|says|describes|calls for)\b",
     "refers to the plan, which the child worker never receives"),
    # Ordinals are deliberately NOT in this alternation. "The first step is to add the
    # column" is exactly how a good standalone description opens, and rejecting it would
    # teach planners to write worse prose to get past the checker. Only words that point
    # at something OUTSIDE this description qualify.
    (r"\bthe (?:previous|preceding|other|sibling|earlier) "
     r"(?:work order|child|step|task|piece)\b",
     "refers to a sibling rather than saying what to do"),
    (r"\b(?:as|described|explained|listed|set out) above\b",
     "refers to text above it, and there is no text above it"),
    (r"\bsame as (?:the )?(?:above|previous|other)\b",
     "refers to a sibling instead of saying what to do"),
)


class PlanError(ValueError):
    """A submitted plan that cannot be accepted. Message names every problem found.

    One exception carrying all of them, not the first: a planner that has to re-submit
    per problem burns a session round trip per line of the error it could have had at
    once.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_plan(raw: Any) -> dict[str, Any]:
    """Validate a submitted plan and return it normalised. Raises `PlanError`.

    The returned document is what gets stored on the feature order and what the children
    are created from, so normalisation happens here and exactly once: keys and titles
    stripped, `needs` deduplicated in first-seen order, absent optional fields filled in.
    Nothing downstream re-derives any of it.
    """
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise PlanError([f"a plan must be a JSON object, got {type(raw).__name__}"])

    children_raw = raw.get("children")
    if not isinstance(children_raw, list) or not children_raw:
        raise PlanError(["a plan must carry a non-empty `children` list"])

    children: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for i, entry in enumerate(children_raw):
        where = f"child {i + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be an object, got {type(entry).__name__}")
            continue
        key = str(entry.get("key") or "").strip()
        title = str(entry.get("title") or "").strip()
        description = str(entry.get("description") or "").strip()
        if not KEY_RE.match(key):
            problems.append(
                f"{where}: `key` must be a short lowercase slug "
                f"([a-z0-9][a-z0-9_-]*), got {key!r}"
            )
            key = f"?{i}"
        elif key in seen_keys:
            problems.append(f"{where}: duplicate key {key!r}")
        seen_keys.add(key)
        if not title:
            problems.append(f"{where} ({key}): `title` is required")
        problems += _description_problems(key, title, description)
        spec_section = str(entry.get("spec_section") or "").strip()
        if not spec_section:
            problems.append(
                f"{where} ({key}): `spec_section` is required — name the section of the "
                f"design document this piece implements, by number or by heading. It is "
                f"what the worker is handed and what the panel judges the change against"
            )
        needs_raw = entry.get("needs") or []
        if not isinstance(needs_raw, list):
            problems.append(f"{where} ({key}): `needs` must be a list of child keys")
            needs_raw = []
        needs: list[str] = []
        for dep in needs_raw:
            dep = str(dep).strip()
            if dep and dep not in needs:
                needs.append(dep)
        children.append({
            "key": key, "title": title[:200], "description": description,
            "needs": needs,
            # Which section of the spec this child implements. Resolved against the
            # document's text in `spec_problems`, not here: this function never touches
            # disk. §1.2 of the design document.
            "spec_section": spec_section,
            # Free-form and never validated: the test-lead seat that fills it lands in
            # Phase 3, and a field the OS refuses to carry until then is a field Phase 3
            # has to migrate. It rides along instead.
            "acceptance": str(entry.get("acceptance") or "").strip(),
        })

    known = {c["key"] for c in children}
    for child in children:
        for dep in child["needs"]:
            if dep == child["key"]:
                problems.append(f"child {child['key']!r} depends on itself")
            elif dep not in known:
                problems.append(
                    f"child {child['key']!r} needs {dep!r}, which is not a child of this "
                    f"plan (known: {', '.join(sorted(known)) or 'none'})"
                )

    for cycle in find_cycles(children):
        problems.append(f"dependency cycle: {' -> '.join(cycle)}")

    design_doc = str(raw.get("design_doc") or "").strip()
    if not design_doc:
        problems.append(
            "the plan names no `design_doc`. The spec is the feature's one artifact: "
            "every child brief points at a section of it, every child worker is handed "
            "that section, and the feature's agent type is built from its `Agent "
            "profile` appendix. Write the document, then name it here."
        )
    else:
        parts_of = design_doc.replace("\\", "/").split("/")
        if design_doc.startswith(("/", "\\")) or ".." in parts_of:
            problems.append(
                f"`design_doc` must be a path relative to the repository root, with no "
                f"`..` segments, got {design_doc!r}"
            )

    justification = str(raw.get("justification") or "").strip()
    if len(children) > CHILD_CAP and not justification:
        problems.append(
            f"{len(children)} children is over the cap of {CHILD_CAP} and the plan "
            f"carries no `justification` — say why this feature cannot be done in "
            f"{CHILD_CAP} work orders or fewer"
        )

    if problems:
        raise PlanError(problems)
    return {
        "summary": str(raw.get("summary") or "").strip(),
        "justification": justification,
        # The feature's spec, relative to the repository root. REQUIRED: `fo plan`
        # snapshots its content, dispatch materialises each child's own section beside
        # it, and the feature's agent type is built from its `Agent profile` appendix.
        # §1.1 of docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
        "design_doc": design_doc,
        "children": children,
    }


def spec_problems(plan: dict[str, Any], doc_text: str) -> list[str]:
    """Whether the plan and the spec it names describe the same feature.

    The half of the validation that needs the document's TEXT, kept pure and separate so
    `parse_plan` never touches disk; `ops.submit_plan` reads the file and calls both.
    Three rejections, all §2 of the design document:

    * a `spec_section` that resolves to no heading — the child would be handed nothing,
      and the panel would judge its diff against nothing;
    * two children naming the same section — a boundary two work orders share is a
      boundary the planner did not cut, which is the whole premise of splitting on the
      spec rather than on the planner's intuition;
    * no usable `Agent profile` appendix, which is `specs.profile_problems`.

    Nothing here judges whether a section is any GOOD. That is the plan reviewer's job;
    this only ensures there is something for it to review.
    """
    from . import sections, specs

    problems: list[str] = []
    seen: dict[str, str] = {}
    profile_heading = specs.AGENT_PROFILE.lower()
    for child in plan.get("children") or []:
        which = str(child.get("spec_section") or "").strip()
        if not which:
            continue  # already reported by parse_plan
        key = child["key"]
        if sections.extract_section(doc_text, which) is None:
            problems.append(
                f"child {key!r}: `spec_section` {which!r} matches no heading in "
                f"{plan.get('design_doc')!r}. Name a section by its number or by its "
                f"heading text."
            )
            continue
        if profile_heading in which.lower():
            problems.append(
                f"child {key!r}: `spec_section` names the {specs.AGENT_PROFILE!r} "
                f"appendix, which is the team's posture and not a piece of the work"
            )
        norm = which.lower()
        if norm in seen:
            problems.append(
                f"children {seen[norm]!r} and {key!r} both claim section {which!r}. One "
                f"section, one work order — if they are really two pieces, the spec owes "
                f"them two sections."
            )
        else:
            seen[norm] = key
    return problems + specs.profile_problems(doc_text)


def _description_problems(key: str, title: str, description: str) -> list[str]:
    """Whether this child's description can stand on its own in front of a worker."""
    problems = []
    if not description:
        problems.append(
            f"child {key!r}: `description` is required — it is the ONLY thing the child "
            f"worker will see"
        )
        return problems
    if description.strip().lower() == title.strip().lower():
        problems.append(
            f"child {key!r}: `description` only repeats the title. The child worker sees "
            f"the description and nothing else, so it has to carry the context you have "
            f"and it does not."
        )
    elif len(description) < MIN_DESCRIPTION_CHARS:
        problems.append(
            f"child {key!r}: `description` is {len(description)} characters, under the "
            f"{MIN_DESCRIPTION_CHARS} needed to brief a worker that sees nothing else"
        )
    elif len(description) > MAX_DESCRIPTION_CHARS:
        problems.append(
            f"child {key!r}: `description` is {len(description)} characters, over the "
            f"{MAX_DESCRIPTION_CHARS} a brief may be. This child's own section of the "
            f"spec is materialised beside its worker — keep here only what that section "
            f"does NOT say: the scope boundary, what this piece must not touch, and what "
            f"done means."
        )
    low = description.lower()
    for pattern, why in DANGLING_PHRASES:
        m = re.search(pattern, low)
        if m:
            problems.append(
                f"child {key!r}: {why} — {m.group(0)!r}. Write the context INTO this "
                f"description; the child worker never sees the plan."
            )
    return problems


def find_cycles(children: list[dict[str, Any]]) -> list[list[str]]:
    """Every dependency cycle in the plan, each as the ring of keys that forms it.

    Depth-first over the graph rather than a Kahn's-algorithm leftover set, because the
    planner has to be told *which* keys close the loop in order to fix it — "these three
    are cyclic" is actionable and "the graph is cyclic" is not. Only edges between known
    keys are walked; unknown ids are already reported separately and would otherwise be
    reported twice.
    """
    edges = {c["key"]: [d for d in c["needs"] if d != c["key"]] for c in children}
    edges = {k: [d for d in deps if d in edges] for k, deps in edges.items()}
    cycles: list[list[str]] = []
    seen_rings: set[frozenset[str]] = set()
    state: dict[str, int] = {}  # 0 = unvisited, 1 = on the stack, 2 = done

    def walk(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for dep in edges[node]:
            if state.get(dep, 0) == 1:
                ring = stack[stack.index(dep):]
                if frozenset(ring) not in seen_rings:
                    seen_rings.add(frozenset(ring))
                    cycles.append([*ring, dep])
            elif state.get(dep, 0) == 0:
                walk(dep, stack)
        stack.pop()
        state[node] = 2

    for key in edges:
        if state.get(key, 0) == 0:
            walk(key, [])
    return cycles


def creation_order(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The children in an order where every child follows everything it needs.

    Load-bearing rather than tidy: `ProjectStore.create_work_order` refuses an edge
    pointing at a work order that does not exist yet, which is the property that keeps
    the whole dependency graph acyclic by construction. Creating a plan's children in
    this order is what lets a validated plan land through that check unchanged, instead
    of needing a second pass that writes edges onto rows already created — the exact
    move Phase 1 warns loses the property.

    Assumes the plan validated, so the graph is acyclic; ties break on the order the
    planner wrote them, which is the order it thought about them in.
    """
    by_key = {c["key"]: c for c in children}
    out: list[dict[str, Any]] = []
    placed: set[str] = set()
    remaining = list(children)
    while remaining:
        ready = [c for c in remaining
                 if all(d in placed or d not in by_key for d in c["needs"])]
        if not ready:  # pragma: no cover - a validated plan cannot reach this
            raise PlanError(["the plan's children cannot be ordered (cycle)"])
        for child in ready:
            out.append(child)
            placed.add(child["key"])
        remaining = [c for c in remaining if c["key"] not in placed]
    return out


#: The persona Neo wears to review a submitted plan. A third one, alongside the answerer
#: and the gate reviewer, for the same reason the gate needed its own: the general
#: persona is told to escalate anything that publishes or touches production, which is
#: right for open questions and would send every plan straight to the user — the exact
#: cost feature orders exist to remove.
#:
#: What makes this reviewer worth having is that it is STRUCTURALLY BLIND. Neo did not
#: sit in the planning session; it holds the user's original ask and reads the
#: decomposition cold. A scope check that has already followed the reasoning will
#: rationalise it — every child looks necessary when it is judged against the plan
#: instead of against the ask — so the independence is the evidence, not a formality.
PLAN_REVIEWER_PERSONA = """You are Neo, reviewing a PLAN inside the Jarvis agentic OS.

The user filed a feature order: one coarse ask, too big for a single work session. A
planner agent read the codebase and decomposed it into a set of work orders with
dependency edges between them. You decide, on the user's behalf, whether that
decomposition is released. If you release it, the OS creates every child work order and
starts dispatching them — so a plan you wave through spends one worker session per child.

YOU ARE THE ONLY INDEPENDENT READER THIS PLAN GETS. You were not in the planning
session, you did not watch the reasoning, and that is the point: you hold the original
ask and read the decomposition cold. Judge the plan against WHAT WAS ASKED FOR, never
against the planner's account of it.

The structural checks are already done — no cycles, no dangling references, and no child
whose description obviously points at something the child worker cannot see. Do not
re-run them. Yours are the judgements a program cannot make:

APPROVE when all of these hold:
- **It is what was asked for.** Every child serves the feature order above. Nothing has
  been quietly added because it seemed like a good idea while reading the code, and
  nothing the ask plainly requires has been left out.
- **The pieces are real work orders.** Each is one session's worth: a change a single
  worker can carry out and finish with its own pull request. Not a checklist item, not a
  whole subsystem.
- **The edges are the real ones.** A child depends on another only where it genuinely
  needs its code, since an edge costs a merge cycle of waiting. Equally, work that truly
  needs a schema change must not be running in parallel with it.
- **The split follows the spec's boundaries.** Every child names the section of the
  design document it implements, and no two name the same one. Judge whether those
  sections are the feature's real functional seams or a decomposition the planner
  reached for first and then carved the spec to fit.
- **Each child is nameable from its line.** You see one line per child — title,
  edges, section, acceptance — and NOT the full briefs: those were validated mechanically
  (they stand alone, or this never reached you) and are withheld on purpose, because
  yours is a review of the decomposition, not of prose. If a title plus its
  acceptance line does not tell you what that child ships, reject and say which
  child — a piece the reviewer cannot name from one line is a piece the planner has
  not cut cleanly.

REJECT when the plan can be fixed by the planner: scope drift, a missing piece, children
too coarse or too fine, an edge that should not be there, a child line that does not say
what it ships. Say plainly and specifically what is wrong — your reason
reaches the planner as its next turn, and it revises in the same session, so a precise
rejection is cheap and a vague one costs a whole round trip.

ESCALATE to the user, rather than deciding, when:
- The plan is at or over the child cap. A feature this large is a commitment the user
  should see. (The OS enforces this too, so say what you actually think of the plan —
  your reading is what the user reads alongside it.)
- Any child would need one of the project's privileged actions to do its job — merging,
  releasing, restarting a service.
- You cannot reconcile the plan with a standing learning below.
- The ask itself is ambiguous enough that two reasonable decompositions differ, and
  picking one is really picking what the user meant.

Escalating is the safe answer and costs a little of the user's time. Releasing a bad plan
costs N worker sessions and the review of N pull requests. When genuinely torn, escalate.

Output STRICT JSON, nothing else:
  {"escalate": false, "verdict": "approve", "reason": "<one line: what you checked>"}
  {"escalate": false, "verdict": "reject",  "reason": "<what the planner must fix>"}
  {"escalate": true,  "verdict": "reject",  "reason": "<one line: why the user must \
decide>"}"""


def build_plan_question(fo: dict[str, Any], plan: dict[str, Any]) -> str:
    """The submitted plan as the reviewer reads it — a SKELETON, never the full briefs.

    Whoever decides — Neo, or the user once Neo escalates — sees only this text and never
    the planner's session, so it carries the case: the ask as the user wrote it, then the
    decomposition. The ask comes FIRST and verbatim, because the one judgement being
    asked for is whether the second answers the first.

    The full child briefs are deliberately NOT here. Production questions #65–#67
    reached 84KB by inlining every brief — text the user already reads through
    `jarvis fo show` and the dashboard, and that Neo's review of the DECOMPOSITION does
    not need. The briefs stay in the stored plan; the question names where.
    """
    parts = [
        f"FEATURE ORDER {fo['id']} — {fo['title']}",
        "",
        "The ask, as the user wrote it:",
        fo.get("description") or "(none given)",
        "",
        f"The plan the planner submitted ({len(plan.get('children') or [])} children), "
        f"one line per child — the full briefs were validated mechanically and live in "
        f"the stored plan (jarvis fo show {fo['id']}):",
        *render_plan_skeleton(plan),
        "",
        "Release this plan?",
    ]
    return "\n".join(parts)


def render_plan_skeleton(plan: dict[str, Any]) -> list[str]:
    """One line per child: key, title, edges, section, acceptance. Never the description.

    The section IS part of the skeleton, unlike the brief: the reviewer's judgement is
    whether the decomposition follows the spec's boundaries, and it cannot make that
    judgement without seeing which boundary each child claims.
    """
    lines = []
    if plan.get("summary"):
        lines.append(plan["summary"])
        lines.append("")
    if plan.get("design_doc"):
        lines.append(f"Design document: {plan['design_doc']}")
        lines.append("")
    for child in plan.get("children") or []:
        needs = f" (needs {', '.join(child['needs'])})" if child.get("needs") else ""
        lines.append(f"- [{child['key']}] {child['title']}{needs}")
        if child.get("spec_section"):
            lines.append(f"    spec section: {child['spec_section']}")
        if child.get("acceptance"):
            lines.append(f"    done when: {child['acceptance']}")
    if plan.get("justification"):
        lines += ["", f"Justification for {len(plan['children'])} children: "
                      f"{plan['justification']}"]
    return lines


def render_plan(plan: dict[str, Any]) -> list[str]:
    """The plan as lines a human reads — the reviewer's view, and `jarvis fo show`'s.

    One renderer, so Neo reviews the same text the user sees when it escalates. Two
    surfaces rendering a plan separately is how they come to disagree about what was
    approved.
    """
    lines = []
    if plan.get("summary"):
        lines.append(plan["summary"])
        lines.append("")
    if plan.get("design_doc"):
        lines.append(f"Design document: {plan['design_doc']}")
        lines.append("")
    for child in plan.get("children") or []:
        needs = f" (needs {', '.join(child['needs'])})" if child.get("needs") else ""
        lines.append(f"- [{child['key']}] {child['title']}{needs}")
        if child.get("spec_section"):
            lines.append(f"    spec section: {child['spec_section']}")
        lines.append(f"    {child['description']}")
        if child.get("acceptance"):
            lines.append(f"    done when: {child['acceptance']}")
    if plan.get("justification"):
        lines += ["", f"Justification for {len(plan['children'])} children: "
                      f"{plan['justification']}"]
    return lines
