# Neo reads one resolved section

Work order wo-87c1f30b. Sibling of
`docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md` (wo-3f01dcc8, merged as
#1053), and depends on it: that spec closed the DIFF door, this one closes every remaining
door a whole spec reached Neo through.

**USER DECISION 2026-10-14, recorded as Neo learning #102: NEO NEVER RECEIVES A FULL
SPEC — only a pointer to ONE specific section, resolved to that section's text for extra
context.** #1053 recorded the same decision's diff half. This is the other half.

Rulings this spec is built on and may not re-litigate:

- **wo-e4a359cb: no flag and no schema.** The reference lives inside the question's
  sentence (`sections.REF_RE`). This work order's own description suggested a `--section`
  flag; **Neo ruling Q1878 refused it** — a spec's wish for a structured pointer does not
  supersede the ruling, and two spellings of one pointer is exactly what it refused.
- **Neo ruling Q1878 (second half):** an assumption's cited section SUPPLEMENTS the work
  order's spec block, never replaces it.
- **Neo ruling Q1883: no cardinality cap.** The budget bounds the text, not a count.
- **Neo ruling Q1884:** the shared budget splits by NEED, with half as a floor.
- **Neo ruling Q1880:** plan review gets the document's heading OUTLINE, not its text.

## The problem

On wo-09c60d79 the whole 18.5k spec reached Neo's packet through the pull request's diff.
#1053 removed the diff. It did not audit the other inputs. This order's audit did, path by
path — and three of them still hand Neo a whole document or drop a pointer on the floor.

**`autoreview._ruling_question` / `_confirm_question`: COMPLIANT, re-verified against
merged `main` at 0879c2c.** A spec reaches Neo only via `_spec_block` over
`specs.spec_of` — the work order's own `spec_section` column — bounded by
`validation.assumption_spec_chars`, with the path and heading cited. No diff, no
changed-file list, no PR body, no file read from disk, no interpolation of any file's
contents.

**`ops.ask_question` (`jarvis wo ask`): bounded, with TWO GAPS.** The question is capped at
`sections.QUESTION_MAX_CHARS = 4000`; an in-text reference is resolved to ONE section,
bounded at `ops.SECTION_SNAPSHOT_CHARS = 6000`, and cited. The gaps:

1. **An unresolvable reference was NOTED IN THE CONTEXT instead of refused.** Neo then
   reads a paragraph arguing from a section it was told does not resolve — a question
   argued from nothing, answered anyway.
2. **A bare spec path with no section was IGNORED ENTIRELY.** "per
   docs/superpowers/specs/x.md, …" resolved to nothing and said nothing, so the paragraph's
   whole authority was invisible to the answerer.

**`jarvis wo assume`: NO section mechanism at all.** An assumption could cite a spec only
by pasting it or by naming it uselessly. Its body was bounded only by the Bash hook
`hooks.payload_reference_decision` — never by `ops` — so every caller that is not a hooked
Bash tool call had no bound and no resolver.

**`plans.build_plan_context` (feature-order plan review, reached by `ops._ask_plan_review`):
PASTES THE WHOLE DESIGN DOCUMENT**, up to `SPEC_CONTEXT_MAX_CHARS = 120_000`. This is the
largest violation in the system and it is deliberate machinery: the constant's own comment
sizes it so that every spec in this repo fits unclipped. 120,000 chars of spec text in a
Neo question is the thing learning #102 forbids, at 6.5x the size of the wo-09c60d79
packet that prompted the decision.

**Gates (`gates.build_request_question`), `automerge`, `remedies`, `issues.ask_triage`,
`supervisor`: COMPLIANT.** Each builds OS- or model-authored text; every borrowed field is
bounded (`provenance` limits 300/1200/2000, triage 4000); none interpolates a file.

### Root cause

The in-text reference form was built for ONE verb and one call site. `jarvis wo ask`
resolves it; nothing else does. So each later Neo channel solved "the answerer needs
design context" independently — plan review by pasting the document, `assume` by not
solving it at all — and `ask` itself treated an unresolvable pointer as a cosmetic defect
in a context string rather than as an ask with no authority behind it. The root cause is
the MISSING SHARED RESOLVER, not any one channel's choice. Fix the channels without
centralising it and the fourth channel repeats this.

## The fix

One resolver, one spelling, used by every verb; refusals instead of notes; the resolved
section snapshotted where it will still exist when the packet is built; and plan review
demoted from spec text to a structural fingerprint.

### Part 1 — one spelling, extended to `jarvis wo assume`

NO FLAG and NO SCHEMA (wo-e4a359cb, held by Q1878). The in-text form
(`from section 3 of design doc "docs/x.md": …`) is the one spelling, and the only change is
that `jarvis wo assume` now understands it too. `sections.REF_RE` stays the one parser and
`sections.find_heading` stays the one matcher; neither grows a sibling.

### Part 2 — `ops._section_blocks`, the one resolver for both verbs

Both `ops.ask_question` and `ops.assume` resolve through `ops._section_blocks`, so the two
verbs cannot drift. Same resolution order it has today (the worker's worktree, the project
tree, then the materialised feature snapshot under `.jarvis/features/`), same
`SECTION_SNAPSHOT_CHARS` bound with the cut announced, and the SAME TWO REFUSALS in both
verbs:

1. **A reference that does not resolve.** The refusal names WHICH failure it is — no such
   file in the worktree or the project tree, or no such heading in a file that exists
   (pointing at `jarvis spec toc <path>`) — and shows the in-text form.
2. **A document named with no section** (`sections.unreferenced_docs`, markdown only: a
   source file named in the same sentence is not a spec citation). Nothing to resolve, so
   nothing is delivered; the refusal shows the form.

**NOTHING IS RECORDED ON A REFUSAL** — no Neo question, no assumption row, no
`ASSUMPTIONS.md` line, no timeline event. A silently dropped reference is a question
argued from nothing, which is gap 1 above. **And the whole file is NEVER substituted for a
section that would not resolve**, which is the decision itself.

**NO CARDINALITY CAP (Q1883).** The resolver does not count references and does not refuse
a text citing two sections; the budget bounds the text. Two sections in one payload was
already the settled design — Part 4 puts the work order's spec block and the assumption's
cited section in the same packet — so a count refusal here would contradict the shape the
same spec ships. It would also break `wo ask` inputs that resolve today, and no new
refusal is retrofitted onto those.

### Part 3 — the assumption's section is snapshotted on the row

`ops.assume` resolves at ASSUME TIME and stores the result on the row: new `assumptions`
columns `spec_path`, `spec_which` and `spec_text`, added through `ProjectStore`'s
`ADDED_COLUMNS` migration like every column added after the first release.
`add_assumption` keeps its positional signature working — the three are keyword-only with
NULL defaults — and **NULL on all three means "cited no section"**, which is what every row
written before today says.

**Why there and not at packet-build time.** The review packet is built long after the
worker's worktree — the only tree the relative path was ever valid in — is gone. A pointer
resolved at read time resolves to nothing. kn-924730d7: a value written once by one path
and read by several readers needs a stored snapshot, not a per-read resolve.

### Part 4 — a SECOND spec block in both Neo assumption questions

`autoreview._ruling_question` and `_confirm_question` render the assumption's cited section
as a second block under its own heading, after the work order's spec block. It
**SUPPLEMENTS that block and never replaces it** (Q1878): the section the work order was
built to and the section the assumption argues from are different claims, and dropping the
first to make room for the second removes the authority #1035 was filed about.

The two blocks **SHARE `validation.assumption_spec_chars`. NO NEW SETTING** — Q1878. The
split is **NEED-BASED WITH HALF AS A FLOOR (Q1884)**:

- Each block takes what it needs when both fit.
- When together they exceed the budget, a block under the half floor keeps all of it and
  donates its remainder, so only the OVERSIZED block is trimmed.
- A flat half-each would starve one block while the other's half sat unused — the common
  case, since a two-paragraph cited section and a long built-to section is the usual
  shape.

Docs-after-source tiering stays the order of trimming, and **every cut is announced in
words** with the reference repeated, as `_bounded` already does: a bounded section the
reviewer cannot name is one it cannot cite.

### Part 5 — plan review gets the heading outline, not the document

`plans.build_plan_context` stops carrying spec text. It carries **every heading line of the
design document, no body, with the document path cited** (Q1880). `SPEC_CONTEXT_MAX_CHARS`
goes with the text it bounded.

**Why an outline is enough.** The judgement being asked is whether the children cover every
spec boundary, and `plans.render_plan_skeleton` already prints each child's own
`spec_section` beside it. Matching a set of claimed sections against the set of headings
that exist is exactly what an outline supports. The outline is a STRUCTURAL FINGERPRINT,
not spec text — it is the shape of the document, which is what the decomposition is judged
against.

**"Do not open this path on disk" STAYS**, with its reason (question 658 judged a revision
nobody had pulled). The outline is still the OS's own copy of the committed document, not
an invitation to go and read the checkout.

This loses the reviewer's ability to judge whether a child's BRIEF matches the section's
CONTENT. See "What this loses".

### Part 6 — worker guidance in `dispatch.py`

The worker, planner, analyst, investigator and manager briefings say to cite a spec **BY
SECTION, in-text** — never to paste one, and never to name a whole document. The second
clause is new: today's briefings teach the form for `wo ask` and leave naming a document
looking harmless, which is the shape Part 2 now refuses.

## Invariants

1. No Neo question builder, on any path, can emit a payload containing a whole spec file.
2. A refused reference records NOTHING: no question, no assumption row, no file append.
3. The whole file is never substituted for a section that did not resolve.
4. `ops.ask_question` and `ops.assume` refuse the same inputs for the same reasons, through
   one resolver.
5. An assumption's cited section never displaces the work order's spec block, and the two
   never together exceed `validation.assumption_spec_chars`.
6. There is exactly one spelling of a section pointer, and it is in-text.
7. A plan review question contains heading LINES of the design document and no body text.
8. An assumption row with NULL `spec_*` reads as "cited no section" everywhere, and every
   pre-existing row is one.

## Tests to write first

These are the work order's acceptance criteria.

1. **The cited section, and only it.** An assumption naming a spec with a section puts
   THAT SECTION's text in Neo's question and no other part of that document — assert on the
   rendered question, including a sentence from a neighbouring section being ABSENT.
2. **A document with no section is refused, in both verbs.** `jarvis wo ask` and
   `jarvis wo assume` with a text naming a `.md` path and no section both raise, and the
   message shows the in-text form. A `.py` path named in the same sentence does NOT
   trigger it.
3. **An unresolvable reference is refused in BOTH verbs, with nothing recorded.** Two
   cases, distinct messages: no such file, and a file with no such heading. After each, the
   assumptions table is empty, `ASSUMPTIONS.md` does not exist, and no Neo question row
   exists.
4. **Need-based budget.** Three cases: both blocks fit (neither trimmed); the cited section
   is under the half floor and the built-to section is oversized (the small one is intact,
   only the big one carries a truncation notice); both oversized (each trimmed, each
   announcing its cut). Assert on rendered text and on the truncation wording.
5. **Two sections are not refused.** A `wo ask` text citing two sections resolves and both
   ride — the pin against a cardinality cap being retrofitted (Q1883).
6. **Plan review carries the outline.** `plans.build_plan_context` for a plan whose design
   doc has a distinctive body sentence contains every heading line and NOT that sentence,
   and keeps the "Do NOT open this path on disk" wording.
7. **The regression pin.** For every Neo question builder, a payload built from a fixture
   spec document contains no body text of that document beyond one bounded section. One
   test over the builders by name, so a NEW channel added later without a resolver fails
   here.
8. **Migration.** A database created before the three columns opens, migrates, and its
   existing assumption rows render as "cited no section".

## What this loses, and why that is acceptable

**Plan review can no longer judge a child's brief against its section's CONTENT.** A
reviewer that read the whole spec could say "child 3 claims §4 but §4 also requires X,
which no child covers". With an outline it can only say "no child claims §4".

Accepted, for three reasons. The judgement the plan question ASKS for is coverage of the
spec's boundaries, and the outline supports exactly that. The content judgement it loses is
the VALIDATION PANEL's, which reads the delivered change against the spec section and
already names "unimplemented, contradicted or exceeded". And 120,000 chars of spec text in
a Neo question is the thing the user's decision names; keeping it for one capability would
make the decision false everywhere it is stated.

**The residual gap, stated plainly:** a spec whose headings are uninformative ("Part 3",
"Details") gives the plan reviewer almost nothing. Nothing in this spec fixes that; it is a
property of the document, and the plan skeleton's per-child sections are the cover.

## Not in scope

- The VALIDATION PANEL's inputs. It reads the full diff at `validation.diff_chars` and that
  is its job; #1053 said so and nothing here touches it.
- `specs.spec_of` and the work order's `spec_section` column. Already compliant; the
  built-to block is unchanged except for the budget split.
- The compliant channels listed in the problem section. Audited, left alone.
- Teaching the plan reviewer to read section CONTENT through a second bounded call. That is
  the residual gap above and wants its own decision.
- `hooks.payload_reference_decision`. It stays as a Bash-layer nudge; Part 2 is the `ops`
  bound that does not depend on which tool the call came through.

## Rejected alternatives

1. **`jarvis wo assume --section <path>#<heading>`** — this work order's own suggestion.
   Refused by Q1878 and by wo-e4a359cb before it. It is a SECOND spelling of a pointer that
   already has one, so every reader, every refusal message and every briefing would have to
   carry both, and a worker that learned one would write the other. The in-text form also
   keeps the citation in the sentence a human reads on `jarvis wo show`, where a flag's
   value would not be.
2. **Cap the payload at one section.** This was the shipped `_section_blocks` behaviour and
   Q1883 refused it. It contradicts Part 4 — which puts two sections in one packet by
   design — and it breaks `wo ask` inputs that resolve today. The budget is the bound; a
   count is a second bound that can disagree with it.
3. **Resolve the assumption's section at packet-build time** instead of storing it. The
   worktree the relative path was valid in is deleted by then, so the common case resolves
   to nothing — and it resolves to nothing SILENTLY, at the one moment there is nobody to
   refuse. kn-924730d7.
4. **Note an unresolvable reference in the context and ask anyway** (today's behaviour).
   The answerer then rules on a paragraph whose authority it cannot read, and the record
   shows a confident ruling with no trace that the argument was unsupported. A refusal
   costs the worker one retry and names the fix.
5. **Keep the whole design doc for plan review, bounded lower** (say 20,000 with
   `clip_at_heading`). A smaller budget makes clipping the COMMON path for this repo's
   specs — the constant's own comment says the largest is 99,602 bytes — so the reviewer
   routinely judges coverage against a document it has seen the front of. The outline is
   complete at every size.
6. **Summarise the design doc for plan review with another model call.** A model-written
   summary is a second unreviewable authority between the spec and the plan verdict, bought
   at a second price, and when it is wrong the record shows a confident review and no trace
   of why. Same rejection as #1053's diff-summary alternative.
7. **Substitute the whole file when a section does not resolve** ("better some context than
   none"). It is the decision inverted: the one input the user forbade, delivered exactly
   when the pointer was wrong.
