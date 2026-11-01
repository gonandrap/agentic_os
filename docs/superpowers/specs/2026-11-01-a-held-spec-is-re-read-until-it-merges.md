# A held spec is re-read until it merges

Issue 258. Ruling: Neo question 2928. Builds on
`2026-10-29-a-feature-spec-pull-request-lands-first.md` §2.

## The problem

The plan snapshots its design doc at `fo plan` time and `specs.spec_of` serves every
child from that snapshot. The panel judges the planner's spec PR after release. When it
rejects the PR and the planner pushes a corrected spec, nothing re-reads it: children
receive the rejected text. Worse, `spec_hold` held only while the planner was
`validating`, `waiting_pr_merge` or `needs_review`, so while the planner reworked
(`running`) children could dispatch on the rejected spec.

## The fix

### 1. The hold covers rework

`SPEC_PR_OPEN_STATUSES` is every open status. A planner carrying a `pr_url` holds its
children until it completes (its spec merged), whatever it is doing meanwhile.

### 2. The OS re-reads the held spec

`ops.refresh_held_spec`, run by `Daemon.refresh_plan_specs` on every executing feature:
while the spec is held, it reads the committed copy (`landing.committed_text`). When the
text changed and `plans.spec_problems` passes, it rewrites `design_doc_content` and
`design_doc_source`, reinstalls the feature agent and writes `plan_spec_refreshed`
(`released: true`) on the planner. Pending children read the new section at dispatch.

It keeps the snapshot and flags the feature for attention instead when the revision
breaks the plan, or when a child that is not pending (nor cancelled) would see its
section change — a started child keeps the section it was dispatched with.

## Out of scope

A manual `jarvis fo respec` (rejected: a manual repair, kn-7698ad89); making the panel
judge the planner before release (reorders plan review).
