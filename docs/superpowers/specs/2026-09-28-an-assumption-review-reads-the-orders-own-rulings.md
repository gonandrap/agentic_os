# An assumption review reads the order's own rulings

GitHub issue #832 (`high`, expedited). wo-035c4fec.

## The problem

The reviewer that rules on an assumption cannot see any decision already taken on that
work order, so it escalates asking the user to re-decide it.

Both packets are built from four things and nothing else — `autoreview._ruling_question`
(src/jarvis/autoreview.py:1186) and `autoreview._confirm_question` (:1231): the assumption's
`content`, the work order `title`/`description`, the `result_summary` (plus diff stat and
diff on the confirm pass), and the sibling assumptions through `autoreview.sibling_line`
(:1164). `sibling_line` reads `n`, `status` and `content` — not `decided_by`, not
`decided_reason`. No Neo question answered on that order reaches either packet. No user
message reaches either packet.

Field instances, 2026-09-27/28:

* **Q900 on wo-9b3e391f** escalated "citing a user correction I can't see (Neo question
  887)". The correction was the user's own answer to Q886 on the SAME work order
  ("[Answer from the user] Neither A nor B: build NO kill remedy") — a row in the same
  `questions` table, same `wo_id`.
* **Q879 on wo-3312682f**: "I can't confirm from here that Neo's earlier (B) meant this
  mechanism". (B) was Neo's own answer to Q877 on the same order.
* **Q847 and Q829**: the user's answer to Q820 was not visible.

Each one costs the user a review action they had already performed, plus the model call
that produced the escalation. The root cause is the packet's input set, not the persona:
no wording change can make a reviewer cite a row that was never in its prompt.

## The fix

A **decision record** block in both packets: the order's answered Neo Q&A, the user's
messages, and the user's rulings on sibling assumptions — newest first, under a character
cap, with anything a cited id names exempt from eviction.

It lives in `autoreview.py` beside `sibling_line`, because it is the same kind of thing:
pure rendering of context rows into one packet section, with a redaction net applied. The
alternative — building it in `daemon.py` and passing a string — is rejected in §7.

### 1. The query: `NeoStore.answered_questions`

New method in src/jarvis/neo_store.py, beside `open_questions` (:492), which it mirrors
(same table, same `wo_id` scoping, opposite half of the lifecycle):

```python
def answered_questions(self, wo_id: str, limit: int = 20) -> list[dict[str, Any]]:
```

`SELECT * FROM questions WHERE wo_id=? AND status='answered' ORDER BY ts DESC, id DESC
LIMIT ?`. Newest first, unlike `open_questions`' `ORDER BY ts` — the caller caps and the
newest ruling is the one that supersedes.

Columns the caller uses, all already on the table (src/jarvis/neo_store.py:138-154): `id`,
`kind`, `question`, `answer`, `answered_by` (`neo` | `user`), `ts`, plus `review_status`
and `review_feedback`.

**No `kind` filter in SQL.** `open_questions`' `kind='question'` exclusion is load-bearing
for a different reason — an `approval` row is a gate reported elsewhere. Here every kind is
a decision taken on this order, and an `approval` answered by the user is exactly the kind
of ruling #832 is about. The filtering that matters is on the RENDERING, §2.

### 2. The renderer: `autoreview.decision_record`

New pure-ish function in autoreview.py, immediately after `sibling_line`:

```python
def decision_record(store: Any, neo: Any, wo_id: str,
                    siblings: list[dict[str, Any]]) -> str:
```

Three sources, concatenated newest-first within each group, groups in this order:

1. **Answered Neo Q&A** — `neo.answered_questions(wo_id)`. One item per row:
   `Q{id} [answered by {answered_by}] {headline} -> {answer}`, where `headline` is the
   question's first line truncated to 160 chars. `answered_by` is rendered verbatim
   because "the user said" and "Neo said" are different authority and the Q879 escalation
   turned on not knowing which. When `review_status == 'corrected'`, append
   `(the user corrected this: {review_feedback})` — a corrected Neo answer is not
   authority on its own, and `neo_store.review` (:557) is where the user's correction is
   the only ruling that survives.
2. **The user's messages** — `store.user_messages(wo_id)` (src/jarvis/project_store.py:3677).
   Already newest-first and already proven user-authored: it matches `authored_by` exactly
   and excludes unstamped legacy rows, so nothing a worker could have written is in it.
   Rendered `[user message {ts}] {body[:400]}`.
3. **The user's rulings on sibling assumptions** — from the `siblings` list already passed
   to both builders. For each settled sibling whose `decided_by` is not `neo`:
   `#{n} {status} by the user — {decided_reason}`.

   **CORRECTION TO THE BRIEF:** the `assumptions` table has NO `review_feedback` column
   (schema src/jarvis/project_store.py:797, added columns :1402-1462). The user's reasoning
   from `jarvis wo review --feedback` is written to `decided_reason` by
   `ProjectStore.review_assumption` (:4166), with `decided_by` naming who decided — `''`
   or `user` for the user, `neo` for the OS (`autoreview.DECIDER`, :97). That pair is what
   this group reads; `ops.assumption_ruling_line` (src/jarvis/ops.py:3425) is the precedent
   for reading it. `questions.review_feedback` is a different column and is handled in
   group 1.

**`assumption`-kind rows are rendered as a one-line ruling and their question text is
never quoted.** `kind == autoreview.QUESTION_KIND` (:92) means the row IS a review packet —
several thousand characters containing the work order description, the diff and the
sibling list. Quoting it is recursive (a packet containing the previous packet containing
the one before) and would consume the whole cap in one item. Rendered instead as
`Q{id} [answered by {answered_by}] assumption #{n} -> {answer}`, where `#{n}` is recovered
by matching `siblings` on `neo_question_id` / `confirm_question_id`. Rendered rather than
EXCLUDED because the ruling itself is the payload — Q879's missing fact was Neo's verdict
on a sibling assumption, and excluding the kind would leave that exact case broken. When no
sibling matches the id, the item says `assumption (row not found)` rather than being
dropped: a ruling whose subject cannot be located is still a ruling.

### 3. The cap, and stating the omission

Module constant in autoreview.py, beside `QUESTION_KIND`:

```python
#: Characters of decision record one packet may carry.
DECISION_RECORD_CHARS = 6000
```

A constant, not a config key — `evidence.DEFAULT_DIFF_CHARS` (src/jarvis/evidence.py:92) and
`neo.LEARNINGS_CHAR_BUDGET` (src/jarvis/neo.py:110) are the precedent for a prompt-size
bound in this codebase, and a per-project knob on this one would be a setting nobody sets
and every test has to pin.

Items are added newest-first until the next one would cross the cap. **When anything is
dropped the block says so, as its LAST line**, per kn-1485b845: an omission stated, last.

```
  (… 4 older items omitted — the record is capped at 6000 characters)
```

Going silent is the failure this whole spec is about: a reviewer that cannot tell "no
prior decisions" from "prior decisions I was not shown" must escalate, and would be right
to.

### 4. Cited ids go in verbatim, exempt from the cap

When the assumption's own `content` names a question id, that Q&A is rendered with its
question headline AND its full `answer` untruncated, placed FIRST, and never evicted. It is
the single item most likely to be why the packet exists — Q900 escalated naming a question
id it could not see.

Recogniser, one module-level regex in autoreview.py:

```python
_CITED_QUESTION_RE = re.compile(
    r"\b(?:neo\s+question|question|neo|q)\s*#?\s*(\d{1,6})\b", re.IGNORECASE)
```

Matches every form seen in the field: `Neo question 887`, `question 887`, `Q887`,
`Neo 887`. Bounded to 6 digits so a commit hash fragment or a line count cannot become an
id. All matches are collected, deduplicated, and capped at 5 citations — a reviewer packet
is not a place to paste a bibliography, and a `content` naming six question ids is a
different problem.

Resolution rules, each testable:

* **Id not in the answered set but on this work order** (still open, or failed): rendered
  `Q887 (cited by the assumption; asked on this order, not answered)`. A pending question
  is not authority, and saying it is pending is what stops the reviewer treating silence as
  a ruling.
* **Id on ANOTHER work order**: rendered `Q887 (cited by the assumption; belongs to
  another work order — not shown)`. NOT quoted. Content from a different order has not been
  through this order's evidence gates, and the worker citing it does not make it this
  order's record.
* **Id does not exist at all**: rendered `Q887 (cited by the assumption; no such
  question)`. Stated, not swallowed — a worker citing a question id that was never asked is
  a fact about the assumption the reviewer is ruling on.

Resolution needs a single-question lookup by id scoped to the work order; `NeoStore.get`
already exists (used by `neo_store.review`, :558) and returns the row with `wo_id`, so the
three cases above are decided on the returned row's `wo_id` and `status` with no new query.

### 5. Filtering: the secret net only

Every record item goes through `autoreview.secret_marker_text` (src/jarvis/autoreview.py:590)
and through **nothing else**. An item whose text carries a credential shape is replaced by
a classification line, same shape as `sibling_line`'s withholding:

```
  Q887 (withheld — carries a line assigning API_KEY)
```

Withheld, never dropped silently: the marker names the SHAPE and never the value, which is
the contract `secret_marker_text`'s docstring states.

**The high-stakes net (`high_stakes_marker`, :381) is NOT applied here**, and that is a
deliberate departure from `sibling_line`. User ruling on Neo question 943, 2026-09-28,
verbatim:

> Go with (A): run only the secret net on each record item and show answered rulings in
> full, even when they are worded as high-stakes. Hiding a ruling the user already gave is
> exactly the #832 bug.

The argument `sibling_line` rests on — an assumption naming production or a credential is
the user's alone to decide, so its text must not reach a model ruling on the row beside it
— does not transfer. Every item in the decision record is SETTLED BY CONSTRUCTION: an
answered question, a message the user sent, an assumption already decided. There is no
decision left to withhold, and withholding it reproduces #832 one table along. The secret
net stays because it is about a credential reaching a model at all, which is true whoever
decided what.

The high-stakes net keeps its job everywhere it had one: `decide`/`decide_early` condition
7, `read_ruling`'s allowlist backstop, and `sibling_line` over PENDING siblings. This spec
touches none of them.

### 6. Telling the reviewer it may cite

Present-and-unused is the likely failure. Two minimum edits:

* `ASSUMPTION_REVIEWER_PERSONA` (autoreview.py:1096) gains one paragraph, in the shape of
  the existing `A SIBLING MARKED (withheld …) IS NOT A BLANK TO FILL IN` paragraph:
  the decision record is what has already been decided on this order and it is AUTHORITY —
  accept citing it by id rather than escalating to ask for it again, and a `(withheld …)`
  or `(not answered)` item is not a ruling.
* The closing `answers` text in each builder (`_ruling_question`:1211, and the final
  string of `_confirm_question`:1280) gains one clause: when the decision record settles
  it, say which id in `reason`.

Nothing else in the persona changes. Every added word is a word in every assumption-review
prompt the fleet sends.

### 7. Plumbing

Both builders take the two stores and the work order id they already have around them:

```python
def _ruling_question(project, wo, assumption, siblings, *, early=False,
                     record="") -> str
def _confirm_question(project, wo, assumption, siblings, stat, diff,
                      record="") -> str
```

`record` defaults to `""` — an empty record renders the section as `(no prior decisions
recorded)`, so a caller not yet teaching them one is merely unaware, never wrong, and the
existing unit tests that call the builders directly keep passing.

`propose` (:1318) and `propose_confirmation` (:1285) call
`decision_record(store, neo, wo["id"], siblings)` and pass the result. Both already take
`store` and `neo`; their callers in src/jarvis/daemon.py — `propose_confirmation` at :5555
and `propose` at :5571 — already hold both stores and change not at all. That is why the
block is built in `autoreview` rather than in the daemon: the daemon half of this module is
the part that runs git and calls models, and a string assembled there would be one more
argument threaded through `Daemon.auto_review` for no gain, with the rendering out of reach
of the pure unit tests that cover every other line of packet text.

The section is placed AFTER the sibling list and BEFORE the closing `answers` text in both
packets, headed:

```
# What has already been decided on this work order — authority you may CITE
```

Last of the context blocks on purpose: it is what the reviewer should read just before it
answers, and it is the block whose absence produced the escalation.

## Tests (tests/test_autoreview.py; the confirm-pass mirrors in tests/test_autoreview_confirm.py)

1. A question answered on the order appears in the packet WITH its `Q{id}`, and
   `answered by user` when `answered_by == 'user'`.
2. With more items than `DECISION_RECORD_CHARS` allows: the newest survive, the oldest are
   absent, and the omission line is present and LAST.
3. An assumption whose `content` says `Neo question 887` puts Q887's full answer in the
   packet even when the cap would otherwise have evicted it, and places it first. Plus one
   case each for a cited id that is unanswered, one on another work order, and one that
   does not exist — asserting the three distinct lines from §4, and that the other-order
   question's TEXT is absent.
4. A `kind='assumption'` row whose `question` is a 5000-char packet: its question text
   never appears in a newly built packet, and the row is present as a one-liner naming the
   sibling number.
5. An item carrying `API_KEY = "sk-live-…"`: the packet contains the withheld
   classification line, does not contain the value, and the item is not silently absent.
6. A sibling assumption settled by the user with `decided_reason` set reaches the packet
   with that reason; one settled by `neo` is rendered as Neo's, not the user's.
7. Both `propose` and `propose_confirmation` produce a packet containing the section
   header, with no daemon and no model call.

## Not in scope

* **The escalation path itself.** A reviewer that still escalates with the record in front
  of it is behaving correctly; this spec only removes the case where the record was absent.
* **Retro-fixing Q829/Q847/Q879/Q900.** Those are escalated and the user's to answer;
  `propose` asks one question per assumption for its whole life (:1323) and re-asking would
  be the OS lobbying.
* **The pending-sibling withholding rule.** Unchanged, deliberately (§5).
* **A config key for the cap.** §3.

## Uncertain

* `DECISION_RECORD_CHARS = 6000` is a judgement, not a measurement. The packets already
  carry up to 2000 chars of description and 150k of diff on the confirm pass, so 6000 is
  small beside the diff and large beside the assumption; if the cap line shows up on most
  orders it is too small.
* Whether group ORDER should be strict-newest-first ACROSS the three groups rather than
  grouped. Grouped is specified because `answered_by` and provenance differ per group and a
  merged stream would need a per-item source label to stay readable.
