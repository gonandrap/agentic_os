# Questions to Neo are atomic

- Work order: wo-a5f4b999
- User decision: 2026-10-14, Neo learning #102 — questions to Neo (both `jarvis wo assume`
  under review and `jarvis wo ask`) must be concrete, granular and ATOMIC: one decision each.
  Under heavy uncertainty a new question REFERENCES earlier Neo questions by id instead of
  carrying more context. Thresholds are catalog configs, never module constants.
- Settled by: Neo Q2039 (a), Q2040 (a), Q2041 (a). Standing ruling `kn-5102e704` (Neo Q1878)
  governs the pointer spelling.
- Every design question below is SETTLED. This spec is the implementation shape only.

## The problem

**Workers bundle several decisions into one assumption or question, and nothing stops them.**
Neo then cannot rule: approving the packet approves halves it was never asked about.

Evidence — live case wo-09c60d79:

- its assumption #1 carried THREE decisions: it chose ending (a), rejected ending (b) with
  the reason, and rejected widening `review_state` — plus a pointer to a whole spec file as
  the reasoning;
- its assumption #2 mixed two different kinds of statement: "I could not verify the premise"
  and "I added new scope (fix C, `set_status` trigger stamps)". Neo ESCALATED it, because it
  could not approve one half without the other.

Root cause, two parts, both in the write path:

1. **No cap on an assumption body, and no cap a worker can discover.** `ops.assume`
   (`src/jarvis/ops.py:2227`) has no length check at all. The PreToolUse hook does cap it,
   but at `wo send`'s 6000 characters: `concision._CAPPED`
   (`src/jarvis/concision.py:115-120`) maps `("wo","assume")` to the `"message"` kind.
   `jarvis wo ask` is capped at `sections.QUESTION_MAX_CHARS = 4000`
   (`src/jarvis/sections.py:9`), which is above the bundled tail rather than inside it.
2. **The guidance gives an assumption NO SHAPE.** Every brief template says only "a call you
   made with NO doubt. Record every one": `src/jarvis/dispatch.py:600, 784, 956, 972, 1080`
   and `src/jarvis/worker_brief.py:147, 299`. `jarvis wo ask` DOES have a shape ("one
   paragraph: the decision, the options, your recommendation",
   `worker_brief.py:258-259`) — that asymmetry is why the bundling lands on assumptions.

Measured, live `jarvis_os` project store and Neo store (2026-10-21):

| Surface | n | median | p75 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| assumption bodies | 1455 | 380 | 501 | 657 | 777 | 1170 | 2340 |
| worker `question`-kind Neo questions | 497 | 1353 | 1646 | 2118 | 3190 | 4662 | 5918 |

Assumption bodies: min 10, p25 296. A 1000-char cap refuses 2.1% (31 rows); 800 refuses
4.3%; 600 refuses 13.5%. Questions: today's 4000 refuses 1.8%; 2000 refuses 12.7% — the
bundled tail the decision targets. A single 1000-char cap over BOTH verbs would refuse 83%
of real questions, which is why one number cannot serve both verbs.

Second half of the problem: a worker who is told to split has no cheap way to carry the
context forward. `jarvis wo ask` resolves NO references to earlier questions today; only the
assumption-review path does (`autoreview._cited_question_ids`, `autoreview.py:808-815`).
Splitting therefore costs a re-paste, which is what the cap is meant to prevent.

## The fix

Three mechanisms: two per-verb caps that REFUSE with a split hint, one shared in-text
reference resolver so a split costs nothing, and a guidance rewrite that states the shape.

### 1. Two caps, in the existing `concision` block

Defaults, in `src/jarvis/concision.py` beside `DEFAULT_MESSAGE_MAX_CHARS` (line 96):

```python
DEFAULT_ASSUME_MAX_CHARS = 1000
DEFAULT_QUESTION_MAX_CHARS = 2000
```

`DEFAULT_QUESTION_MAX_CHARS` REPLACES `sections.QUESTION_MAX_CHARS = 4000` as the default.
`concision.py` must stay standard-library-only (no `catalog` import — the hook runs on every
Bash command).

**Import direction flips.** `concision.py:10` currently re-exports `QUESTION_MAX_CHARS`
FROM `sections`. `sections.py` does not use the constant itself — it only defines it (the
only other consumer there is `QUESTION_WARN_CHARS = 1500`, which stays in `sections`). So
the surviving direction is: **`concision` owns it; `sections` re-exports from `concision`**
(`QUESTION_MAX_CHARS = concision.DEFAULT_QUESTION_MAX_CHARS`). Both modules stay
stdlib-only and no cycle appears. Keep `ops.QUESTION_MAX_CHARS` and
`sections.QUESTION_MAX_CHARS` as names: `tests/test_question_diet.py:492`,
`tests/test_concision_mechanisms.py:233,240` and `evals/test_question_diet_budget.py:92-96`
read them.

Catalog, on `catalog.ConcisionConfig` (`src/jarvis/catalog.py:320-325`):

```python
assume_max_chars: int = concision.DEFAULT_ASSUME_MAX_CHARS
question_max_chars: int = concision.DEFAULT_QUESTION_MAX_CHARS
```

Parser `_parse_concision` (`catalog.py:1600-1618`), on the existing `message_max_chars`
pattern: field-level override off `base`, `0` means the check is off, a negative value is
REFUSED, and the error names the setting
(`f"{where}.assume_max_chars must be 0 (off) or more, got {chars}"`). No floor clause like
`message_max_chars`' "below 1000 leaves no message that can pass" — 1000 IS the assume cap.

`catalog.example.json`'s `$comment_concision` (line 28) gains both keys: what each caps,
that over the cap is a refusal with a split hint, and that `0` switches each off. Add the
keys to the `concision` block (lines 29-32) with their defaults.

### 2. Enforcement in the CLI path

`ops.assume` (`ops.py:2227`), FIRST statement, before `find_work_order` and before
`_section_blocks`, so nothing is resolved for a body that cannot be recorded:

- over the cap: `raise OpsError` naming the length and the cap, and telling the worker to
  SPLIT — one decision per assumption, a second decision is a second `jarvis wo assume` —
  and to cite the first in the second's text by its `#n`.
- `ops.ask_question` (`ops.py:7511`) already has exactly this refusal shape at 7515-7523;
  change the constant it reads to the per-project value and add the split-and-reference hint
  (split the question, cite the first by `Q<id>`). The `QUESTION_WARN_CHARS` warning at
  7545-7550 keeps its 1500 and reports the new cap.

Per-project value: a NEW helper copied exactly from `ops.validation_config`
(`ops.py:3535-3543`):

```python
def concision_config(project: str | None = None) -> Any:
    """The concision settings in force for `project` — or the OS's — or None"""
    try:
        catalog = resolve_catalog()
        if project is None:
            return catalog.os.concision
        return catalog.project(project).concision
    except (OpsError, CatalogError, OSError, ValueError):
        return None
```

Both call sites take the fleet default from `concision` when it returns None, so a checkout
with no catalog still enforces 1000/2000. `0` switches the check off.

Over the cap is **REFUSED, never trimmed**. Nothing is recorded: no `assumptions` row, no
`ASSUMPTIONS.md` append, no Neo question row.

**Compatibility:** the caps are WRITE-time only. Assumptions already stored over the cap
review normally; nothing in `autoreview` gains a length check.

### 3. The same numbers in the PreToolUse hook

The hook must refuse before the shell expands anything, so it carries the caps as env:

- `concision._CAPPED` (`concision.py:115-120`): give `("wo","assume")` its OWN kind,
  `"assume"`. `("wo","send")` keeps `"message"` and its 6000 unchanged.
- new `ASSUME_CAP_ENV = "JARVIS_ASSUME_MAX_CHARS"` / `QUESTION_CAP_ENV =
  "JARVIS_QUESTION_MAX_CHARS"` with `assume_cap(env)` / `question_cap(env)` on
  `message_cap`'s exact pattern (`concision.py:102-111`): missing/empty/unparseable gives
  the default, a negative value gives the default.
- `dispatch._write_worker_settings` (`dispatch.py:152-155`) passes both from
  `project.concision`, beside `concision.MESSAGE_CAP_ENV`.
- `hooks.payload_reference_decision` (`hooks.py:344-356`): line 346's
  `limit = cap if kind == "message" else concision.QUESTION_MAX_CHARS` becomes a
  per-kind lookup — `"message"` → `message_cap(env)`, `"assume"` → `assume_cap(env)`,
  `"question"` → `question_cap(env)`. `0` still means no check (line 347 already handles
  it). The deny text keeps its shape and names the kind and both numbers; for `assume` and
  `question` it carries the SPLIT hint too, because the hook is the refusal a worker usually
  meets first.

### 4. References are IN-TEXT, keyword-anchored

**No `--ref` flag, no new column, no new catalog setting.** The reference lives inside the
sentence, exactly as a spec section reference does (`ops.SECTION_FORM_HINT`,
`ops.py:7459-7461`).

The existing mechanism is `autoreview._CITED_QUESTION_RE` (`autoreview.py:25-27`):

```python
r"\b(?:neo\s+question|question|neo|q)\s*#?\s*(\d{1,6})\b"
```

scanned by `_cited_question_ids` (`autoreview.py:808-815`, `_CITED_LIMIT = 5`) and resolved
by `_cited_item` (`autoreview.py:818-831`), which renders the cited question FIRST and
EXEMPT from `DECISION_RECORD_CHARS`, and already distinguishes three cases: no such
question; belongs to another work order — not shown; asked on this order, not answered.

Three changes:

**(a) The scan also matches SIBLING ASSUMPTIONS, keyword-anchored.** `assumption #2`,
`assumptions 2 and 3`. **A BARE `#n` matches NOTHING**, and that is load-bearing: assumption
and question bodies routinely carry `issue #832` and `PR #1090`, and under refuse-on-unknown
(c) a bare scan would REFUSE those commands. A cited sibling renders with its text and its
ruling — status, who decided, the reason — through the existing renderers `_user_ruling_item`
(`autoreview.py:870-879`) and `sibling_line` (`autoreview.py:753-759`); bounded as they
already are. The high-stakes withholding net `_netted` (`autoreview.py:882-885`) applies to
a cited sibling EXACTLY as it does to an uncited one — a cited high-stakes sibling is still
withheld. Siblings resolve off `ProjectStore.all_assumptions(wo_id)`
(`project_store.py:3198`) by `n`.

**(b) The same scan and resolver run for `jarvis wo ask`**, which resolves no refs at all
today. `ops.ask_question` builds `context` at `ops.py:7526-7530` and already appends each
resolved spec section to it; the resolved refs go in there the same way, bounded by
`validation.decision_record_chars` (`autoreview._record_chars`, `autoreview.py:1090-1093`,
default `DEFAULT_VALIDATION_DECISION_RECORD_CHARS = 6000`, `catalog.py:267`).

**(c) An UNKNOWN or FOREIGN ref is REFUSED at command time in BOTH verbs**, with nothing
recorded — no `assumptions` row, no `ASSUMPTIONS.md` append, no Neo question row. The model
to follow is `ops._section_blocks`' existing refusals (`ops.py:7485-7501`): the message names
WHICH ref failed and WHY ("no such question", "belongs to another work order", "this order
has no assumption #7"). An id that exists on this order but is **NOT YET ANSWERED is NOT a
refusal** — it already has a rendering ("asked on this order, not answered") and refusing it
would stop a worker citing a question it just asked. Sibling `n` is order-scoped, so a
foreign sibling cannot be spelled; "foreign" applies to question ids only.

**One scanner, one resolver, shared.** Both verbs go through the same two functions, the way
`ops._section_blocks` is already the one resolver for both verbs. They stay in `autoreview`
beside `_CITED_QUESTION_RE` and `decision_record`, promoted off the leading underscore; `ops`
reaches them with the deferred `from . import autoreview` it already uses at `ops.py:3106`
and `3123`. Resolution is NOT duplicated in `ops`.

### 5. The guidance rewrite

Every `wo assume` guidance block states the shape: **ONE DECISION** — the option taken, the
strongest alternative rejected and why — in a short paragraph; a second decision is a second
`jarvis wo assume`. Each names the cap and the split hint, and names the in-text reference
form so a worker cites an earlier question or sibling instead of pasting context.

Sites: `src/jarvis/dispatch.py:600-604, 784-788, 956-957, 972-976, 1080-1084`;
`src/jarvis/worker_brief.py:147-150, 299-305`, plus the `wo ask` guidance in
`worker_brief._question_shape` (`worker_brief.py:248, 262-264`), which prints the question
cap and must print the new one; `src/jarvis/assets/OPERATION.md.tmpl:43-50`.

`worker_brief` has a `CORE_BUDGET_CHARS = 3220` asserted by two tests — trim the existing
prose rather than raising it.

Tests that PIN the current wording and must be UPDATED, not fought:
`tests/test_decision_routing.py:30-32, 88-96, 104-112`,
`tests/test_worker_brief.py:182-191`, `evals/test_jarvis_behavior.py:211-216`.

## Rejected alternatives

1. **`--ref <qid|#n>`, repeatable.** This is how the work order's own brief asked for it. It
   is the STALE HALF under standing ruling `kn-5102e704` (Neo Q1878, superseding
   `kn-385459bb` / wo-e4a359cb): a brief describing how a pointer is SPELLED is stale when a
   standing ruling already fixed the spelling, and a second spelling is never shipped to
   satisfy both. The fixed spelling is in-text (`ops.SECTION_FORM_HINT`'s shape). Settled by
   Neo Q2040/Q2041, both option (a).
2. **One cap for both verbs.** 1000 over both would refuse 83% of real questions; 2000 over
   both leaves the assumption tail untouched. The measured distributions are an order of
   magnitude apart. Neo Q2039 option (a): two settings.
3. **Trim instead of refuse.** Trimming delivers a truncated decision to Neo and hides that
   it happened; the whole point is that the worker SPLITS. Refusal is the only outcome that
   produces two atomic records.
4. **A bare `#n` scan.** Rejected under (c): with refuse-on-unknown it would refuse every
   body containing `issue #832` or `PR #1090`, which is most of them.
5. **A new `refs` column on `assumptions`.** No new column and no new catalog setting: the
   text IS the record, the scan is deterministic, and a column would be a second place for
   the same fact to drift.
6. **A length check in `autoreview`.** Would retroactively refuse the 1455 stored
   assumptions, including the 31 over 1000. The cap is write-time only.

## Test list

In `tests/test_concision_mechanisms.py` (hook and caps), `tests/test_question_diet.py`
(`ask`), and the assumption-review tests for the resolver:

1. An over-cap `jarvis wo assume` is REFUSED and the message carries the SPLIT hint; nothing
   recorded — no `assumptions` row, no `ASSUMPTIONS.md` append.
2. An over-cap `jarvis wo ask` is REFUSED with the split-and-reference hint.
3. A per-project catalog override of `assume_max_chars` and of `question_max_chars` is
   honoured, and `0` switches each check off. A negative value is refused by the parser with
   the setting named.
4. A cited earlier question (`Q1234`, `neo question 1234`) renders into Neo's input for BOTH
   verbs, bounded by `validation.decision_record_chars`.
5. A cited sibling assumption (`assumption #2`) renders with its ruling — status, decider,
   reason — and a HIGH-STAKES cited sibling is still WITHHELD by `_netted`.
6. An unknown ref is refused in both verbs with nothing recorded; a FOREIGN ref (another
   work order's question id) is refused too, with the reason named.
7. A bare `#832` and a `PR #1090` in the body are NOT treated as refs and do NOT refuse.
8. A question id asked on THIS order but not yet answered is ACCEPTED, and renders as "asked
   on this order, not answered".
9. An assumption already stored over the cap still reviews normally (no length check in
   `autoreview`).
10. Every brief template — all five `dispatch` sites, both `worker_brief` sites,
    `OPERATION.md.tmpl` — carries the one-decision guidance and the in-text reference form.
11. The PreToolUse hook refuses an over-cap `wo assume` and an over-cap `wo ask` through the
    real hook entry point, while `wo send` keeps its 6000-char cap unchanged.

Plus the existing guard that `concision.py` imports only the standard library.

## Not in scope

- `jarvis neo ask` (project-level) shares the `"question"` kind and therefore the new cap in
  the hook; no separate `ops`-side work is specified for it here.
- No change to `QUESTION_WARN_CHARS` (1500), to `SECTION_SNAPSHOT_CHARS`, or to
  `_CITED_LIMIT = 5`.
- No change to the assumption review packet's own budgets beyond refs sharing
  `decision_record_chars`.
- No backfill, split or migration of the 1455 stored assumptions.
