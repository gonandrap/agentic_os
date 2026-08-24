# Ranked knowledge search

*2026-08-24*

Follow-on to `docs/superpowers/specs/2026-07-27-knowledge-on-demand-design.md`, which moved
the knowledge base out of the worker prompt and left a bounded index plus four retrieval
verbs behind it. Written for `wo-34ff39a6`.

---

## 1. Problem

Retrieval is now the whole product. A worker sees 160 characters of headline and an id; if
`jarvis learn search` does not return the entry, the entry does not exist.

`CentralStore.search_knowledge` matched substrings. `kn-b02bd307` already fixed the worst
of it — the query used to be one `LIKE '%whole phrase%'`, so `cents rounding format`, a
perfectly aimed query, returned nothing — by ORing the query's words and ranking rows by
how many matched. What that left:

* **No stemming.** `rounding` does not find `rounded`. It survives only by riding along
  with the other words in the query.
* **No ranking worth the name.** "How many of the query's words appear anywhere in the
  row" treats a hit on `the` as a hit on `idempotency`, and treats a row that mentions a
  word once as equal to one about it.

## 2. Why not pure FTS5

FTS5 with the `porter` tokenizer is the obvious answer and it is half of this one. It is
not all of it, because porter's stems are asymmetric in a way that would make the lookup
verb retrieve **less** than it did the day before. Measured on the SQLite bundled with
this Python (3.51.1), via `fts5vocab`:

| words indexed | stem |
|---|---|
| `deploy`, `deploys`, `deployed` | `deploi` |
| `deployment` | `deploy` |
| `kubernetes` | `kubernet` |
| `rounding`, `rounded` | `round` |

Porter's step 1c rewrites a trailing `y` to `i`, and its `-ment` rule fires first on the
longer word — so the two land in different buckets. Under pure FTS5 the query `deploy`
stops retrieving an entry that says `deployment`, and `kube` stops retrieving
`kubernetes`, both of which today's substring pass finds. FTS5 prefix queries do not
rescue it: FTS5 stems the prefix too, so `deploy*` searches for `deploi*` and misses the
token `deploy` entirely.

The `rounding`/`rounded` row is the win, and it is real. The rest is a regression a worker
cannot see — it gets an empty result and concludes the OS knows nothing.

**Decision (Neo, question 163 on `wo-34ff39a6`): never let a lookup verb retrieve less
than it did yesterday.** FTS5 is added as a ranked source *over* the substring pass, not
in place of it.

## 3. Two tiers

`search_knowledge` returns, in order:

1. **FTS5 hits, ordered by BM25.** Stemming and rarity-weighted ranking.
2. **Substring hits FTS5 did not return**, ordered as before: number of the query's words
   the row matched, then recency.

Deduplicated by entry id, then truncated to `limit`. Tier 2 is a floor, not a tie-break:
its ordering is untouched and every row it used to return is still returned, at worst
further down the list. Tier 1 only decides what comes first.

The empty term stays the "everything" read that `jarvis learn list` and the dashboard
rely on: it skips tier 1 (an empty FTS query matches nothing) and falls through to
`LIKE '%%'`.

## 4. The index and how it stays in sync

`knowledge_fts` is an FTS5 **external-content** table over `knowledge` — `content=` and
`content_rowid=` — so entry text is stored once. Three `AFTER INSERT/UPDATE/DELETE`
triggers on `knowledge` keep it current, which covers every writer including the ones
that do not go through `add_knowledge` (`retract_knowledge`, `record_memory_file`,
`set_knowledge_tags`).

Existing installs are backfilled once with FTS5's `'rebuild'` command, guarded by an
`os_state` key (`knowledge_fts_built`) rather than by a row count: a count comparison on
an external-content table re-scans `knowledge`, and it would silently paper over a broken
trigger by rebuilding on every open.

`os.db` upgrades in place via `ADDED_COLUMNS`/`_migrate`, which only knows how to add
columns. The FTS objects are created separately, in `_ensure_fts`, for the same reason
`ADDED_COLUMNS` exists: `executescript(SCHEMA)` is a no-op on a database that already has
its tables.

## 5. Translating a query

Agents type prose, and prose contains characters FTS5 reads as syntax (`-` is NOT, `:` is
a column filter, `"` opens a phrase). An unescaped query is a `sqlite3.OperationalError`,
and this is a read verb: it must never raise on what a user typed.

Each whitespace-separated word becomes a **quoted phrase**, and the phrases are ORed —
`PRs #1-#2` becomes `"PRs" OR "#1-#2"`, where the second quoted string is tokenized as
the phrase `1 2`. Words containing no alphanumeric character are dropped: they tokenize
to nothing, and an empty phrase is itself a syntax error. If nothing survives, tier 1 is
skipped.

## 6. Column weights

`bm25(knowledge_fts, 1.0, 4.0, 2.0)` — content, topic, tags. A query word that matches an
entry's **topic** is a much stronger signal than one that appears somewhere in 2,000
characters of body, and topics are the axis the index is organised by (`jarvis learn
topics`, and the prompt's overflow roll-call). Tags sit between the two.

## 7. What this does not fix

**Synonyms.** `jarvis learn search deploy` still will not surface an entry that only ever
says `shipit`. That is out of reach for any lexical index — FTS5 matches the words that
are there — and needs either a curated synonym map or embeddings. Filed as backlog
(`jarvis backlog list jarvis_os`), not scope here.

`evals/llm/test_knowledge_retrieval_judgment.py` remains the early-warning system: it
grades retrieval by running the subject's chosen search term against a real store, so the
day a battery starts failing on a synonym is the day this becomes the next work order.

## 8. When SQLite has no FTS5

FTS5 is compiled into every SQLite this project has met, including the one bundled with
CPython. It is still a compile-time option, so `_ensure_fts` catches the failure, records
it on the store, and `search_knowledge` runs tier 2 alone. A search that is merely as good
as yesterday's is not an outage; a store that will not open is.
