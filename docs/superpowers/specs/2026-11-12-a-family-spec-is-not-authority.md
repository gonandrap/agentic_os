# A spec written inside a feature order is not authority for that order

io-74eab254 finding `spec-self-ratified`, wo-58726b9f.

## 1. Problem

fo-dc277eea's planner (wo-ac5be35f) committed its design doc (eaa2e413), then recorded
assumptions #2 and #6 citing it. Neo accepted both: "Confirmed: spec § 4 settles this
verbatim". Plan review took 39s, no escalation. The user never chose any of the four
interaction choices they later complained about.

## 2. Root cause

`autoreview._spec_block` and `_cited_block` (src/jarvis/autoreview.py:1138, :1174) label
EVERY spec section "authority you may CITE", and the ruling text says "where the record or
spec settles it, cite the section rather than escalate". Nothing distinguishes a spec the
user's project already held from one the same feature order wrote minutes earlier. So a
planner's spec ratifies the planner's own assumptions. A prompt nudge alone would not
change what Neo is told counts as authority.

## 3. Fix

### 3.1 Same-family spec

A spec path is SAME-FAMILY for an order whose `parent_id` is a `feature` order when the
project's default branch has no commit touching that path dated before the feature
order's `created_at` (`specs.family_written`). No git answer counts as same-family: the
OS never grants authority it cannot prove. Mechanical, so it lives in `specs.py` beside
`spec_of`, not in a prompt. Orders outside a feature family are unchanged.

### 3.2 Assumption review packet

A same-family spec block is relabelled "written inside <fo> — the worker's own claim, NOT
authority", and the packet carries the feature order's ask verbatim plus the rule: accept
only on the user's own words or a spec or learning that predates the feature order; a
user-facing interaction choice that interprets or narrows the ask escalates
(`ambiguous-intent`). A pre-existing spec keeps "authority you may CITE".

### 3.3 Bundled into the plan review

While its feature order is `planning` or `plan_review`, a PLANNER assumption citing a
same-family spec is not put to Neo on its own: the daemon records `autoreview_bundled`
once and skips it, so the user gets no per-assumption ask (Neo Q4364).

`submit_plan` stores those ids as `plan["bundled_assumptions"]` and the plan question
lists each one under the user's ask. The plan reviewer escalates the plan when any listed
choice interprets or narrows the ask; the escalation's inbox row shows the user's words
beside the choices.

`review_plan` settles the listed rows with the plan: a release accepts them, decided by
whoever released it; a rejection leaves them pending for the revised plan. A bundled
assumption recorded after release goes to Neo on its own, relabelled per §3.2.
