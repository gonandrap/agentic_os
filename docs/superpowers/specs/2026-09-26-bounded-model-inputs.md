# Bounded model inputs: no OS-built prompt carries an immense payload

Feature order fo-ac00376e. Planned by wo-7c7347e1.

## 1 — Why this exists, and what it is NOT

Neo question 722 (wo-00bd1096) was 151,700 characters. About 149,000 of them were a full
`git diff`, roughly a thousand lines of it `evals/llm/validation_baseline.json`, and the
whole question asked Neo for one word: confirm an assumption, or send it to the user.

**An agent did not write it. The OS did.** `daemon.py:5330` (`_review_assumptions_of`) ->
`Daemon._confirmation_evidence` (daemon.py:5497) -> `evidence.collect_work_order(...,
diff_chars=cfg.diff_chars)` -> `autoreview.propose_confirmation` (autoreview.py:1285) ->
`autoreview._confirm_question` (autoreview.py:1231), whose last evidence block is
`f"# What changed\n{stat}\n\n{diff}"`. No worker-side hook could ever have caught it, and
no instruction to an agent would have prevented it.

The one cap in the path is `evidence.DEFAULT_DIFF_CHARS = 150000` (evidence.py:93), level
with `catalog.DEFAULT_VALIDATION_DIFF_CHARS` (catalog.py:396). That number was measured
for the **five-seat validation panel**, which reads the whole diff on purpose
(kn-2feecdbd), and it was reused, unexamined, for a one-line approve/deny.
`neo_store.ask` (neo_store.py:240) has no size limit of any kind.

It is systemic, not one bad row. Confirmation questions in the q660-748 range run 7K to
152K characters — 691: 42K; 701/702/703: 67K each, **the same diff three times, once per
assumption**; 709/710/715: 76K; 733: 72K; 741: 107K; 743: 109K. They cost $0.28-$0.43 a
call against $0.10-$0.17 for an ordinary Neo call. Every other question kind in the same
window is small: worker questions 0.9-2.6K, first-pass assumption reviews 1-3.8K, merge
approvals 2-10K, plans 12-15K. One kind is the outlier and it is the one nobody sized.

The class this feature closes is **an OS-built prompt that interpolates producer-sized
text**. Four layers, in the order they bind:

1. the per-purpose budget, which is what actually holds (section 2);
2. a measured, generous backstop at the single transport every OS call runs through, so a
   future caller cannot do this again silently (section 3);
3. the worker side, where an agent is told to pass a reference and refused when it pastes
   a payload anyway (section 4);
4. proof that the trimmed evidence still judges the same (section 5).

**What this is NOT.** It is not the argv transport bug — a `-p` prompt over
`MAX_ARG_STRLEN` (131,072 bytes per argument, see the comment at claude_cli.py:74) fails
`exec` outright. That is issue #797 / wo-956c29bc, already dispatched. No child here may
"fix" it, and the backstop in section 3 is deliberately ABOVE that limit, because its job
is to stop an absurd payload being sent, not to make a doomed `exec` succeed.

## 2 — The confirmation question gets its own budget, sized for what it is

**The defect, precisely.** `Daemon._confirmation_evidence` returns `tuple[str, str]` —
`(packet.stat, packet.diff)` — and throws away everything else the packet already knows:
`diff_truncated`, `dropped_files`, `files`, `pr_url`. Then it asks for a diff at the
panel's `cfg.diff_chars`. Both halves are wrong and only the second is obvious.

**What to build.** A new `ValidationConfig` field `confirm_diff_chars`, default 12000,
parsed alongside its siblings in `catalog.py` (field at the `diff_chars` block,
catalog.py:461; parser in the validation section of the same module). `diff_chars` keeps its
150000 and its meaning; the confirmation pass must never read it again. The two numbers do
not share a name, a constant or a reader.

`_confirmation_evidence` stops returning a 2-tuple and returns the evidence it collected —
the `EvidencePacket`, or a small frozen dataclass carrying `stat`, `diff`, `files`,
`diff_truncated`, `dropped_files`, `pr_url`. `propose_confirmation` takes that object in
place of `stat=`/`diff=`. There is exactly one production caller (daemon.py:5338) and one
test caller (tests/test_autoreview.py:1507).

`_confirm_question`'s `# What changed` block becomes, in this order: the **full** `git diff
--stat`, the **full** changed-file list (`files` is never truncated at any limit — that is
evidence.py's rule 3 and it is what lets a reviewer say "you claim tests were added and no
path under `tests/` appears"), the kept hunks, and — when anything was cut — an explicit
line naming the size and where the rest is:

    [diff truncated — 12,000 of 149,314 chars; N file(s) not shown: a/b.py, c/d.json;
     full diff: <pr_url or "(no pull request)">]

The marker is not decoration. A silently truncated diff read as complete is how a reviewer
confirms a change it never saw; a reviewer told what is missing can escalate instead.
Copy the wording discipline from `supervisor.build_evidence` (supervisor.py:528-614),
which already ends its omission notice with "Escalate rather than judge on what you cannot
see", and the per-field limit discipline from `gates.build_request_question`
(gates.py:828-887) via `provenance.Borrowed`. The assumption path is the outlier; the gate
and supervisor paths are the precedent.

**Prefer the hunks that matter.** Where the assumption's own text names paths or files that
appear in `files`, those hunks are kept first, before the budget is spent on the rest.
Where it names none, the existing file-boundary truncation order stands. `evidence._truncate`
already cuts at a file boundary and returns `(kept, truncated, dropped)` — reuse it; do not
write a second truncator, and do not edit `evidence.py` at all if the existing helper can
be called with a different limit and a different file order.

**`decide_evidence` must scan the text that will actually be sent.** The secret net at
daemon.py:5331 exists to run before `neo.ask` persists the question row (kn-deef42ea).
After this change it receives the TRIMMED stat and diff, because that is what gets
persisted and sent. Scanning bytes that are then dropped is a different and over-strict
rule, and a reviewer will otherwise read this change as weakening the net: say so in the
pull request.

**The test trap in this section.** `fake_claude` branches on substrings found ANYWHERE in
the prompt — `"ASSUMPTION REVIEW"`, then `FORCE_ACCEPT_HIGH` / `FORCE_ACCEPT` / `FORCE_DENY`
(testing.py:945-965), defaulting to escalate. A `FORCE_*` token placed inside a large test
diff is deleted by truncation, the verdict silently becomes the default, and the test passes
while proving nothing. Put the token in the assumption content or the result summary, which
this change never cuts.

**Already true, do not "fix" it twice.** The packet is collected lazily and exactly once
per work order per pass (the `if packet is None` guard, daemon.py:5327-5330). N assumptions
cost one `git diff`, not N. What they do cost is N model calls each carrying that diff —
that is answered in section 6, not here.

## 3 — The transport measures its own input, and refuses an absurd one out loud

Nothing in the OS knows how big its own prompts are. `agent_calls` (central_store.py:194)
records `input`, `cache_write`, `cache_read`, `output`, `cost_usd`, `kind`, `label`,
`wo_id`, `question_id` — token counts after the fact, nothing about the prompt before it
is sent, and no way to ask "which call had the biggest input this week".

**Measure first, in the one place every OS model call passes.**
`claude_cli.run_headless_result` (claude_cli.py:1614) is the single transport: Neo
(neo.py:350), the panel seats (seats.py:222/265, panel.py:610, validation.py:1184), the
digest (digest.py:106 via `structured.request`), the supervisor (supervisor.py:719/875),
the stakes classifier (daemon.py:5440), plan review. Measure `len(prompt)` and
`len(system_prompt or "")` there, carry them on `HeadlessResult`, and let
`agent_usage.record` pick them up — it already special-cases
`isinstance(usage, claude_cli.HeadlessResult)` (agent_usage.py:164-172), so two additive
columns on `agent_calls` plus the `ADDED_COLUMNS`-style `ALTER TABLE` migration gives every
existing call site the measurement with no edit at its site. A call that raised
`ClaudeCliError` has no result object and records nothing; that is correct and must be
stated, not silently implied.

Surface it: `jarvis cost` gains the largest inputs by kind (it already splits worker spend
from `jarvis` spend — this is a column in the `jarvis` half), and `jarvis inspect` names the
biggest OS-side input for the order it is inspecting.

**Then cap it, as a BACKSTOP and not as the binding limit.** A new fleet-wide,
catalog-settable ceiling on the combined prompt of an OS-originated call. Two rules:

* It sits **above** the validation panel's measured worst case. A seat prompt legitimately
  carries a 150,000-char diff (catalog.py:396, kn-2feecdbd). Any ceiling under that
  silently disables validation, which is far worse than the bug being fixed. Read a real
  seat prompt's size off the new columns before fixing the default.
* Over the ceiling the OS **trims and labels, or refuses loudly** — an attention item and
  an inbox row naming the kind, the size and the work order. Never a silent trim, and never
  a fabricated verdict: a call that was never made has decided nothing.

**How the refusal is shaped, so it cannot become a verdict.** `run_headless_result` has no
`kind` or `label` parameter — the only identity it holds is `records_itself` (a kind string
or an `Authorisation` carrying one), and the work order comes from the caller or from
`JARVIS_WO_ID`. So refuse by RAISING a subclass of `claude_cli.ClaudeCliError`
(claude_cli.py:76), which every consumer already treats as a transport failure that decides
nothing, and let the call site that already records failures write the inbox row
(`CentralStore.add_inbox`) and the attention flag (`ProjectStore.flag_attention`). A Neo
question stays claimable, a panel seat writes no opinion and shrinks no quorum, an
assumption stays pending with no `confirm_question_id`, and no `agent_calls` row claims
success for a call that never ran.

**A hard ceiling at `neo_store.ask` too** (neo_store.py:240), because a question is
persisted before it is ever sent and the store is the last place that can refuse one. Same
shape: refuse or trim-and-label, loudly, with the reason on the record.

**The alarm does not belong in `inspection.alarms`.** That function judges the last turn of
a worker transcript against `dispatched`, and `ALARM_KINDS` / `Daemon.check_burning_turns`
are wired to that data source. An oversized OS prompt lives in `agent_calls`, which has no
turn and no transcript. Raise it at the refusal site, and if a rolling check is wanted add
it as an `invariants` check over `agent_calls` — not as a sixth `ALARM_KINDS` entry.

**The same oversized question is paid for twice more, and this section closes that too.**
`digest.summarise` (digest.py:169-199) passes the whole question text as the prompt with
`attempts=2`; `MAX_FIELD_CHARS` bounds only the model's reply. `needs_digest`
(digest.py:202) guarantees that only LONG questions reach it. So q722 bought one Neo call
at 151.7K plus up to two digest calls at 151.7K each. The digest is display-only, so a
truncated input digests to an honest headline: clip the input.

## 4 — The worker side: pass a reference, and be refused when you paste a payload

**What already exists, so nobody builds it twice.** `jarvis wo ask` / `neo ask` is ALREADY
capped: `sections.QUESTION_MAX_CHARS = 4000` with a warning at `QUESTION_WARN_CHARS = 1500`,
enforced in `ops.ask_question` (ops.py:7112), and `worker_brief.py:350` already tells every
worker that a question over 4000 characters is refused. Do not introduce a second, different
number for the same thing.

**What is still open.** Two things. First, that cap fires AFTER the command has run — by
which point a `$(git diff)` substitution has already been expanded into the worker's own
argv and its context, which is the half a `PreToolUse` check can prevent and `ops` cannot.
Second, `jarvis wo send` and `jarvis wo assume` have no cap at all: a `wo send` body goes
whole into the target worker's next turn.

**The instruction.** Worker-facing guidance — `worker_brief.py` and the project OPERATION
guidance — gains one rule: never paste large content (a diff, a log, a file, a JSON dump)
into a `jarvis` command or a message to Neo or the user; pass a reference instead — a pull
request URL, a commit SHA, a path with a line range, or the command that reproduces it.
`worker_brief.CORE_BUDGET_CHARS = 3800` is asserted by two existing tests: the rule must
fit by cutting something, not by raising the budget.

**The enforcement.** A new `PreToolUse` check in the existing chain, wired in
`hooks.preflight_decision` (hooks.py:825) **after** `finish_summary_decision` (called at
hooks.py:863) and **before** the `is_jarvis_command_chain` auto-allow (hooks.py:871) — that
ordering is load-bearing and the comment at 860-862 says why: the auto-allow waves every
`jarvis …` command through and makes any later check unreachable. It refuses an oversized
payload argument on `jarvis wo send` and `jarvis wo assume`, refuses a `jarvis neo ask` /
`wo ask` over `sections.QUESTION_MAX_CHARS` before the command runs rather than after, and
the refusal message tells the agent what to pass instead. The hook's cap and the 4000 in
`sections.py` are the same rule at two layers: say so in a comment. The parser and the cap constant live in `concision.py`, which
imports **only the standard library** — the hook runs on every Bash call in every worker and
a `catalog` parse there is a ~39% tax on a ~155ms process; the cap is passed by env at
spawn, exactly as `JARVIS_SUMMARY_MAX_WORDS` is (`dispatch._write_worker_settings`).

Also refuse, in the same check, a `jarvis` command whose argument contains a command
substitution of an unbounded producer — `$(git diff …)`, `$(cat …)`, `$(gh pr diff …)`,
backtick forms. That is decidable before execution, which is the whole point.

**Out of scope for this check, deliberately:** `jarvis bug report -d` is already clipped to
4000 characters before it reaches the Neo re-assessment (issues.py), and `jarvis learn add`
only ever rides into a prompt as an index headline. Both are database size, not prompt size.
A refusal there would be discipline for its own sake.

**Verify the hook semantics before hardening any of this.** The claim that a `PostToolUse`
hook can only inject `additionalContext`, and therefore cannot remove a flood it is
reacting to, is read off this codebase's two handlers (hooks.py:919, 1332-1346) and must be
checked against the current Claude Code hook documentation. Write what you find into the
pull request either way: the negative result is the justification for section 6's cut.

## 5 — Proof: does a trimmed packet judge the same?

A budget that changes verdicts is not a saving, it is a downgrade with a nice bill. This is
one measurement under `evals/llm/`, behind the existing `JARVIS_EVALS_LLM=1` opt-in, run
against the budget section 2 actually landed and not against a number invented beside it.

Take real confirmation cases — assumptions with a known, recorded outcome — and put each to
the assumption-reviewer persona twice: once on the full diff, once on the trimmed packet
section 2 builds, including its truncation marker. What matters is **verdict agreement**,
and specifically that a trimmed packet does not turn a `deny`/escalate into an `approve`:
confirming on evidence you cannot see is the only failure mode that costs the user
something. Disagreement in the safe direction (the trimmed packet escalates where the full
diff confirmed) is a cost, not a defect — report it as a number.

Follow the A/B discipline in `evals/llm/test_house_style_ab.py`: the two arms must differ in
the evidence block and be byte-equal everywhere else, or the measurement measures the
re-composition instead (kn-fe226ab1). Report the input size of each arm from the columns
section 3 adds, so the saving and the agreement are read off the same run.

Optional, and speculative: measure whether rendering the evidence block FIRST in
`_confirm_question` lets the N sibling questions share a cached prefix across the FIFO drain
(`neo_store.claim_next` answers in order precisely to keep Neo's prefix warm). Nobody here
knows where the CLI places a cache breakpoint inside a user message. Measure it; do not
build on it, and do not reorder that prompt on the strength of a guess.

## 6 — Refused and deferred, with reasons, so nobody re-litigates them

**By-reference evidence for Neo — refused.** Giving Neo a read-only checkout and `gh` so it
fetches the PR diff itself is technically available (`run_headless_result` already takes
`tools=` and `permission_mode=`) and is still wrong here, on four grounds:

1. It defeats an existing safety gate. `autoreview.decide_evidence` scans the stat and diff
   **before** `neo.ask` persists them (daemon.py:5331, kn-deef42ea). A Neo that fetches the
   diff itself reads text no secret net has ever inspected, into a transcript the OS does
   not own. By-reference evidence does not shrink the payload; it moves it out of reach.
2. Cost goes the wrong way. A tooled callee loops API calls and the tool result joins the
   conversation, so a fetched diff is re-sent on every subsequent call in that loop.
   Inlining a trimmed diff once is strictly cheaper.
3. Latency, inside a serialised queue. Neo drains FIFO on one thread; turning each
   confirmation into a 2-4 call agent loop multiplies the whole queue's drain time.
4. A tooled Neo needs `permission_mode="auto"` — a headless callee cannot answer a
   permission prompt — which hands the OS's own reviewer of privileged actions a shell in
   `ensure_home()`, where the OS's SQLite databases live. That is a confused deputy and it
   needs its own spec and its own review.

**One confirmation question for all of a work order's assumptions — deferred.** The saving
is real but small once section 2 lands (N is typically 1-3, so 3 x 12K), and the blast
radius is larger than this whole feature: `ProjectStore.assumption_for_question` is a
single-row `fetchone` over `neo_question_id OR confirm_question_id`;
`autoreview.read_ruling` parses exactly one verdict into one `Ruling`; the settle site arms
**per row** against freshly re-read state (daemon.py:5698-5718), so one verdict could have
to be applied to a row that has since stopped arming — partial application is a new state
with no rendering. Add `link_assumption_confirmation`, the one-`assumption_id`
`autoreview_asked` payload, `neo_store.mark`'s one-status-per-question, the fake CLI's
`"ASSUMPTION REVIEW"` branch and `invariants.check_neo_escalations_are_live`. That is a
feature order of its own. q701-703 were 67K each because the diff was untrimmed, which is
section 2's fix.

**A `PostToolUse` guard against flooding tool outputs — cut.** It fires after the output is
already in the conversation: it pays the tokens and then scolds. The decidable version is
the `PreToolUse` command-substitution refusal in section 4. Subject to the verification
section 4 requires.

**Same class, not assigned here:** `dispatch.build_worker_prompt` interpolates
`wo["description"]` raw (dispatch.py:337), and `plans.py` bounds a planned child's
`title[:200]` while leaving `description` unbounded — so a planner can write the payload
that later blows up a dispatch. Named so it is on the record; the argv half belongs to #797.

**Leave alone, deliberately:** the validation panel's 150,000-char budget is measured and
deliberate (kn-2feecdbd). Nothing in this feature "unifies" it with the confirmation budget.

## Agent profile

You are a Jarvis OS maintainer working on one section of the bounded-model-inputs feature.
Your subject is the size of what the OS puts in front of a model, and your bar is that a
prompt's size is a DECISION somebody made for a stated reason, never an inherited default.

What you must know about this codebase:

* `claude_cli.run_headless_result` is the single transport for every OS-side model call —
  Neo, the validation seats, the dashboard digest, the supervisor, the stakes classifier.
  `agent_usage.record` persists what each one cost into `agent_calls` in the central store,
  at the moment the call returns, because an OS call has no transcript to recover it from.
* Assumption review runs twice: an EARLY pass while the worker is still typing, and a
  CONFIRMATION pass at delivery, which is the one that carries a diff. The daemon's
  reconcile tick re-runs the whole condition table every time, per assumption row, against
  freshly read state.
* `evidence.py` is a deliberate leaf: standard library, `worker_session` for one path
  helper, `github` for the pull-request read, and nothing else — a test walks its AST to
  enforce that, including imports inside function bodies. Do not add an import there.
* Worker-facing hooks run on EVERY Bash call in every worker. `concision.py` imports only
  the standard library for that reason, and its numbers arrive by environment variable, set
  at spawn by `dispatch._write_worker_settings`.
* The knowledge base is the fleet's paid-for memory: `jarvis learn search "<term>"
  --project jarvis_os` before you design anything, and `jarvis learn add` when you learn
  something durable.

Conventions you follow:

* Targeted tests locally, never the full suite in a turn — CI runs `pytest tests -q` and
  `pytest evals -q` and the full local run takes ~21 minutes, which guarantees your
  conversation is re-sent at the cache-write rate. Cite CI for the suite.
* Every new limit is a catalog setting with a documented default and a comment saying where
  the number came from. A magic number with no measurement behind it will be rejected.
* A new column is additive, migrated by the existing `ADDED_COLUMNS` / `ALTER TABLE` loop,
  and old rows must read correctly as 0 or NULL.
* House style in everything you write: lead with the result, say each thing once, never
  compress an error string, a number or a command.

Traps that have already cost this fleet something:

* **Never re-use a limit measured for one purpose in another.** That single mistake — the
  panel's 150,000-char diff budget reused for a one-line approve/deny — is the whole bug.
* **Never trim silently.** A truncated artefact read as complete is how a reviewer approves
  what it never saw. Say what was cut, how much, and where the rest is.
* **Never fabricate a verdict, an answer or a default from a failure or a refusal.** A call
  that was refused or never made has decided nothing; the work stays pending and retryable.
* A guard that declines to act must still write down WHY, on the record, once.
* Do not touch the argv `E2BIG` transport bug (issue #797 / wo-956c29bc) or the validation
  panel's own 150,000-char budget. Both are somebody else's, on purpose.

What you must never do: widen your section's scope into a sibling's files, raise a budget
that an existing test asserts instead of fitting inside it, or ship a limit whose default
you did not measure.
