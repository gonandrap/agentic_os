# `jarvis learn search` returns an index, and dispatch hints at title matches

Work order wo-2b132bd2. Evidence: improvement order io-edacb3ea, finding on knowledge-base
reads. Decisions settled by Neo question 1233 — recorded below as decisions, not reopened.

## The problem

Two failures at once, in opposite directions, both in how knowledge reaches a worker.

**1. The retrieval verb pays for the whole base.** `jarvis learn search` prints the FULL
BODY of every match. `cli.cmd_learn`, search branch at `src/jarvis/cli.py:4314-4322`:

```python
rows = central.search_knowledge(args.term, limit=args.limit, ...)
central.record_knowledge_read("search", rows, term=args.term, ...)
_print(rows, args.json)          # rows carry `content` — every body, in full
```

Measured over the fleet:

| Figure | Value |
|---|---|
| knowledge reads | 988 |
| chars fetched | 21,180,000 |
| mean per read | 21,437 |
| mean per order that read at all | ~89,747 (~26k tokens) |
| reads that were `search` | 436 of 988 |
| entries in base | 615, median body 1,854 chars, max 7,118 |
| `learn search` default `--limit` | 20 (`src/jarvis/cli.py:1351`) |

~26k tokens of knowledge text per order is not paid once: it lands in the worker's
conversation and is re-sent on every subsequent API call in that session. A default-limit
search over median entries is ~37k chars in one tool result.

The shape that fixes this already exists one layer away and was never applied to the
retrieval verb. `central_store.knowledge_brief` (`src/jarvis/central_store.py:987-1046`)
and `dispatch.render_knowledge_block` (`src/jarvis/dispatch.py:262-310`) ship headline + id
under a char budget and make the worker fetch bodies on demand; `jarvis learn list` already
degrades to `digested()` rows (`src/jarvis/cli.py:4275-4290`) unless asked for `--full`.
`search` is the one verb with no index form.

**2. The opposite failure, same measurement window.** 17 orders completed having read
nothing at all; 15 of those had an entry matching their OWN TITLE, recorded before they
started. Cause: 586 of 615 entries never reach the prompt index — a typical brief reports
"25 indexed, 4 pinned, 586 overflow", because `knowledge_brief` fills `digest` to
`digest_limit`/`digest_chars` by topic round-robin and rolls the rest up as a topic count
(`central_store.py:1035-1045`). A worker whose relevant entry sits in that overflow has
nothing pointing at it and must guess a search term. The join that would have pointed at it
is already written, but only for the post-mortem report:
`ops.knowledge_usage_report`'s `could_have_read` (`src/jarvis/ops.py:13557-13567`) runs the
work order's title through `central_store.search_knowledge` and keeps the hits.

**Root cause, stated plainly:** knowledge retrieval has a bounded-index design that was
applied to the push path (the dispatch prompt) and never to the pull path (the `search`
verb), and the index selection has no term-relevance input at all — only recency and topic
round-robin — so relevance-matched entries are exactly what falls into the overflow. Half 1
fixes the pull path; half 2 adds one relevance signal (the order's own title) to the push
path. What this work order does NOT fix: the index selector stays recency + round-robin for
everything other than the title hint, and synonym retrieval remains unsolved
(`search_knowledge`'s own closing note, `central_store.py:875-878`).

## The fix

### Half 1 — `jarvis learn search` returns an INDEX

**Where:** `cli.cmd_learn`, the `kn_cmd == "search"` branch, `src/jarvis/cli.py:4314-4322`.
Not in `central_store.search_knowledge`: that method is also the dashboard's read
(`src/jarvis/search.py:127`), `ops.knowledge_usage_report`'s entry sweep
(`ops.py:13530`) and the `could_have_read` join (`ops.py:13561`), all of which need
`content`. The store keeps returning rows; the CLI decides what it prints. Same seam
`list` already uses.

Row shape — a new local beside `digested`, call it `excerpted(rows, term)`, reusing
`digested()` for the common fields and adding two:

| Field | Source |
|---|---|
| `id`, `project`, `topic`, `pinned` | `digested()` as today |
| `headline` | `central_store.headline(r["content"])` — unchanged |
| `retired` | `digested()`'s existing marker, preserved verbatim |
| `chars` | `len(r["content"])` — so the reader can price `jarvis learn show <id>` before paying for it |
| `excerpt` | first body line containing a query word, passed through `headline(line)` |

Excerpt rules:

1. Query words are the term split on whitespace, filtered to words containing an
   alphanumeric — the same filter `central_store.fts_query` applies
   (`central_store.py:65`) and the same words `_search_like` scores with
   (`central_store.py:920`). Match case-insensitively on the line.
2. Bound by calling `headline(line)` on the chosen line. `headline` already collapses
   whitespace and truncates at `HEADLINE_CHARS = 160` with a trailing `…`
   (`central_store.py:41,68-77`). **Do not add a new module constant**; the reuse IS the
   bound.
3. Omit `excerpt` entirely when the only matching line is the first line — the headline
   already shows it, and printing both doubles the row for nothing.
4. Omit it when no body line matches (an FTS5 stem hit, or a hit on `topic`/`tags`). The
   row is still a hit; it just has nothing to quote.
5. Label it in the rendered output as an excerpt, so no reader can mistake 160 quoted chars
   for the entry.

Not done, deliberately:

- **No `--full` on `search`.** `jarvis learn show <id>` becomes the only verb that emits a
  body. A flag would be re-added to every worker prompt within a month and the measured
  cost would return.
- **`jarvis learn list --full` is untouched** (`cli.py:1345`, `cli.py:4313`). Out of scope.
- **No truncation of a body mid-text.** The deliverable is a different SHAPE, not a shorter
  body. An ellipsised ≤160-char line LABELLED as an excerpt is a different shape; a body cut
  at N chars is the same shape, lying. This is the acceptance distinction for review.
- **Pinning unchanged.** No change to `PINNED_TAG`, `pin_knowledge`, or the pinned tier of
  `knowledge_brief`.
- Retired entries stay in the result set. `search_knowledge` is the AUDIT surface by design
  (`central_store.py:853-862`) and `digested`'s `retired` marker must survive the shape
  change: a 160-char excerpt is even easier to mistake for standing advice than a headline.

**Accounting (load-bearing).** `central_store.record_knowledge_read` charges
`sum(len(content))` unless the caller passes `chars=` (`central_store.py:1050-1071`). The
search call site must pass the chars it ACTUALLY printed — the serialised index rows —
exactly as the `list` branch already does (`cli.py:4308-4312`). Without it `jarvis learn
stats` keeps reporting bodies nobody received, `read_chars_per_order`
(`ops.py:13590-13591`) stays at its old level, and this work order's done-when condition is
unmeasurable. Hit rows stay: `search` remains in `AIMED_VERBS`
(`central_store.py:46`) and is still aimed at the entries it names, so
`knowledge_read_hits` keeps feeding `knowledge_hit_counts` and the `never_read` figure
(`ops.py:13595-13596`).

**Every prompt string that now lies.** Each advertises `search` as returning full text and
must be re-worded to the index/show split (search finds and ranks; `show` is the only verb
that returns a body):

| Site | String |
|---|---|
| `src/jarvis/dispatch.py:280` | `render_knowledge_block` — `# full text of matches` |
| `src/jarvis/dispatch.py:307-308` | overflow roll-call: "Reach them with … or `jarvis learn search`" — still true, re-word for the new shape |
| `src/jarvis/dispatch.py:691-694` | `_planner_prompt` bullet |
| `src/jarvis/dispatch.py:807` | `_analyst_prompt` command list — "what the fleet already knows" |
| `src/jarvis/dispatch.py:885-886` | `_analyst_prompt` knowledge bullet |
| `src/jarvis/dispatch.py:1074-1075` | `_investigator_prompt` knowledge bullet |
| `src/jarvis/dispatch.py:1204-1205` | `_manager_prompt` knowledge bullet |
| `src/jarvis/worker_brief.py:246-251` | `core_contract` knowledge bullet |
| `src/jarvis/worker_brief.py:416-422` | `contract_section` — "to sweep for one" |
| `src/jarvis/worker_brief.py:841` | `knowledge_section` — `# full text of matches` |
| `src/jarvis/cli.py:1347` | argparse help: `"full text of entries matching a term"` |
| `src/jarvis/assets/OPERATION.md.tmpl:61-67` | the project's own operating doc |
| `src/jarvis/ui/templates/knowledge.html:6-10` | dashboard blurb (already says "index"; verify it does not imply search returns bodies) |

`tests/test_worker_brief.py:228` asserts the literal `"jarvis learn search"` survives in
the contract section — keep that substring present while re-wording around it.

### Half 2 — title-matched hints at dispatch, bounded

Surface, at dispatch, a small set of entries whose text matches the WORK ORDER'S OWN TITLE,
so an order whose relevant entry sits in the 586-entry overflow sees a pointer to it.

**Mechanism** — reuse the `could_have_read` shape (`ops.py:13557-13567`), moved into the
store so dispatch can use it:

1. `central_store.KnowledgeBrief` (`central_store.py:80-100`) gains
   `hints: list[dict[str, Any]] = field(default_factory=list)`. Additive; `__bool__` and
   `overflow_count` unchanged.
2. `central_store.knowledge_brief` gains a `title: str = ""` argument plus the two new
   bounds. With `title == ""` it behaves EXACTLY as today — this is what keeps
   `validation.py:959` (`_round`'s shared prefix) and `ops.py:13177` (`_index_cost`, which
   measures the prompt and must keep measuring the same thing) unchanged. Both call it
   positionally with the project only.
3. Selection: `self.search_knowledge(title, limit=<hint_limit>, project=project)`, dropping
   retired rows, then deduplicated against `pinned` and `digest` by id — an entry already in
   the index must not be printed twice.
4. `MISSED_MIN_WORDS` **applies** (recommended, and this is the decision): below 3 title
   words the query is "fix the", which matches a large share of the base and would
   manufacture a hint for every short title. The constant lives at `ops.py:13162`; move it
   to `central_store` (the store cannot import `ops` — layering runs stores upward only) and
   re-export or re-import it in `ops` so `knowledge_usage_report` keeps one definition.
5. The `ts <= wo.created_at` filter does **NOT** apply at dispatch (decision). It exists in
   the report to avoid blaming an order for not reading an entry it wrote itself
   (`ops.py:13559-13560`). At dispatch there is no "itself" yet: every existing entry is
   fair game, and filtering would silently drop the newest, most relevant lessons — exactly
   the ones a fresh order should see.

**Rendering** — `dispatch.render_knowledge_block` (`dispatch.py:262-310`) emits `hints` as
its own labelled block ABOVE the topic index (it is the most relevant thing in the section;
below the index it would be read as a footnote), headline + id ONLY, no bodies. The label
must say in the text that these matched the work order's TITLE and are therefore a HINT, not
a verdict — same labelling rule as kn-0281d10b point 4, and the same rule
`knowledge_usage_report` already states for `could_have_read` ("A title match is EVIDENCE,
NOT A VERDICT", `ops.py:13513-13515`). A hint block with no such line invites a worker to
treat a lexical coincidence as an instruction.

**Bounds** — two new `catalog.OsConfig` keys beside the existing three
(`src/jarvis/catalog.py:1337-1339`, parsed at `catalog.py:2100-2102`):

```python
knowledge_hint_limit: int = 3     # max title-matched hint lines
knowledge_hint_chars: int = 400   # hard char budget for those lines
```

Passed from `dispatch.dispatch_work_order` at `src/jarvis/dispatch.py:1321-1326`, alongside
`title=wo["title"]`.

The hint budget is **separate from and smaller than** the digest budget (decision). Three
reasons: taking hints out of `knowledge_digest_chars` would make the index shrink whenever a
title happened to match, so the same base would produce different index sizes for different
titles and `_index_cost` would stop being comparable across orders; the two have different
jobs and should be tunable independently; and 400 chars is ~2.5 headlines, which bounds the
worst case at a rounding error against a 4,000-char digest while `knowledge_hint_limit = 3`
keeps `search_knowledge` cheap. Defaults deliberately small — this is a pointer, not a
second index.

### Rejected alternatives

| Alternative | Why it loses |
|---|---|
| Truncate each body to N chars in `search` | Same shape, now lying. A reader cannot tell a truncated entry from a complete one and will act on half a rule. The excerpt is LABELLED, which is the whole difference. |
| `search --full` for when you really want bodies | Every worker prompt would carry it within a month and the 21M chars come straight back. `show <id>` already does this, aimed. |
| Change `central_store.search_knowledge` to stop returning `content` | Breaks the dashboard search, `knowledge_usage_report`'s sweep and the `could_have_read` join, all of which need bodies. The CLI is the right seam, and `list` already proves it. |
| Rank the whole prompt index by the title instead of adding a hint block | Replaces a measured, bounded selector (topic round-robin, which exists so one busy topic cannot hide every other) with an unmeasured one. The hint block is additive and reversible by one config key. |
| Raise `knowledge_digest_limit` so the 586 overflow entries fit | Reinstates exactly the cost this design removed: prompt size growing with base size. 615 entries of median 1,854 chars do not fit any prompt budget. |
| Let the worker figure out a search term from the overflow topic roll-call | That is the measured failure: 15 of 17 silent orders had a matching entry and none of them searched. |

## Test plan

New tests belong in `tests/test_knowledge_ondemand.py` (the index-not-payload suite) and
`tests/test_knowledge_observability.py` (the read log and `stats`), both of which already
drive `cmd_learn` through `build_parser` (`test_knowledge_ondemand.py:17-18`).

Half 1:

1. **An index row never contains a body.** Add an entry with a long, distinctive body;
   `jarvis learn search <term> --json`; assert no row has a `content` key and that the full
   body string does not appear anywhere in stdout. This is the acceptance test — it fails for
   any truncate-the-body implementation that keeps the field.
2. **Chars accounting matches what was printed.** Capture stdout, read the newest
   `knowledge_reads` row via `central.knowledge_reads(...)`, assert
   `row["chars"] == len(printed index rows)` and `row["chars"] < sum(len(body))` by a wide
   margin. Mirrors the assertion the `list` branch already needs.
3. **Hit rows survive.** After a search, `knowledge_hit_counts()` counts each returned entry
   once — `search` is still in `AIMED_VERBS`.
4. **Excerpt behaviour**, four cases: a match on a later body line yields an `excerpt`
   containing the query word; a long matching line is bounded at `HEADLINE_CHARS` and ends
   `…`; a match only in the first line yields NO `excerpt`; a hit with no matching body line
   (topic/tag hit) yields no `excerpt` and still returns the row.
5. **Retired marker survives the shape change** — `retired` present with its reason
   (extend the existing coverage in `tests/test_retraction.py`).
6. **`chars` field is the body length**, so a reader can price the fetch.
7. **Prompt strings no longer promise full text** — assert no rendered worker/planner/
   analyst/investigator/manager prompt contains `search` next to a full-text promise, and
   that `tests/test_worker_brief.py:228`'s `"jarvis learn search"` substring still passes.

Half 2:

8. **Default is byte-identical.** `knowledge_brief(project)` with no `title` returns a brief
   whose `hints == []`, and `build_worker_prompt` output is unchanged against the current
   expectation — protects `validation.py:959` and `ops.py:13177`.
9. **A title-matched overflow entry reaches the prompt.** Fill the base past
   `knowledge_digest_limit` so a target entry lands in `overflow`; dispatch a work order
   whose title matches it; assert its id appears in the rendered hint block.
10. **No duplication.** An entry already in `pinned` or `digest` is not also in `hints`.
11. **Bounds hold.** `knowledge_hint_limit`/`knowledge_hint_chars` cap the block; the digest
    is the same size with and without hints (proves the budgets are separate).
12. **Short titles get no hints** — fewer than `MISSED_MIN_WORDS` words, `hints == []`.
13. **No `ts` filter at dispatch** — an entry recorded AFTER the work order was created
    still appears as a hint.
14. **Hint block is labelled** — the rendered text says title match and says hint, not
    instruction.
15. **Catalog round-trip** for both new keys, defaults and overrides
    (`tests/test_catalog.py` pattern).

Run: `uv run pytest tests/ evals/`.

## Done-when, and how it is checked

Both halves are judged by `jarvis learn stats` (`ops.knowledge_usage_report`), over a window
after the change ships:

- `read_chars_per_order` (`ops.py:13590-13591`) falls well below ~89,747 — the Half 1
  target. Only credible because of the `chars=` call site above.
- `silent_order_count` and `could_have_read_count` (`ops.py:13597-13598`) do NOT rise — the
  Half 2 guard. Cheaper reads that make workers read LESS is the failure mode this condition
  exists to catch.
- `prompt_cost[].index_chars` (`ops.py:13184`) rises by at most `knowledge_hint_chars`, and
  `indexed`/`overflow` are unchanged — the hint block did not eat the digest.

`observed_from` (`ops.py:13593`) bounds every one of these to work after the read log
started; quote the before-window figures from io-edacb3ea rather than re-deriving them.
