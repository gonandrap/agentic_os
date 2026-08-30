# Spec-driven feature orders

**Status:** implemented by wo-4580e7c1.
**Supersedes:** §2, §2.1 and §2.2 of `2026-08-23-the-work-order-record.md` (the
`design_doc_by` escape hatch, removed here — see §1.1).

## 0. The complaint

`fo-e353491c` produced thirteen children whose briefs ran to several kilobytes each.
The user's verdict, verbatim: *"I won't read it; it is dup of the spec written; increases
token usage."* PR 125 had already capped a brief at 1500 characters and told planners to
cite the design document instead of restating it. That was the right direction and it
stopped short: a 1500-character brief is still a paraphrase of the spec, the child worker
still reads the whole spec to find the part that concerns it, the validation panel still
judges the work against a paraphrase, and every child is a generic worker that has to
re-derive the feature's posture from scratch.

This design finishes the move. **The spec is the artifact; everything else points at it.**

## 1. What a plan is now

### 1.1 A plan stands on a spec that EXISTS

`design_doc` is required. `design_doc_by` — a plan naming the child that will write the
spec later — is removed.

Ruled by Neo on question 179: every check this design adds (the section a child names
resolves; the Agent profile appendix is present; the agent type can be built) needs the
spec's text at submission time. Deferring them to dispatch means releasing a plan whose
children point at sections nobody has written, and discovering it one worker session at a
time. A source of truth that may not exist yet is not one. The planner reads the codebase
already and is the right author; "there is no spec yet" is precisely what a planner
session is for.

### 1.2 Every child names its section

Each child carries a required `spec_section` — a heading number or name in the spec,
resolved with `sections.extract_section`. Two rules, both mechanical:

* **Sections are unique across children.** One functional boundary, one work order. This
  is the enforcement of "the spec's design boundaries guide the split": a boundary two
  children share is a boundary the planner did not cut.
* **A child may not claim the Agent profile appendix.** It is the team's posture, not a
  piece of the work.

`spec_section` is stored on the child work order (`work_orders.spec_section`), because
three different readers need it later: dispatch materialises that section beside the
worker, the worker's prompt names it, and the validation panel judges the diff against it.

### 1.3 Briefs shrink to 600 characters

`MAX_DESCRIPTION_CHARS` drops 1500 → 600. `MIN_DESCRIPTION_CHARS` stays 80. A brief is now
what the section does NOT say: the scope boundary, what this piece must not touch, and
what done means. Everything else is one sentence pointing at the section the worker
already has in front of it.

The cap is mechanical for the same reason PR 125's was (kn-3af6a034): `_planner_prompt` had
said "a brief, not an encyclopedia" since `design_doc` existed and planners kept shipping
encyclopedias. Prose in a prompt is not a constraint.

## 2. What a spec must contain

Checked in `plans.spec_problems`, a pure function over the plan and the spec's text,
called from `ops.submit_plan` at the one moment both are in hand:

1. Every child's `spec_section` resolves to a real heading.
2. No two children name the same section.
3. There is an **`Agent profile`** section, and it is not a stub.

Nothing checks that a section is *good*. That is the plan reviewer's job, and it now has
something to review it against.

## 3. The per-feature agent type

The spec's `Agent profile` appendix is a system prompt. On plan release the OS writes it
to a Claude Code agent definition and every child work order of that feature runs AS that
agent:

```
<project>/.jarvis/features/<fo-id>/agent/.claude/agents/<fo-id>.md
```

The directory rides in on `--add-dir` and the agent is selected with `--agent <fo-id>`.

**Verified live on 2026-08-29, with a negative control** (the probe pattern kn-02cb9c88
recommends): `claude -p --agent pirate-tester --add-dir /tmp/agtest` adopted the profile as
the LEAD session's persona; the identical invocation without `--add-dir` failed with
`--agent 'pirate-tester' not found`. So `--add-dir` supplies lead-session agents exactly
the way kn-02cb9c88 established it supplies subagent types, and no new transport is needed.

The generated definition declares **no `tools:` key**. kn-44fb3e42's finding — that a
`tools:` restriction is enforced by the CLI — is what makes this deliberate rather than an
omission: a child worker must keep every tool, and a per-feature profile is not the place
to discover that it cannot run `git`.

**Lifecycle.** Written at plan release; rewritten idempotently at every dispatch from the
stored plan, so a deleted or corrupted definition heals itself and a feature released
before this change acquires one on its next dispatch; deleted when the feature order
reaches a terminal status (`completed`, `failed`, `cancelled`). `jarvis fo agent <fo-id>`
recreates it on demand from the stored spec.

**Degradation is silent and total.** No parent, no plan, no spec content, no Agent profile
section, or an unwritable directory — the child dispatches as an ordinary worker with no
`--agent` flag. A missing persona must never be the reason a work order does not run.

## 4. What a child worker is given

`dispatch.materialize_design_doc` writes two files under
`<project>/.jarvis/features/<fo-id>/`:

* `<spec-name>.md` — the whole spec, as before.
* `sections/<wo-id>.md` — **this child's section, extracted.**

The prompt names the section file first and the spec second, as the wider context to reach
for only when the section is not enough. That ordering is the token saving: the previous
prompt pointed at the whole spec and every child read all of it.

## 5. What the validation panel is given

`EvidencePacket` gains `spec_ref` (path and section name) and `spec_section` (the section's
text). `build_packet_prompt` renders it directly after the brief, under a heading that says
what to do with it: the section is what the change is judged to be aligned with, and the
brief is only the scope boundary around it.

A packet with no spec — a standalone work order, a feature released before this change —
renders exactly as it did before. The panel's existing behaviour is the null case.

## 6. What this does NOT change

The planner's team (`jarvis-architect`, `jarvis-test-lead`), the child cap, the cycle and
dangling-reference checks, the dependency graph, the round machine, the message bus, and
every state machine. This is a change to what a plan carries and what a worker reads, not
to how anything is scheduled or settled.
