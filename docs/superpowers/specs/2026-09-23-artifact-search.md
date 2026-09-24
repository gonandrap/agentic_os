# Artifact search — finding a record again

**wo-edf5c425.** Re-reading a completed work order meant opening its project, expanding
the settled count and scanning it. Every other kind of record was worse: an alarm, a
gate, a Neo ruling or a backlog item had no "find it" verb at all.

## 1. One verb, two scopes

`jarvis search "<words>" [--project <p>] [--kind <k>] [--limit n] [--json]`, and the
dashboard's `/search`, reached from a box in the chrome of every page and from a
project-scoped box on the project page. The scoped form carries `project`, and the
results page offers "search every project" in one click — narrowing must not be a
one-way door.

## 2. What it searches

The six kinds the order named — work orders, feature orders, Neo questions, alarms,
gates, backlog items — plus knowledge entries, through `search_knowledge`, which already
ranks (Neo question 528). The inbox is out: it is a notification stream, acked and gone.

**Nothing is filtered by status, and hidden orders are included.** Listings answer "what
wants me now" and are right to hide settled and hidden rows; search answers "where is
that thing", and the settled row is the one being looked for. This is the single rule
that makes the feature worth having, and the first one a later change will be tempted
to undo.

## 3. Matching

Substring, word-OR, weighted by field (`db.score_sql`): per query word, the heaviest
column containing it, summed over words. An id typed in full adds `EXACT_ID_BONUS`, so a
paste of `wo-1234abcd` is a jump rather than a search. Words under
`db.MIN_SEARCH_WORD` characters are dropped unless that is all the query has — under
substring matching "in" is inside "login", so short words match everything.

**Not FTS5.** The corpus is a few hundred rows spread over N project databases plus
os.db and neo.db; an external-content index there means triggers and a backfill guard in
three schemas (the machinery kn-c6e8fbf0 documents for one). Ruled by Neo, question 528.
If the fleet's history grows by an order of magnitude, the `search_*` store methods are
the seam to re-implement — `search.py` reads their rows and nothing else.

## 4. Ranking and output

Hits sort by score, then by the kind's position in `search.KINDS`, then by recency. Each
carries `url` (the dashboard page for that record) and `ref` (the `jarvis …` command that
shows it), so a hit is actionable from either surface. The snippet is the window around
the first match, not the opening of the body — and is dropped when it would restate the
title.
