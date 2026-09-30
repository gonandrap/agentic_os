# A boundary is classified once, and every turn gets its own

Work order wo-bf9fbaaa, GitHub issue #867. Neo question 1048 settled the mechanism; §3
records what it rejected. All numbers in §1 were measured in this worktree against live
production transcripts.

## The problem

### 1. The compaction detector is not blind. The issue's diagnosis is wrong.

#867 reports `jarvis inspect` showing `boundaries_compact = 0` on orders that compacted and
guesses `usage.compactions_in` misses the rows. Measured, all three of its premises fail:

- `usage.compactions_in` (`src/jarvis/usage.py:763-792`) parses real `system` /
  `compact_boundary` rows at both triggers (`manual` and `auto`).
- **84 of 84** production worker sessions containing a compaction report a non-zero
  `usage.Usage.boundaries_compact` through `usage.read_session`. So `jarvis cost`'s
  `rewrite.compact_boundaries` (`src/jarvis/bill.py:1354`) is right everywhere.
- `jarvis inspect wo-35fc3de7 --json` already labels two of its eleven large writes
  `compaction` in `units[].writes` — `inspection.classify_writes`
  (`src/jarvis/inspection.py:1151-1182`).

### 2. The real zero is per TURN, and it is a whole family of dead keys

The zero the reporter saw is `units[].turns[].usage.boundaries_compact`.

`inspection.Turn.usage` (`src/jarvis/inspection.py:703-711`) builds its `Usage` by summing
`usage.priced()` over the turn's `Call`s. `priced()` (`src/jarvis/usage.py:882-908`)
classifies nothing — by design, documented: it prices counts a caller already holds. So on
**every turn of every order** these keys of `Turn.as_dict()["usage"]`
(`usage.Usage.as_dict`, `src/jarvis/usage.py:350-374`) are structurally zero:

| key | value on every turn, always |
|---|---|
| `resume_boundaries` | 0 |
| `boundaries_ttl` | 0 |
| `boundaries_compact` | 0 |
| `rewrite_compact_write` | 0 |
| `rewrite_excess` | 0 |
| `rewrite_ttl_excess` | 0 |
| `rewrite_ttl_share` | `None` |
| `context_peak` | 0 |

Verified on a real turn of wo-35fc3de7 whose own `cache_write` is **138,376**: a turn that
paid the re-write tax reports `rewrite_excess: 0`.

`context_peak` is the sharpest form of it. `Turn.as_dict()` emits the key **twice**:
`"context_peak"` at `:741` from `Turn.context_peak` (`:689-697`, real), and
`"usage"."context_peak"` at `:750` (always 0). Two keys, one name, one of them a lie.

`supervisor._session_lines` (`src/jarvis/supervisor.py:385`) and
`inspection._spend_so_far` (`:1553`) read `turn.usage` too, so the dead zeros reach the
evidence packet a model reads when deciding whether to spend the user's attention.

### 3. No session-level rewrite total anywhere in the payload

`Anatomy.as_dict()` (`src/jarvis/inspection.py:949-982`) carries `rewrite_excess` and
`cache_ttl` and nothing else about boundaries. There is no per-session equivalent of
`bill._worker_extras`' `rewrite` block (`src/jarvis/bill.py:1330-1356`), which is the
figure the cache-cost work (#846) needs and the figure that would let a reader lay
`jarvis cost` and `jarvis inspect` side by side.

### 4. Root cause

**Boundary classification exists once, inside a private per-file loop, and is unreachable
from anywhere else.** The block at `src/jarvis/usage.py:849-866` is the OS's only
implementation of "a cache read that went backwards is a boundary, and here is what caused
it". It is buried in `_usage_of`, keyed to `_assistant_messages(path)`, and returns nothing
but summed totals. Every other surface that needs the same judgement either restates it
(`inspection.classify_writes`, `scripts/compaction_cohort.py`) or silently reports zero
(`Turn.usage`). §2 is the third outcome of that one cause.

## The fix

Four changes. Nothing in §1's measured behaviour of `jarvis cost` changes by a token.

### 1. Lift boundary classification into one shared public function in `usage`

**Where:** `src/jarvis/usage.py`, a new `Boundary` dataclass and
`classify_boundaries()` beside `calls_of`, plus the deletion of the inline block at
`:849-866`.

```python
#: Why one cache boundary happened. `UNDECIDED` is the TTL-vs-prefix split left OPEN
#: because the caller could not reach `os.cold_prefix_floor` — it is never produced when a
#: floor was passed, and it is not a fourth cause.
BOUNDARY_COMPACTED = "compacted"
BOUNDARY_TTL = "ttl"
BOUNDARY_PREFIX = "prefix"
BOUNDARY_UNDECIDED = "undecided"


@dataclass
class Boundary:
    """One place a conversation was re-written, and what re-wrote it."""

    ts: float          # the call that paid for it
    cause: str
    cache_write: int   # that call's own write
    cache_read: int
    gap: float         # seconds since the previous call, 0.0 for the first


def classify_boundaries(calls: Sequence[Call], *, compactions: Sequence[float] = (),
                        cold_prefix_floor: int | None = None) -> list[Boundary]:
```

The body is the existing block, moved, with its three rules kept verbatim and its comments
carried across:

1. A boundary is a cache read that went **backwards** versus the previous call
   (`read < previous_read`). No size threshold — that is what keeps it free of magic
   numbers and lands it on exactly `turns - 1`.
2. **Compaction is tested FIRST.** `any(previous_ts < c <= ts for c in compactions)`. A
   compacted boundary reads the static head seconds later and is otherwise
   indistinguishable from a prefix miss.
3. Then `expired = gap >= WRITE_TTL_SECONDS and read <= cold_prefix_floor` gives
   `BOUNDARY_TTL`, else `BOUNDARY_PREFIX`.

One addition, and it is the only new behaviour: **`cold_prefix_floor=None` yields
`BOUNDARY_UNDECIDED`** for anything rule 3 would have decided. Rules 1 and 2 need no floor,
so a boundary is still COUNTED and a compacted one still LABELLED. The split is left open
rather than guessed, because `src/jarvis/usage.py:127-131` forbids a module-level default
for that threshold and says why: a report that classified against an invented number would
print a finding the configuration never produced.

`_usage_of` (`:816`) becomes a caller: it builds its `Call` list, calls
`classify_boundaries(calls, compactions=compaction_stamps(path),
cold_prefix_floor=cold_prefix_floor)`, and folds the result into the same six fields.

**This must be an exact refactor, and two things make it one.** `calls_of`
(`:690-713`) is documented as exactly the messages `_usage_of` totals — same
`_assistant_messages(path)` walk, same seven fields. And `_usage_of` keeps calling the
classifier **once per FILE**, not once per session. That is the trap: `usage.session_calls`
(`:716-731`) concatenates every segment of a session, so a session-wide run sees an extra
boundary between the last call of segment 1 and the first of segment 2 where the per-file
run sees none. `_usage_of` is per path (`read_session`, `:951-952`) and stays per path.

`rewrite_excess` and `context_peak` stay exactly where they are, in `_usage_of`
(`:844`, `:878`): they are accumulated over the whole message walk, not per boundary.

### 2. `inspection.read_session` classifies once and attributes each boundary to a turn

**Where:** `src/jarvis/inspection.py` — `read_session` at `:1267-1272`, one new field on
`Turn`, one new helper beside `_attach_calls` (`:1331-1347`), and `Turn.usage` at
`:703-711`.

`read_session` already holds both ingredients at `:1267-1271`: `usage.session_calls(...)`
and the gathered `compactions`. Add one line beside the existing `classify_writes` call:

```python
boundaries = usage_mod.classify_boundaries(
    calls, compactions=sorted(compactions), cold_prefix_floor=cold_prefix_floor)
anatomy.boundaries = boundaries
_attach_boundaries(turns, boundaries)
```

`Turn` gains `boundaries: list[usage_mod.Boundary] = field(default_factory=list)`.

`_attach_boundaries` uses **the same last-turn-started-by-then rule as `_attach_calls`**
(`:1338-1347`, itself matching `bill._turn_locator`), so the boundaries cut the session at
the same points the calls and the bill do. Written as one shared locator rather than a
second copy of the loop: two spellings of "which turn was this in" is how two numbers on
one payload come to disagree.

`Turn.usage` then fills the fields from its OWN boundaries and its own calls:

- `resume_boundaries` = `len(self.boundaries)`
- `boundaries_compact` / `boundaries_ttl` = count by cause
- `rewrite_compact_write` / `rewrite_ttl_write` / `rewrite_prefix_write` = summed
  `cache_write` of the boundaries with that cause
- `context_peak` = `self.context_peak` — the property that already exists at `:689-697`.
  The two keys of §1.2 now carry the same number, which is the point.
- `rewrite_excess` = `max(0, sum(c.cache_write for c in self.calls) - self.context_peak)`,
  `usage`'s definition applied at this grain, the same arithmetic
  `Anatomy.rewrite_excess()` (`:937-947`) already does for the session.

An `UNDECIDED` boundary lands in `resume_boundaries` and in **none** of the write buckets.
That is already the honest reading: with all three buckets empty `Usage.rewrite_ttl_share`
returns `None` (`src/jarvis/usage.py:322-336`), which the renderers are already required
not to print as 0%.

`Usage` gains one field so the gap is nameable rather than inferred from a `None`:

```python
#: Boundaries whose TTL-vs-prefix split was left OPEN — the caller had no
#: `os.cold_prefix_floor`. Counted in `resume_boundaries` and in no write bucket.
boundaries_undecided: int = 0
```

Additive in `__add__` (`:257-289`, beside `resume_boundaries`) and a key in `as_dict()`.
**Deliberately NOT added to `bill._worker_extras`' `rewrite` block**: `bill` always resolves
the floor (`bill._cold_prefix_floor`, `:1145-1153`, no fallback by design), so the key would
be permanently zero there — which is the exact defect §1.2 is about.

`Usage.__add__` already takes the **max** of `context_peak` (`:275`), so summing turns
yields the session's peak rather than a nonsense total. No change needed; assert it.

### 3. A session-level rewrite total on `Anatomy`, keyed to the bill

**Where:** `Anatomy.rewrite()` beside `rewrite_excess()` (`src/jarvis/inspection.py:937`),
emitted as `"rewrite"` in `as_dict()` (`:949-982`).

Keys are `bill._worker_extras`' `rewrite` keys (`src/jarvis/bill.py:1338-1356`) wherever
the field means the same thing, so the two payloads can be laid side by side:

```python
{"boundaries": …, "ttl_boundaries": …, "compact_boundaries": …,
 "undecided_boundaries": …, "ttl_write": …, "prefix_write": …, "compact_write": …,
 "cache_write": …, "tokens": self.rewrite_excess()}
```

- Summed from the boundaries `read_session` classified in §2 and the calls already in hand.
  Nothing re-reads a file and nothing re-classifies.
- `tokens` is `self.rewrite_excess()` called, not recomputed, so the top-level
  `rewrite_excess` key and this one cannot drift.
- **No `list_usd` and no `ttl_share`.** `jarvis cost` is the money surface; this report
  prices nothing at session level and adding a second dollar figure invites two answers to
  one question. `ttl_share` is a ratio of the above and belongs to whoever divides them.
- `undecided_boundaries` is the extra key, and it is why the block is safe to read with an
  unknown floor: `ttl_write` and `prefix_write` at 0 with `undecided_boundaries` at 4 is
  "not measured", not "no TTL expiry".

**The renderer prints it.** `cli._print_anatomy` (`src/jarvis/cli.py:2115-2121`) already
prints one re-write line; it gains one line under it, inside the same block, before the
blank line at `:2122`:

```
  boundaries 14 — 9 prefix, 3 expired, 2 compacted · re-written 1.2M of 3.4M written
```

`undecided` replaces the prefix/expired pair when `undecided_boundaries` is non-zero:
`boundaries 14 — 2 compacted, 12 unclassified (no os.cold_prefix_floor)`. A `--json`-only
field nobody can see is half a fix.

### 4. Plumbing: `cold_prefix_floor` reaches `read_session`, best-effort

Recorded as an assumption on wo-bf9fbaaa.

`inspection.read_session` gains `cold_prefix_floor: int | None = None`, keyword-only,
beside `spans` and `turn_starts` and for the same stated reason (`:1236-1247`): this module
walks files Claude Code wrote and has never opened the OS's database or a catalog.

Three call sites pass it, each resolving it best-effort:

| call site | passes |
|---|---|
| `ops.inspect_report` → `unit()` (`src/jarvis/ops.py:11434`) | resolved once per report, beside `index` |
| `ops.context_report` (`src/jarvis/ops.py:11569`) | the same value |
| `supervisor._session_lines` (`src/jarvis/supervisor.py:372`) | the same value |

Resolution is a new `ops.cold_prefix_floor(project=None) -> int | None`, shaped exactly
like `ops.inspect_config` (`src/jarvis/ops.py:11264-11279`) — `try: resolve_catalog().os
.cold_prefix_floor` / `except (OpsError, CatalogError, OSError, ValueError): return None`.
Its docstring must say what `inspect_config`'s says: **a report over files on disk must not
fail because a catalog has moved.** Unlike `inspect_config` it falls back to `None` and not
to a number, because `bill._cold_prefix_floor` (`:1145-1153`) has already settled that
there is no defensible default. `supervisor` imports it inside the function, as it already
does for `holds` and `inspection` (`supervisor.py:364`).

Two call sites pass **nothing** and keep today's behaviour:

- `inspection.live_alarms` (`:1702-1723`). No alarm reads a boundary field off `Anatomy` —
  `REWRITE_TTL_ALARM` and `REWRITE_PREFIX_ALARM` are raised by the daemon off `bill`/`usage`,
  which has the floor. Adding an argument nothing reads is a parameter to keep in step for
  no reading.
- `ops._diagnose_holds` (`:1905`). It reads one number off the anatomy
  (`anatomy.unexplained`), which its own comment says (`:1901-1904`).

## Tests

1. **The issue's own case.** A fixture with a compact turn between two ordinary turns:
   `Turn.usage.boundaries_compact == 1` on the post-compaction turn, `0` on the others, and
   the same turn's write labelled `COMPACTION` in `Anatomy.writes`. This is #867's ask and
   it is the only assertion in the issue that was ever true of the fix rather than of the
   diagnosis.
2. **`_usage_of`'s numbers are unchanged, to the token.**
   `tests/test_compaction.py:531-541` and `:585-614` already pin them
   (`rewrite_compact_write == 15_380`, `boundaries_compact == 1`,
   `rewrite_ttl_write == 120_000` on `s-mixed`). They must pass **unedited**. A test that
   moves here is a refactor that was not one.
3. **A per-turn `Usage` on a real multi-turn fixture reports a non-zero
   `resume_boundaries`** on the turn the session's boundary fell in, and the per-turn
   `usage["context_peak"]` equals the sibling top-level `context_peak` key (§1.2's two
   spellings of one number).
4. **The session-level total agrees with `usage.read_session`.** On one fixture,
   `Anatomy.as_dict()["rewrite"]["compact_write"] / ["ttl_write"] / ["prefix_write"] /
   ["cache_write"]` equal `usage.read_session(sid, FLOOR).total`'s same four fields — the
   rule `tests/test_compaction.py:585` already enforces for
   `scripts/compaction_cohort.py`. Sum the per-turn `Usage`s and assert they agree with the
   session block too, so the two grains cannot drift.
5. **Floor unknown does not crash and does not guess.** `read_session(...)` with no
   `cold_prefix_floor`: `rewrite["boundaries"]` is the same count as with the floor,
   `compact_boundaries` is the same, `ttl_write == prefix_write == 0`,
   `undecided_boundaries` equals the rest, and `Usage.rewrite_ttl_share is None`.
6. **`ops.cold_prefix_floor` returns `None` for an unreachable catalog** and does not
   raise — the guarantee `ops.inspect_config`'s docstring makes.

## Non-goals

- **`classify_writes` is not merged into `classify_boundaries` and does not change.** They
  answer different questions and §3 says why the merge was rejected.
- **`bill`'s payload does not change**, `_worker_extras` included. §1 measured it correct.
- **No new alarm and no new catalog setting.** `os.cold_prefix_floor` already exists
  (`catalog.py:1254`); nothing here reads a threshold that is not already configured.
- **Subagent anatomies get no boundary attribution.** `SubagentAnatomy`
  (`inspection.py:757`) holds its own turns and its own writes; giving it boundaries is the
  same refactor one level down and wants its own order once this one is in.
- **#846's cache-cost decision is not taken here.** This produces the figure it needs; it
  does not spend it.

## Rejected alternatives

1. **Fill the per-turn fields from `classify_writes`' causes.** It is right there in
   `read_session` and already labels compactions. Two defects: its writes are filtered by
   `cfg.report_write_floor` (`:1167`, `if call.cache_write >= floor`), so a count drawn
   from it would silently mean "boundaries whose write was large enough to report" while
   `Usage.resume_boundaries` means "cache reads that went backwards" — the same key with
   two definitions on two surfaces, which is the defect this order exists to stop. And its
   TTL-vs-prefix test is **gap-only** (`gap > ttl`, `:1174`); it never consults
   `cold_prefix_floor`, so its `ttl-expiry` and `usage`'s `boundaries_ttl` are not the same
   predicate. `classify_writes` is a correct answer to "which large writes should this
   report list, and why"; it is not a boundary census.
2. **Drop the dead-zero keys from the payload.** Cheapest, and it removes the lie. It also
   removes the per-turn figure #867 was asking for and the figure #846 needs, and it leaves
   the root cause of §4 untouched — the next surface that needs a boundary count restates
   the rules a fourth time.
3. **Put `cold_prefix_floor` on `InspectConfig`** so it rides along with the floors
   `read_session` already takes. It is an `os.*` setting (`catalog.py:1254`) and
   `InspectConfig` is per-project overridable through `jarvis config set`: a project could
   then change what the word "boundary" means in its own report, and `jarvis cost` — which
   reads the OS value — would disagree with `jarvis inspect` on the same session. A separate
   parameter cannot be overridden by a project.
4. **Make `cold_prefix_floor` required, as `usage.read_session` makes it
   (`src/jarvis/usage.py:936-943`).** Correct for the bill, wrong here: `ops.inspect_config`'s
   docstring commits this report to not failing because a catalog moved, and `jarvis inspect`
   over a stale worktree is exactly when someone needs it. `UNDECIDED` is what that
   commitment costs, stated on the payload.
5. **Classify per turn instead of once per session.** Each turn's first call would read less
   than the previous turn's last — so every turn would open with a boundary it did not
   necessarily have, and the per-turn counts would not sum to the session's. Classify once,
   attribute after: the ruling in Neo question 1048.
