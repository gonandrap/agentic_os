# Subagent cache anatomy, and the navigation volume split by side (wo-4359681a)

**Status:** specified; amended 2026-10-02 after review round 1 on PR 927
**Subject:** `jarvis inspect`'s subagent lines, `jarvis cost`'s re-write tax, and a new
top-level `jarvis navigation`
**Builds on:** `docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md` §4b (a subagent
is a PARTITION of a turn, never an addition),
`docs/superpowers/specs/2026-09-29-inspect-boundary-classification.md`
(`usage.classify_boundaries`, the OS's one boundary judgement),
`docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md` §5.3 (the navigation
measurement method and its BEFORE figures)
**Rulings:** Neo q1215 (what "the parent's boundaries and re-write tax" means), q1216 (a
top-level command, and the classifier's patterns live in the catalog)
**Amendment:** §1.1, §1.3, §1.4, §3.1-3.3 and rejected alternative 9 record problem 4 — the
zero-default defect this spec's first draft prescribed and review caught. Knowledge entry
`kn-4d32fe12`.

## The problem

Two reports go silent exactly where the subagent half of the fleet's spend is — and this
spec's own first draft replaced one silence with a falsehood.

**1. `jarvis inspect` prints `no large writes` under a subagent that wrote 334,427 tokens.**
`cli._print_subagent` renders the bare string `no large writes` whenever `writes_by_cause`
is empty. On wo-fb7c0fc2 the `jarvis-implementer` subagent `a8e11a7e` wrote **334,427
cache-creation tokens across 115 API calls** with a **max single write of 19,381** — every
write under the floor, so the list is empty and the line reads as "nothing here" when it
means "nothing AT THAT FLOOR". Across all **420** subagent transcripts on this box, **213
have no single write at or over 20,000** and 207 do; **91,636,120** cache-creation tokens
were written in total. Half the corpus renders as silence.

**2. `jarvis cost` adds the subagent re-write tax without saying it added it, and the
number it adds is zero.** `usage.read_session` (`src/jarvis/usage.py:1022-1054`) folds every
`<session>/subagents/*.jsonl` into `SessionUsage.subagents`, `SessionUsage.total` is
`main + subagents`, and `Usage.__add__` (`src/jarvis/usage.py:279-280`) sums
`rewrite_excess` and `resume_boundaries`. So `bill._worker_extras`'
`rewrite` block (`src/jarvis/bill.py:1338-1356`) is already a rollup of both sides — an
UNLABELLED one, whose subagent contribution is 0 by arithmetic and reads as "subagent cache
spend is negligible". It is not negligible; it is unattributable from that number.

**3. There is no navigation measurement anywhere in `src/`.** The 41.3% finding — the
largest single cost category in the fleet — exists only as the hand-run script described in
`docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md` §5.3, and its BEFORE
figures (0 symbol calls, 41.3% of 14,558 MB of read volume) cannot be re-run by anyone who
has not read that section. With `worker.bash_first` now shipping `off`, the AFTER figure is
the thing that says whether that fix worked, and the fleet has no command that produces it.

**4. The `int = 0` defaults this spec prescribed printed a FALSEHOOD on every settled
order.** §1.1's first draft gave `total_written`, `max_write`, `write_floor` and
`api_call_count` the type `int` with a `0` default. `autopsy._read_subagent`
(`src/jarvis/autopsy.py:371-384`) rehydrates a sealed autopsy through `row.get(<name>)`, and
a seal written before those keys existed has no such key — so `row.get` returned `None`, the
dataclass default stood in, and `jarvis inspect wo-fb7c0fc2` (seal written 2026-10-01)
printed **`wrote nothing to the cache`** about the very subagent whose 334,427 tokens across
115 calls are problem 1's evidence. The tell was in the same payload: `write_floor: 0` and
`api_call_count: 0` sitting beside `api_calls: 115`. That line is strictly worse than the
flat `no large writes` this order exists to delete — a measurement claim about an
unmeasured subagent, where the old string was merely uninformative.

**Root cause of (1), named: the FLOOR, not an unread transcript.** The work order's
complaint reads as a depth problem and it is not. `inspection._read_subagent`
(`src/jarvis/inspection.py:1511-1542`) ALREADY calls
`classify_writes(calls, cfg.report_write_floor, …)` on every subagent transcript;
`classify_writes` keeps only writes `>= floor` and `report_write_floor` defaults to 20,000
(`catalog.DEFAULT_INSPECT_REPORT_WRITE_FLOOR`). The data is read and then thresholded away.
So the fix is NOT "read deeper" — reading deeper would change nothing about wo-fb7c0fc2 —
it is a threshold-free figure beside the floored list, and a renderer that cannot print an
empty list as an absence.

**Root cause of (2), named: both zeros are structurally correct.** Measured on session
`22b44b27-03f8-4417-bc6a-a8f66910d028` at `cold_prefix_floor=50000`:

| side | `rewrite_excess` | `resume_boundaries` | `cache_write` |
|---|---|---|---|
| main | 79,404 | 1 | 332,441 |
| subagents | 0 | 0 | 259,744 |
| total | 79,404 | 1 | 592,185 |

`rewrite_excess = max(0, sum(cache_write) - max(context))` (`src/jarvis/usage.py:55`,
`:940`) and a subagent's written volume never exceeds its own context peak, so the excess
is 0. A single continuous subagent conversation has no cache read going BACKWARDS, so
`classify_boundaries` finds none. A subagent pays cold-start plus delta writes and NO
re-write tax. **Do not invent a tax where the arithmetic gives none.** The defect is the
report, which must label that zero a STRUCTURAL zero — never evidence that subagent cache
spend is small, when those subagents wrote 259,744 tokens in that one session.

**Root cause of (4), named: §1.3 covered the wrong half of the seal problem.** The first
draft reasoned about a field a NEW seal DROPS ("a field the seal drops is a field that
silently becomes zero on every settled order") and prescribed carrying all five keys
through `autopsy._subagent`. It never covered seals that PREDATE the field — and on this box
every settled order is one, so the case it skipped is the only case that exists today. The
general rule, now `kn-4d32fe12`: **a field added to a dataclass that is rehydrated from a
persisted seal must be `int | None` with a `None` default, never `int` with a zero default,
because zero is a measurement and absence is not.** It is the rule
`SideVolume.code_nav_share()` already states for a share (§2.1) and
`usage.rewrite_ttl_share` states for a ratio; the draft applied it to the derived figures
and not to the scalars they are derived from.

## The fix

Two parts, one shipped change each, and the measurement in part 2 is new code rather than a
widening of part 1's.

### 1. Per-subagent cache anatomy: threshold-free figures beside the floored list

#### 1.1 `inspection.SubagentAnatomy` gains five fields

`src/jarvis/inspection.py:803-902`. Every one is ADDITIVE; no existing field changes
meaning and `writes` still holds exactly what `classify_writes` returned.

Four of the five are `int | None = None` for problem 4's reason, verbatim:

```python
    #: THRESHOLD-FREE, and the reason this class needed more than `writes`: a subagent
    #: that wrote 334,427 tokens in 115 writes none of which reached the floor had an
    #: empty `writes` list and rendered as "no large writes" (wo-fb7c0fc2, a8e11a7e).
    #:
    #: `None` AND NEVER 0 WHEN IT WAS NOT MEASURED — `SideVolume.code_nav_share()`'s
    #: rule: a zero share is a finding and an unmeasured one is not. Every order sealed
    #: before these fields existed carries no key at all, and defaulting those to 0 made
    #: the renderer say `wrote nothing to the cache` about that same 334,427.
    total_written: int | None = None
    #: The largest single write, floor-free — the number that EXPLAINS an empty `writes`
    #: list, and the only one that distinguishes "under the floor" from "wrote nothing".
    max_write: int | None = None
    #: The floor `writes` was built at, carried so a renderer never has to consult the
    #: config to word an absence (`Anatomy.write_floor`'s rule).
    write_floor: int | None = None
    #: `usage.classify_boundaries` over THIS subagent's calls — the threshold-free
    #: census q1215 requires. One run of calls per transcript, which is the grain
    #: `classify_boundaries` documents.
    boundaries: list[usage_mod.Boundary] = field(default_factory=list)
    #: Calls, carried as a count so `total_written` can be read as a rate. A FIELD and
    #: not `api_calls`: a transcript with no prompt row has no turns at all
    #: (`read_transcript` opens one only at a prompt), so the property counts 0 over the
    #: calls that were made, and the two must not disagree. `None` is absent, as above.
    api_call_count: int | None = None
```

`boundaries` keeps the `list` with `default_factory` — an empty list is already
indistinguishable from an absent one, which is exactly why the absence is carried on
`total_written` and read from there (below).

`api_call_count` stays a field and is NOT collapsed into the `api_calls` property: a
transcript whose rows contain no prompt opens no `Turn` at all, so the property sums 0 over
calls that were made.

New method, mirroring `Anatomy.rewrite()` and NOT a second copy of it. It returns `None`,
not a dict of zeros, when the figures were never measured:

```python
def rewrite(self) -> dict[str, int | None] | None:
    """This subagent's own boundary census, same keys as `Anatomy.rewrite()`.

    `None` WHEN THE FIGURES WERE NEVER MEASURED, because the census is derived from
    them: `cache_write` is `total_written` and `tokens` is its excess over the peak,
    so a block built over an absence would be a dict of zeros claiming "no re-write
    tax" about a subagent nobody counted. An empty `boundaries` list cannot say
    which, so the absence is carried on the scalar.
    """
    if self.total_written is None:
        return None
    return _rewrite_block(self.boundaries, written=self.total_written,
                          excess=max(0, self.total_written - self.context_peak))
```

Lift the body of `Anatomy.rewrite()` into a module-level
`_rewrite_block(boundaries, *, written, excess) -> dict[str, int | None]` and have BOTH call
it. Two spellings of that dict is how one key comes to mean two things on two surfaces —
the rule `usage.fold_boundaries` was factored out under.

`as_dict()` adds `total_written`, `max_write`, `write_floor`, `api_call_count` and
`rewrite` (the census dict, not the boundary list: the per-turn `boundaries` key already
exists at the parent grain and a renderer needs the counts, not the rows). All five keys are
EMITTED even when the value is `None` — an omitted key is precisely what an old seal looks
like, so absence is said rather than left out.

#### 1.2 Where they are filled: `inspection._read_subagent` only

`src/jarvis/inspection.py:1511-1542`. It already holds `calls`; the five fields are four
sums and one call over a list it has:

```python
    boundaries = usage_mod.classify_boundaries(
        calls, compactions=compactions, cold_prefix_floor=cold_prefix_floor)
```

which means `_read_subagent` takes a `cold_prefix_floor: int | None` parameter and
`_attach_subagents`'s caller (`read_session`) threads the value it already holds. `None`
yields `BOUNDARY_UNDECIDED` and the renderer must print "unclassified (no
os.cold_prefix_floor)" — never 0 prefix misses, which would read as a finding.

**It goes here and nowhere else.** `_read_subagent` is the one place a subagent transcript
is opened; `inspection` stays a LEAF (it imports `usage`, `catalog` and `holds`, and never
opens the OS database) and this adds no import. A live read always MEASURES: every one of
the four scalars is an `int` here, never `None`. `None` is reachable only through the seal
path (§1.3), which is what makes it mean "this seal predates the field". Nothing is folded
upward: `Turn.usage` and `Anatomy.rewrite()` are untouched, so spec 2026-08-30 §4b's
partition survives byte for byte — a parent turn that merely waited on a join still did not
pay that write.

#### 1.3 The seal round-trip, and the seals that PREDATE the fields

A sealed order is the only reading available once its transcript expires
(`autopsy.anatomy_for`), so the seal has two failure modes and the draft of this section
covered one.

**Forward: `autopsy._subagent` (`src/jarvis/autopsy.py:218-226`) carries all five fields**,
`boundaries` using the existing `_boundary`/`_read_boundary` pair. A field a new seal drops
is a field that silently becomes absent on every order sealed from here on.

**Backward: `autopsy._read_subagent` (`src/jarvis/autopsy.py:371-384`) reads each of the
four scalars as `row.get(<name>)` with NO `0` default** — verbatim:

```python
        # NO `0` DEFAULT (spec 2026-10-02 §1.3, as it lands on seals written BEFORE the
        # keys existed): absent must stay distinguishable from a measured zero, or the
        # renderer says `wrote nothing to the cache` about 334,427 tokens.
        total_written=row.get("total_written"),
        max_write=row.get("max_write"),
        write_floor=row.get("write_floor"),
        api_call_count=row.get("api_call_count"),
```

The `row.get(…, 0)` form is refused on all four, and so is the `int = 0` dataclass default
that makes `row.get(name)`'s `None` collapse to zero on the way in. `boundaries` keeps
`row.get("boundaries") or []`: an old seal and a monotonic conversation are both legitimately
an empty list, and `total_written is None` is what tells them apart.

`kn-4d32fe12` is the general rule, and it binds every future field on this dataclass and on
`Anatomy`, not just these four.

#### 1.4 The renderer: FOUR states, and an absent seal is one of them

`cli._print_subagent` (`src/jarvis/cli.py:2189-2234`). Four states, four sentences, and the
strings live in `cli` beside `PART_LABELS` because the dashboard reads the payload and not
these words:

| state | tested | rendered |
|---|---|---|
| `total_written is None` | FIRST | `cache anatomy not in this seal — sealed before it was recorded` |
| `writes` non-empty | 2nd | `writes cold-start 45.2k, prefix-miss 157.1k` — unchanged |
| `writes` empty, `total_written` truthy | 3rd | `wrote 334,427 in 115 calls — no single write reached the 20,000 floor (largest 19,381)` |
| `total_written == 0` | else | `wrote nothing to the cache` |

```python
SUB_UNDER_FLOOR = ("wrote {total} in {calls} calls — no single write reached the "
                   "{floor:,} floor (largest {largest})")
SUB_NO_WRITES = "wrote nothing to the cache"
#: THE FOURTH STATE, ahead of the other three: the figures are ABSENT, not zero. Every
#: order sealed before §1.1 added them has no key to read, and naming the SEAL is the
#: only wording that cannot be read as an absence of writes.
SUB_NOT_SEALED = "cache anatomy not in this seal — sealed before it was recorded"
```

**`SUB_NOT_SEALED` is tested FIRST, on `total_written is None`, and that order is
load-bearing.** An old seal carries neither the scalars nor `writes`-derived certainty, so
every later branch would read off absence: `writes` empty then falls through to
`SUB_NO_WRITES`, which is problem 4. After this branch, `wrote nothing to the cache` is only
ever a MEASURED zero — the one claim that line is allowed to make. It also names the SEAL
rather than the writes: the subagent's cache behaviour is unknown, not empty, and no wording
that starts with "wrote" can say that.

Then one more line per subagent, the boundary census, printed only when there is something
to say and worded so the zero case is the FINDING rather than the silence:

```python
SUB_NO_BOUNDARY = ("no boundary — one continuous conversation, so no re-write tax. "
                   "STRUCTURAL, not small: {written} was still written")
```

Its guard is `elif total:` over the same `total_written`, so an absent seal prints no census
line at all — `rewrite()` returned `None` and `SUB_NO_BOUNDARY` would be a second
unmeasured claim.

The `deeper` lines stay exactly as they are. `SUBAGENT_DEPTH_READ` stays **1**; deeper
subagents remain COUNTED and not read, and the "NOT read — depth read 1" line is the
unread-depth labelling q1215 requires kept. Where a depth is unread the report already says
so; what it did not say was where a FLOOR hid the figure, and §1.4 is that sentence.

The dashboard partner (`src/jarvis/ui/templates/_debug.html:101-133`) already renders
`writes_by_cause` and `deeper` from the payload. It carries THE SAME FOUR STATES in the same
words and the same order, opening on `{% if s.total_written is none %}` — the Jinja spelling
of the `is None` test, and not `{% if not s.total_written %}`, which would merge states 1
and 4 back together.

#### 1.5 `jarvis cost`: label the subagent side of the re-write tax, and name its zero

`bill._worker_extras` (`src/jarvis/bill.py:1320-1358`). The `rewrite` block keeps every key
it has — it is the TOTAL and `jarvis cost`'s headline arithmetic must not move — and gains
one nested key:

```python
            # q1215: the order-level rollup, LABELLED by side. `total` already summed
            # both (`Usage.__add__`, `usage.py:279-280`); what no surface could say is
            # which side contributed what, and the subagent side is ZERO BY ARITHMETIC,
            # never by being small.
            "by_side": {
                "main": {"tokens": session.main.rewrite_excess,
                         "boundaries": session.main.resume_boundaries,
                         "cache_write": session.main.cache_write},
                "subagent": {"tokens": session.subagents.rewrite_excess,
                             "boundaries": session.subagents.resume_boundaries,
                             "cache_write": session.subagents.cache_write,
                             "count": session.subagent_count,
                             "structural_zero": (
                                 session.subagents.rewrite_excess == 0
                                 and session.subagents.resume_boundaries == 0)},
            },
```

This block is computed from a LIVE `SessionUsage` and never from a seal, so §1.3's rule does
not reach it: every figure here is measured and `0` means zero.

One module constant beside `bill.ABSENT_NOTES`, for that module's stated reason — a caveat
worded differently in two renderers is one the reader learns to ignore:

```python
SUBAGENT_REWRITE_ZERO = (
    "of that, subagents contributed 0 tokens across 0 boundaries — a STRUCTURAL zero and "
    "not a small one: a subagent's written volume never exceeds its own context peak, and "
    "one continuous subagent conversation has no cache read going backwards, so the "
    "arithmetic can give nothing else. Those {count} subagent(s) still wrote {written} "
    "cache-creation tokens, itemised under the turn each ran in.")
```

`cli._print_bill` (`src/jarvis/cli.py:2011-2027`) prints it as the next line after the
re-write tax paragraph, only when `structural_zero` is true; when it is false it prints the
two sides plainly with no note, because a non-zero subagent side is a real finding and the
sentence above would be a lie about it. `ui/templates/_bill.html` reads the same constant.

### 2. `jarvis navigation`: the fleet's read volume, split by side

#### 2.1 A new LEAF module, `src/jarvis/navigation.py`

Imports `usage` (for `rows`, `blocks_of`, `transcript_root`, `index_sessions`) and
`catalog` (for `NavigationConfig`) and NOTHING else. It never opens the OS database and
never imports `ops`, `project_store` or `inspection` — the same constraint `inspection`
holds, for the same reason: a report over files on disk must not fail because a catalog or a
database moved.

Data model:

```python
SIDE_LEAD = "lead"
SIDE_SUBAGENT = "subagent"
SIDES = (SIDE_LEAD, SIDE_SUBAGENT)

@dataclass
class SideVolume:
    """One side's navigation behaviour over one or more transcripts."""
    side: str
    transcripts: int = 0
    symbol_calls: int = 0          # Serena symbol tools, either prefix
    text_search_calls: int = 0     # the `Grep`/`Glob` TOOLS
    nav_bash_calls: int = 0        # Bash whose command is a read/search
    code_nav_bash_calls: int = 0   # ...of a path with a configured code suffix
    other_bash_calls: int = 0
    read_tool_calls: int = 0       # the `Read` tool
    result_bytes: int = 0          # every attributed `tool_result`
    nav_bash_bytes: int = 0
    code_nav_bash_bytes: int = 0
    symbol_bytes: int = 0
    #: `tool_result` bytes whose `tool_use_id` matched no `tool_use` in the same file.
    #: REPORTED, never silently dropped and never in a share's numerator.
    unattributed_bytes: int = 0

    def code_nav_share(self) -> float | None:
        """`code_nav_bash_bytes / result_bytes`, or None on an empty corpus.

        None and NEVER 0.0: a zero share is a finding and an unmeasured one is not,
        which is `usage.rewrite_ttl_share`'s rule.
        """

@dataclass
class NavigationVolume:
    scope: str
    found: bool = False
    sides: dict[str, SideVolume] = ...     # both keys always present
    window_days: int | None = None
    def as_dict(self) -> dict[str, Any]: ...
```

These counters are `int = 0` on purpose and §1.3's rule does not apply: nothing here is
rehydrated from a persisted seal, every instance is built by a read that measured, and the
derived `code_nav_share()` carries the absence instead.

Functions:

* `classify_command(command: str, cfg) -> tuple[bool, bool]` — `(is_navigation,
  targets_code)`. First token after stripping env assignments and leading `sudo`; a pipeline
  or `&&` chain is navigation if ANY stage is, matching §5.3's method; `targets_code` is any
  whitespace-split token ending in a configured suffix. Deliberately NOT an argument parser:
  §5.3's BEFORE numbers came from token matching and a cleverer classifier makes the AFTER
  figure incomparable.
* `is_symbol_tool(name: str, cfg) -> bool` — strips a leading `mcp__<server>__` and matches
  the bare name, so `mcp__serena__find_symbol` and
  `mcp__plugin_serena_serena__find_symbol` both count. Both prefixes exist in the fleet
  (`dispatch.SERENA_TOOL_PREFIXES`, `src/jarvis/dispatch.py:49`).
* `read_transcript(path: Path, side: str, cfg) -> SideVolume` — one pass with
  `usage.rows(path)`: build `{tool_use_id: name_and_command}` from `tool_use` blocks in
  `assistant` rows, then attribute each `tool_result` block's serialised content length to
  the producing id. One pass and no `needle`, because the two row kinds are both needed and
  two filtered passes read the file twice.
* `read_session(session_id, cfg, *, index=None) -> NavigationVolume` — the per-order path.
  Lead files are `index[session_id]` (`usage.index_sessions`); subagent files are
  `path.with_suffix("") / "subagents" / "*.jsonl"` for each of them.
* `read_tree(root=None, cfg, *, days=None) -> NavigationVolume` — the fleet path.
  `<slug>/<uuid>.jsonl` is a LEAD, `<slug>/<uuid>/subagents/agent-*.jsonl` is a SUBAGENT,
  and `days` filters by file mtime BEFORE opening anything.

**The side comes from the PATH, and that is not a workaround.** No transcript row carries
`isSidechain`. The OS mints the lead session id itself at spawn (`worker_session.start`) and
stores it on `work_orders.session_id`, so for an order the lead file is identified and not
guessed; Claude Code writes every subagent beside it under `<session-id>/subagents/`, which
`usage.read_session:1039-1049` already relies on. Nothing is inferred from content and no
vendor field is invented.

**The cost budget, and why the fleet scope is opt-in.** `~/.claude/projects` is **2.7G**
with **11,889** lead transcripts; the Jarvis-worktree subset is **273** directories,
**567M** lead plus **238M** subagent. A no-argument `jarvis navigation` therefore does NOT
read the root: it requires a project, an order, or an explicit `--fleet`, and both wide
paths honour `--days` (default `navigation.window_days`, 7). Read-only, no model call, same
posture as `jarvis inspect`.

#### 2.2 The CLI surface

`jarvis navigation [target] [--project P] [--fleet] [--days N] [--json]`, a TOP-LEVEL
command (q1216) — not a `jarvis cost` subcommand: cost is the money surface, this is
behavioural volume, and burying the 41.3% successor under it hides it.

* `cli.build_parser` — a new `sub.add_parser("navigation", …)` beside `inspect`
  (`src/jarvis/cli.py:543-557`), and a dispatch line in `cli.main` beside
  `src/jarvis/cli.py:4744`.
* `cli.cmd_navigation(args)` + `cli._print_navigation(payload)`. The renderer DERIVES
  NOTHING: every number and every share comes out of `as_dict()`, which is what keeps the
  CLI and the dashboard from disagreeing.
* `ops.navigation_report(target=None, project=None, *, fleet=False, days=None) -> dict` —
  resolves a target the way `ops.inspect_report` does (feature order first, then work
  order, then a project name), reads `ops.navigation_config(project)` (new, modelled on
  `ops.inspect_config`, `src/jarvis/ops.py:12170-12185`: best-effort, falling back to
  `NavigationConfig()` rather than to None, because every default here is a pattern list
  with a measured justification).
* **The per-order path is also a section on `jarvis inspect`** (q1216): `ops.inspect_report`
  adds a `"navigation"` key per unit from `navigation.read_session(...)`, and
  `cli._print_anatomy` prints it after the `tools:` block
  (`src/jarvis/cli.py:2325-2329`) via `_print_navigation`. One reader, two surfaces.

Report shape, printed and in `--json`: SHARES, not raw megabytes, so a differently sized
corpus stays comparable (§5.3 item 3). Per side: symbol calls, text-search tool calls,
Bash-navigation calls, and `code_nav_bash_bytes / result_bytes` as a percentage. The BEFORE
line to compare against is stated in the output footer: **0 symbol calls, 41.3% of
14,558 MB**, from spec 2026-10-01 §5.3.

#### 2.3 The catalog: `navigation.*`, fleet-wide with per-project override

q1216's condition. The classifier's patterns are DATA, never module constants — otherwise
re-measuring under a different definition of "navigation" needs a code change and a release.

`src/jarvis/catalog.py`, defaults beside the `inspect` block
(`src/jarvis/catalog.py:750-843`):

```python
#: Bash commands that count as reading or searching code. EXACTLY §5.3's set and no more:
#: the BEFORE figure (41.3%) was measured with these six, and a wider set makes the AFTER
#: figure incomparable rather than better.
DEFAULT_NAVIGATION_BASH_COMMANDS = ("cat", "head", "sed", "grep", "rg", "find")
#: Symbol tools, BARE — `navigation.is_symbol_tool` strips the `mcp__<server>__` prefix,
#: because both `mcp__serena__` and `mcp__plugin_serena_serena__` exist in this fleet.
#: `search_for_pattern` is DELIBERATELY ABSENT: it is text search with a Serena name, and
#: counting it as a symbol call is the vacuity trap kn-a397fb52 documents.
DEFAULT_NAVIGATION_SYMBOL_TOOLS = ("find_symbol", "find_referencing_symbols",
                                   "get_symbols_overview", "find_declaration",
                                   "find_implementations")
#: The TOOLS that are text search. `Bash` is not here and must not be — a worker runs all
#: sorts of legitimate shell; the COMMAND is classified instead.
DEFAULT_NAVIGATION_TEXT_SEARCH_TOOLS = ("Grep", "Glob")
#: Which files make a read a CODE read. `.py` because that is what the 41.3% measured.
DEFAULT_NAVIGATION_CODE_SUFFIXES = (".py",)
#: The default window for a wide scope, in days. Seven, for
#: `DEFAULT_INSPECT_ALARM_REWRITE_WINDOW_DAYS`' reason: a share averaged over all history
#: reports the trend away, and the trend is the whole question after `worker.bash_first`.
DEFAULT_NAVIGATION_WINDOW_DAYS = 7


@dataclass
class NavigationConfig:
    enabled: bool = True
    bash_commands: tuple[str, ...] = DEFAULT_NAVIGATION_BASH_COMMANDS
    symbol_tools: tuple[str, ...] = DEFAULT_NAVIGATION_SYMBOL_TOOLS
    text_search_tools: tuple[str, ...] = DEFAULT_NAVIGATION_TEXT_SEARCH_TOOLS
    code_suffixes: tuple[str, ...] = DEFAULT_NAVIGATION_CODE_SUFFIXES
    window_days: int = DEFAULT_NAVIGATION_WINDOW_DAYS
```

`_parse_navigation(raw, base=None, where="os.navigation")`, copying
`_parse_inspect`'s field-level inheritance exactly (`src/jarvis/catalog.py:1655-1727`): a
project naming one key keeps the OS answer for the rest, so no caller consults two objects.
Three refusals, each because the failure is silent otherwise:

1. a pattern list that is not a list of strings — `_err(f'"{where}.{name}" must be a list of strings')`;
2. an EMPTY pattern list — an empty classifier reports 0% everywhere and looks like a win;
3. `window_days < 1`, and a `code_suffixes` entry not starting with `.`.

Wire it the way `inspect` is wired, four sites: `OsConfig.navigation`
(`src/jarvis/catalog.py:1342`'s block), `ProjectSpec.navigation`
(`src/jarvis/catalog.py:1223`'s block), `os.navigation` in `parse_catalog`
(`src/jarvis/catalog.py:2105`), and the per-project override
(`src/jarvis/catalog.py:2206-2208`).

`jarvis config set <project> navigation.bash_commands '["cat","rg"]'` then needs no further
code: `ops.set_config` writes any dotted path and re-parses to validate, so the refusals
above ARE the error messages the user sees. **No `APPLY_RULES` entry**:
`ops.apply_class` defaults to `hot` (`src/jarvis/ops.py:10824-10827`) and that is correct
here — nothing is baked into a worker's settings file, the value is read when the report
runs.

### 3. How it is proven

Targeted tests only, in the existing files, over FIXTURE transcripts written under a
`monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, …)` root. **No test may read a live
transcript**: the `write_transcript` fixture (`tests/test_inspection.py:240-259`) is the
pattern, it already takes `subagents={"agent-<id>": rows}`, and the root `conftest.py`
isolation gate is the floor under it.

1. **`tests/test_inspection.py`** — the wo-fb7c0fc2 shape, scaled: a subagent of many
   writes all under 20,000. Assert `writes == []`, `total_written` equals their sum,
   `max_write` is the largest and is under the floor, `write_floor == 20_000`. Then a
   subagent with no writes at all: `total_written == 0` — **an `int`, asserted with
   `is not None`**, because the live path always measures and a `None` here would mean
   `_read_subagent` stopped reading. Then boundaries: a subagent whose `cache_read` goes
   backwards once yields one `Boundary`; a monotonic one yields `[]`, and
   `rewrite()["tokens"] == 0` with `boundaries == 0`. Then `cold_prefix_floor=None` yields
   `BOUNDARY_UNDECIDED` and `undecided_boundaries == 1`. Then the DEFAULTS, directly:
   `SubagentAnatomy(task_id="x").total_written is None` and `.rewrite() is None`, which is
   the assertion an `int = 0` regression trips.
2. **`tests/test_inspection.py`, renderer** (beside the existing render tests at
   `tests/test_inspection.py:2174-2205`) — capsys over `cli._print_anatomy`. Four
   acceptance criteria, one per state:
   * the under-floor subagent's line contains the token total and `no single write reached
     the`, and the string **`no large writes` appears nowhere in the output**;
   * the zero-write subagent renders `wrote nothing to the cache`;
   * **the absent-seal subagent — a payload with `total_written: None`, the shape
     `jarvis inspect wo-fb7c0fc2` reads off its 2026-10-01 seal — renders
     `SUB_NOT_SEALED`, and the strings `wrote nothing to the cache` and
     `SUB_NO_BOUNDARY`'s `no boundary` appear NOWHERE in the output.** Problem 4 is exactly
     the second half of that assertion: the first state was reachable while the falsehood
     was still being printed;
   * the non-empty `writes` state is unchanged.

   The `deeper … NOT read — depth read 1` line is still present in all four.
3. **`tests/test_autopsy.py`** — two round-trips, because the seal has two directions
   (§1.3):
   * `_subagent` then `_read_subagent` preserves all five new fields and the boundary list,
     and `as_dict()` of the rehydrated anatomy equals the fresh one's (the equality rule
     `anatomy_for` is built on);
   * **an OLD seal row — a dict with `task_id`, `turns` and `writes` and NONE of the four
     new keys — rehydrates with all four `is None`, `rewrite() is None`, and
     `as_dict()["rewrite"] is None` with the four keys PRESENT.** This is the test that
     would have caught problem 4; the first draft had only the forward round-trip, where
     every key is written by the test itself and the zero default never shows.
4. **`tests/test_usage.py`** — the structural zero, asserted as a PROPERTY and not a
   constant: on a fixture whose main side has a backwards cache read and whose subagent does
   not, `SessionUsage.subagents.rewrite_excess == 0`,
   `SessionUsage.subagents.resume_boundaries == 0`, `subagents.cache_write > 0`, and
   `total == main + subagents` on all three.
5. **`tests/test_bill.py`** — `rewrite["by_side"]["subagent"]["structural_zero"] is True`
   with `cache_write > 0`; `cli._print_bill` prints `SUBAGENT_REWRITE_ZERO` with the
   written figure in it; and a hand-built non-zero subagent side prints the two sides
   WITHOUT the structural note.
6. **New `tests/test_navigation.py`** — a classifier table (`sed -n 1,50p src/x.py` →
   `(True, True)`; `cat README.md` → `(True, False)`; `uv run pytest` → `(False, False)`;
   `grep -rn foo src/x.py | head` → `(True, True)`); `is_symbol_tool` on both prefixes and
   on `search_for_pattern` (False); bytes attributed to the producing `tool_use` id; a
   `tool_result` with an unknown id landing in `unattributed_bytes` and NOT in
   `result_bytes`' shares; the side split derived from a fixture tree with one lead and two
   subagent files; `code_nav_share() is None` on an empty corpus, never `0.0`; and
   `--days` excluding a file by mtime.
7. **`tests/test_catalog.py`** — `navigation.*` parses, a project overriding one key
   inherits the rest, an empty `bash_commands` raises `CatalogError` naming the key, a
   non-list raises, a suffix without a leading dot raises.
8. **`tests/test_config_console.py`** — `apply_class("projects.x.navigation.bash_commands")`
   is `hot`, so the console's note to the user is right.

No eval, and no change to `evals/llm/test_navigation_judgment.py`: that suite grades whether
a worker NAVIGATES well, this measures what the fleet DID. They answer different questions
and merging them would make a measurement depend on a paid run.

## Rejected alternatives

1. **Read deeper than `SUBAGENT_DEPTH_READ = 1`.** The obvious reading of the complaint,
   and it fixes nothing: wo-fb7c0fc2's 334,427 tokens were in a transcript that WAS read,
   at depth 1, and thresholded away. Raising the depth also costs a glob per subagent per
   report and breaks the §4b promise that the report never implies completeness it does not
   have. The depth stays 1 and deeper subagents stay counted.
2. **Lower `report_write_floor`.** Would surface wo-fb7c0fc2 and break the thing the floor
   exists for: inside one turn every call after the first writes a small delta, and
   classifying those labels the cache WORKING as a `prefix-miss`
   (spec 2026-08-30 §3). The floor is load-bearing; the fix is a figure beside it.
3. **Fold subagent writes into the spawning turn so the parent's tax includes them.**
   Refused by q1215 and by §4b: a parent turn that waited on a join did not pay that write,
   and attributing it upward names the wrong turn as the prefix break. The rollup `jarvis
   cost` reports is at the ORDER level, where both sides are already summed, and the only
   thing missing was the label.
4. **Derive a subagent re-write tax some other way, so the number is not zero.** There is
   no other way: `rewrite_excess` has one threshold-free definition and it yields 0 here.
   Inventing a per-subagent cold-start charge would put a figure on the bill that no
   arithmetic supports, which is worse than the silence it replaces.
5. **`jarvis cost navigation` as a subcommand.** Ruled out by q1216 and the reason holds:
   cost is the money surface, and a fleet-wide behavioural share filed under it is a finding
   nobody scrolls to.
6. **Classifier patterns as module constants in `navigation.py`.** Ruled out by q1216.
   Re-measuring with `awk` or `.ts` in the set would then need a code change and a release;
   as catalog keys it is a `jarvis config set`.
7. **Walk `~/.claude/projects` by default.** 2.7G and 11,889 lead transcripts per
   invocation, for a command whose per-order answer is what `jarvis inspect` wants. The wide
   scope stays behind `--fleet` and a window.
8. **Use `isSidechain`, or infer the side from content.** No transcript row carries it. The
   path is unambiguous and anchored on a session id the OS minted itself; a content
   heuristic would be a second, wrong answer to a question the filesystem already answers.
9. **Keep `int = 0` and BACKFILL the old seals** (problem 4's obvious fix — re-run
   `inspection.read_session` over every settled order and re-seal it). Refused on three
   counts. The transcripts are the thing a seal exists because they EXPIRE, so the orders
   that most need the figures are the ones a backfill cannot read, and it would write a
   measured-looking `0` for exactly those — the same falsehood, now persisted. It is also a
   migration over three SQLite databases to fix a renderer. And it leaves the defect in
   place: the next field added to `SubagentAnatomy` needs another backfill, where
   `int | None` needs nothing. A variant — `int = 0` plus a `sealed_version` column to
   gate the renderer on — was rejected for the same last reason, and for putting the
   absence somewhere other than the field that is absent.
10. **`int = 0` with `-1` as the sentinel for "not measured".** A magic number every
    renderer, the dashboard template and `--json` must each know, and one that arithmetic
    silently accepts: `total_written - context_peak` yields a number rather than raising.
    `None` is refused by the type checker at every site that forgets it.

## Not in scope

* **A per-subagent budget cap.** Finding **kn-5e49403f**: a cap before the measurement has
  nothing behind it. This spec produces the measurement; the cap is a separate decision
  taken on its numbers, and nothing here reserves, bounds or refuses a subagent's spend.
* **The v12 crew contract.** Untouched. No seat's `tools:`, model or mandate changes, and
  nothing here is enforced against a worker.
* **The AFTER navigation figure.** `jarvis navigation` is the instrument; the comparison
  against 41.3% needs the fleet to have run on `worker.bash_first: off` long enough to
  accumulate transcripts. Quoting an AFTER number from this worktree would be quoting a
  fixture.
* **Alarms.** No new `ALARM_KINDS` entry, and `alarms()` still judges the last turn of the
  MAIN transcript only. A navigation share is a standing condition, not a live turn; if it
  earns an alarm it earns it from this command's numbers.
* **Languages other than Python.** `code_suffixes` defaults to `(".py",)` because that is
  what 41.3% was measured over. Another project changes the key.
* **A `/navigation` dashboard page.** The payload is renderer-ready and `jarvis inspect`'s
  page gets the per-order section; a fleet page is a follow-up.
* **Auditing every OTHER seal-rehydrated dataclass for problem 4's defect.** `kn-4d32fe12`
  is the rule; this spec applies it to `SubagentAnatomy` only. `autopsy._read_turn`,
  `_read_write` and `_read_boundary` were not swept, and a field of theirs added with an
  `int = 0` default before today still carries the same latent falsehood. That sweep is a
  separate order, and it wants `jarvis io`, not a patch here.
