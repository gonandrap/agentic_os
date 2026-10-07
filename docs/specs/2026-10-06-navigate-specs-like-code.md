# Navigate specs like code: progressive disclosure for docs, and refuse whole-file dumps

Feature order fo-2cc90946. Planner wo-5ef5f42c.

The fleet navigates Python with a symbol index and navigates markdown by dumping the
whole file into the conversation, where it rides along in every later API call of the
session and is re-written at every cache TTL expiry. This feature gives markdown the
same progressive disclosure Serena gives code: a table of contents, one section, and a
section-granular search — plus a refusal, default off, for the dump.

---

## 1. The problem, measured

Measured 2026-10-06 by the planner over 877 transcripts under
`~/.claude/projects/*agentic*/`, attributing each `tool_result` to the `tool_use` that
produced it in the same file. Tokens are `chars // 4`.

**The `Read` tool, by extension and by `limit`:**

| | tokens | calls | avg |
|---|---|---|---|
| `.png`, `limit=none` | 8,044,824 | — | — |
| `.md`, `limit=none` | 1,032,302 | 199 | 5,187 |
| `.py`, `limit<=200` | 1,242,656 | 1,422 | 873 |
| `.py`, `limit=none` | 487,048 | 133 | 3,662 |
| `.py`, `limit>200` | 317,713 | 79 | 4,021 |
| `.md`, `limit<=200` | 72,739 | 75 | 969 |
| `.md`, `limit>200` | 35,230 | 6 | 5,871 |

**Bash, by command and by the suffix the command names:**

| | tokens | calls | avg |
|---|---|---|---|
| `sed … .py` | 8,719,547 | 7,499 | 1,162 |
| `head … .py` | 2,321,354 | 5,411 | 429 |
| `cat … .py` | 662,025 | 750 | 882 |
| `sed … .md` | 621,456 | 676 | 919 |
| `cat … .md` | 437,890 | 905 | 483 |
| `head … .md` | 193,341 | 608 | 317 |

Five readings decide the whole design:

**(a) `limit is None` IS the predicate.** Every expensive `Read` is one the model made
with no `limit` key, and every cheap one carries a small `limit`. 1,422 `.py` reads at
`limit<=200` average 873 tokens; 133 at `limit=none` average 3,662. So "is this a whole
file read" is answerable from `tool_input` alone — no `stat`, no line count, no disk
access, no latency added to any `Read`. A threshold on `limit` is the secondary arm, for
`limit>200`.

**(b) `.md` whole-file reads are 1.03M tokens over 199 calls.** 199 calls is a small
number and 5,187 tokens each is why it matters: these are the 82 specs under `docs/specs`
and `docs/superpowers/specs`, read whole.

**(c) the Bash `.md` dumps are worth MORE than the `Read` ones**: `sed` + `cat` + `head`
on `.md` is 1.25M tokens over 2,189 calls. A refusal that covers only the `Read` tool is
one `sed -n '1,2000p'` away from being evaded, and `sed` is the command the fleet
actually reaches for. The docs arm covers both tools or it covers nothing.

**(d) `sed … .py` at 8.72M tokens is already in scope of an existing hook** —
`hooks.py_nav_decision`, which ships `off` and whose flip is sequenced behind issue 936
(kn-76f7d3f6, kn-358ffb53). This feature does not touch that decision. What it adds on
the `.py` side is the `Read` arm — 805k tokens over 212 calls — under that same existing
key, so the existing flip decision still governs one policy and not two.

**(e) `.png` at 8.04M tokens is the single largest `Read` cost in the fleet** and is NOT
this feature: those are dashboard screenshots, not file dumps, and no table of contents
helps. Filed to the backlog.

### 1.1 What this feature is not

- Not a graph or vector index over the docs. Markdown headings already give the tree.
- Not a flip. Every new key ships `off`; §7's eval is the evidence a later order flips on.
- Not a change to what any existing counter means (§2.2).
- Not a refusal of text search. `grep`, `rg` and `find` over markdown stay legal, always.

---

## 2. Architecture and the three rules every child is bound by

### 2.1 Where each piece lives

```
navigation.py      stdlib-only leaf. The CLASSIFIERS: what is a doc, what dumps one.
nav_volume.py      the fleet COUNTER, reads navigation.py's classifiers.
spec_index.py      NEW stdlib-only leaf over sections.py: toc, section, search.
ops.py             the payloads `jarvis spec` prints.
cli.py             the `jarvis spec` subparser and the rendering.
hooks.py           the REFUSAL, reads navigation.py's classifiers.
catalog.py         the new key.
dispatch.py        the env var, and what a child is told about its spec.
worker_brief.py    the navigation posture in a worker's prompt.
concision.py       what a SubagentStart injects.
```

`hooks.py` imports `navigation` on every `PreToolUse`. It must not grow an import of
`spec_index`, `catalog` or anything else: a new import there is a cost paid per command
the fleet runs.

`dispatch.py` has TWO owners in this feature and they land on independent branches.
§5 edits the env block beside `JARVIS_PY_NAV_HOOK` at dispatch.py:215 and nothing else in
that file. §6 edits the spec prose at dispatch.py:456-467 plus `_planner_prompt` and
`_common_briefing`, and nothing else. Either straying conflicts with the other.

`jarvis fo spec <fo-id>` already exists (cli.py:969, `ops.feature_spec`): it prints the
spec a FEATURE ORDER holds. `jarvis spec` is a second, PATH-addressed surface. Nothing
in this feature changes `jarvis fo spec` or routes through it.

### 2.5 Every section carries an anti-vacuity pin

Three pin idioms already exist in this tree and each section below names which it owes:

Every citation below is by TEST NAME, because line numbers drift. Find them with
`grep -n '<name>' tests/` — do not trust a line number anyone hands you.

- **identity, not equality** —
  `tests/test_navigation.py::test_hooks_re_exports_the_moved_helpers_rather_than_copying_them`
  asserts `hooks._mask_shell_text is navigation._mask_shell_text`, and
  `::test_the_catalog_defaults_are_the_leafs_sets_and_not_a_second_definition` asserts the
  catalog defaults ARE the leaf's sets. A copied body passes equality and then drifts.
- **AST enclosure** —
  `tests/test_remedies.py::test_the_acting_calls_stay_inside_the_handlers` walks
  `ast.parse` over a module, collects called names per enclosing `FunctionDef`, and asserts
  `found` is non-empty FIRST so the walk cannot pass on any module; the test beside it
  re-runs the shape over synthetic source that violates it, to prove the pin would catch
  the move it forbids. Copy both halves or the pin is decoration.
- **re-apply the shipped predicate** —
  `evals/llm/test_navigation_judgment.py::test_the_hook_does_not_refuse_a_literal_word_search_in_a_markdown_file`
  grades recorded commands by calling the shipped classifier in Python, because a
  `PreToolUse` recorder sees the ATTEMPT and not the verdict.

### 2.2 A predicate read by a counter and by a hook is never narrowed in place

kn-358ffb53, and it is the standing rule of this file. `navigation.navigates_source` is
read by `nav_volume`, `inspection`, `autopsy` and the evals to COUNT, and by
`hooks.py_nav_decision` to REFUSE. The two have opposite error costs, and
`nav_volume.BEFORE_NOTE`'s fleet baseline was measured with the predicate as it stands.

So: **the doc classifiers are NEW sibling functions, and `navigates_source`'s body is not
edited.** No existing counter changes meaning, no existing counter is renamed, and
`BEFORE_NOTE`'s existing sentence is left byte-identical — §1's figures travel as a
second, additive note. An enforcement-only narrowing travels as a default-off keyword
argument at the hook's call site, never as an edit to a shared body.

Corollary: nothing in this feature depends on wo-d2d777dc / issue 936. The `scoped_sweeps`
fix is about `_sweeps_the_tree`, which only fires for `grep`/`rg`/`find` — and those three
are deliberately absent from the doc command set (§3.1). Do not add that dependency.

### 2.3 A spec path is three-valued

Three classes of markdown, and the middle one is the trap:

1. `docs/**/*.md` — the 82 specs and everything beside them. **A spec.** This is where
   §1(b)'s 1.03M tokens are.
2. `.jarvis/features/<fo-id>/<name>.md` — the whole spec `specs.materialize` writes and
   `dispatch.py:464` points every feature child at. **A spec**: measured at only 9,733
   tokens over 5 calls today, so it is in scope for correctness, not for volume.
3. `.jarvis/features/<fo-id>/sections/<wo-id>.md` — the child's OWN assigned section,
   which `dispatch.py:462` tells it to read first. **NEVER a spec, at any size.** Refusing
   this one strands the child on its own brief.

One function answers all three, once, in `navigation.py`. Two spellings of this test is
the failure kn-7f5f2d0d records for the shell masker: a copied body passes equality and
then drifts.

### 2.4 Ordering inside `preflight_decision`

`hooks.preflight_decision` has a docstring whose entire content is ordering arguments,
and two of them bind here:

- **A Bash arm placed after `is_jarvis_command_chain`'s `_allow` is unreachable in
  production however green its unit test.** The new Bash doc arm goes beside
  `py_nav_decision` at hooks.py:2131-2135, before that auto-allow.
- **No `_allow` branch, ever**, in a navigation decision function. It denies or it
  returns `None`, so it can never hand out something a gate would have caught.
  `py_nav_decision`'s docstring states this as a contract; the new function inherits it.

There is today no `Read` branch in `preflight_decision` at all. The new one goes in its
own `if tool == "Read":` block, before the `Edit`/`Write`/`NotebookEdit` block and not
folded into the `mcp__` branch. There is no auto-allow in front of it, so the first
argument above does not apply to it — and the reason it does not apply must be written
into the docstring, because the next reader will assume it does.

---

## 3. The doc classifiers and the measurement counters

Owner of `navigation.py`'s new symbols, `nav_volume.py` and `cli._print_navigation`.
Lands FIRST: §1's figures were produced by a planner's ad-hoc script, and an AFTER figure
nobody can get from a command is not a measurement. It also owns the predicate §5 and §7
both read, so there is exactly one body.

### 3.1 New symbols in `navigation.py`

```python
#: Markdown. A sibling of SOURCE_SUFFIXES, which is unchanged.
DOC_SUFFIXES: tuple[str, ...] = (".md",)

#: The commands that DUMP a file. `grep`, `rg` and `find` are deliberately ABSENT:
#: text search in a .md is legitimate and refusing it is this feature's MUST NOT.
#: `tail` is absent too — measured at 137 tokens a call, it is already cheap.
DOC_DUMP_COMMANDS: tuple[str, ...] = ("cat", "head", "sed")

def dumps_doc(command: str,
              suffixes: tuple[str, ...] = DOC_SUFFIXES,
              commands: tuple[str, ...] = DOC_DUMP_COMMANDS) -> bool:
    """Whether this Bash command DUMPS a markdown file."""

def is_spec_path(path: str) -> bool:
    """§2.3's three-valued test. The ONE spelling."""
```

`dumps_doc` reuses `_mask_shell_text`, `_statements`, `_command_word` and `_reads_with_sed`
by calling them — a `.md` inside a quoted string is prose, and `sed` without `-n` is an
edit. It must NOT call `_sweeps_the_tree`: with `grep`/`rg`/`find` absent from the command
set there is no sweep to classify, and reaching for it is how the issue-936 defect would
be inherited.

`is_spec_path` tests path COMPONENTS, never substrings: a `docs` component for class 1, a
`.jarvis`/`features` prefix for class 2, and a `sections` component anywhere inside a
`.jarvis/features/` path for class 3, which returns `False` and is checked before class 2.
`hooks.spec_shape_decision`'s `"specs" not in path.parts` is the precedent for the
component test.

### 3.2 New counters in `nav_volume.SideVolume`

Additive only. `result_bytes`, `nav_bash_bytes`, `code_nav_bash_bytes`, `symbol_bytes`,
`unattributed_bytes` and `NavigationConfig.code_suffixes` keep their exact current
meaning (§2.2).

```
read_tool_bytes: int        # the Read tool's own result bytes
doc_read_bytes: int         # ...of a .md path
whole_file_read_calls: int  # Read with no `limit` in its input
doc_dump_bash_calls: int    # Bash that `dumps_doc`
doc_dump_bash_bytes: int
```

plus a per-tool result-byte breakdown, which nothing in the tree reports today
(`inspection.tool_profile` is per-tool SECONDS, and wo-38456776's fleet cost view is
per-order dollars). `NavigationConfig` gains `doc_suffixes` and `doc_dump_commands` so
re-measuring needs no release, the way `code_suffixes` already works.

Two traps in this dataclass:

- `nav_volume._merge` folds every field with `+` over `vars()`. A `dict[str, int]` field
  raises `TypeError` there. Either give `_merge` an explicit dict branch or model the
  per-tool bytes as something `+` folds.
- `SideVolume.as_dict`'s `calls` block is gated on `calls_reported`, because the
  per-order projection omits call counts (Neo q1242). New CALL counters go inside that
  gate; new BYTE counters go outside it. Backwards, and `jarvis inspect`'s section claims
  per-order call counts it does not have.

`BEFORE_NOTE` is not edited. §1's figures go in a new `DOC_BEFORE_NOTE` added beside it
in the payload, labelled with its corpus (877 transcripts) and its date (2026-10-06), so
the two baselines are separately attributable. It is a RECORDED BASELINE, not an
assertion: whether doc reads fell is unprovable until a later order flips the key and a
window passes. The acceptance criterion is that the command reproduces a number.

`tests/test_nav_volume.py` defines module-level `CALL_KEYS` and `BYTE_KEYS` tuples and
three tests read them. New call counters go in `CALL_KEYS`, new byte counters in
`BYTE_KEYS`, or every new key is untested on both projections.

### 3.4 The pins this section owes

- AST enclosure over `FunctionDef navigates_source` in `navigation.py`: the set of names
  it calls is unchanged, so no branch and no helper was added to it.
- AST enclosure over `FunctionDef dumps_doc`: `_sweeps_the_tree` is NOT among the names it
  calls, and behaviourally `dumps_doc("find docs -name '*.md'")` is False.
- `DOC_SUFFIXES is not SOURCE_SUFFIXES`, `dumps_doc is not navigates_source`, and
  `navigates_source("cat README.md", SOURCE_SUFFIXES) is False` — that last assertion
  already lives inside
  `tests/test_navigation.py::test_bookkeeping_reads_do_not_navigate_source` and turns red
  for anyone who "measures markdown" by widening `SOURCE_SUFFIXES`. It is not to be edited.
- `DOC_BEFORE_NOTE is not BEFORE_NOTE`, and the substrings `tests/test_nav_volume.py`
  already asserts in `BEFORE_NOTE` are untouched — find that test by its
  `nav_volume.BEFORE_NOTE` reference.
- The fold: a tree with lead AND subagent transcripts, both having made `Read` and `Bash`
  calls, folds the per-tool bytes to the correct sum. A single-transcript test passes
  vacuously, and `_merge`'s `+`-over-`vars()` raises `TypeError` on a `dict` field.
- `tests/test_navigation.py::test_the_leaf_imports_nothing_from_jarvis` stays green, and so
  do `tests/test_inspection.py`, `tests/test_autopsy.py` and `tests/test_autopsy_read.py`:
  `inspection` and `autopsy` both read `nav_volume`, so a renamed field breaks
  `jarvis inspect` and `jarvis autopsy` silently in the per-order projection.

### 3.3 The surface

`jarvis navigation` and its `--json` gain the new figures. There is no dashboard route
for this payload (`_print_navigation` lives only in `cli.py`), so this is CLI-only.

---

## 4. `jarvis spec toc | section | search`

Owner of a new `src/jarvis/spec_index.py`, `ops.py` and the `cli.py` subparser.
Mechanical: no model call, no new dependency, no index on disk.

Every `jarvis <verb>` chain is already auto-allowed by `hooks.is_jarvis_command_chain`, so
these three run in a background worker with no permission prompt and no hook work. That
same function refuses any command containing `| ; \` $ < >`, so a query carrying one of
those gets a prompt and stalls the worker — say so in the `--help`.

### 4.1 `spec_index.py`

Stdlib-only, imports `sections` and nothing else from `jarvis`.

```python
@dataclass(frozen=True)
class Section:
    number: str   # "3" when the heading is numbered, "" otherwise
    name: str
    level: int
    line: int
    tokens: int   # chars // 4, an ESTIMATE, labelled as one in every rendering

def toc(markdown: str) -> list[Section]: ...

def section(markdown: str, which: str) -> str | None: ...

@dataclass(frozen=True)
class Hit:
    path: str
    section: str   # the ref `jarvis spec section` accepts, verbatim
    line: int
    context: str   # ONE line

def search(root: Path, words: str, *, suffixes: tuple[str, ...] = (".md",),
           limit: int = 40) -> list[Hit]: ...
```

`section` DELEGATES to `sections.extract_section`, and `toc` derives `number` from
`sections.HEADING_RE`'s matches. `sections.find_heading`'s docstring says it is THE ONE
MATCHER and the reason: a second matcher gives a child a brief cut from §4 and a link
that lands on §5. A re-implementation here is that bug.

`tokens` is `chars // 4` because a tokenizer is a dependency and this feature adds none.
An unlabelled wrong number is worse than a labelled approximate one, so every rendering
says estimate.

`search` matches at SECTION granularity: it finds the words, then reports the section
that contains them, deduplicated per section. Not a grep line list — the point is that the
caller's next call is `jarvis spec section`, and the output names it.

### 4.2 `ops.py` and `cli.py`

`ops` returns the payload and `cli` renders it, deriving nothing — the pattern
`ops.navigation_report` and `cli._print_navigation` already follow.

```python
def spec_toc(path: str, project: str | None = None) -> dict[str, Any]: ...
def spec_section(path: str, which: str, project: str | None = None) -> dict[str, Any]: ...
def spec_search(words: str, project: str | None = None, limit: int = 40) -> dict[str, Any]: ...
```

Every hit in every payload carries the exact `jarvis spec section <path> <ref>` command
that shows it, the way `jarvis search` already prints the command per hit. That is what
makes the output navigable rather than a dump with extra steps.

Path resolution refuses a path outside the resolved project root rather than traversing
it. `search` defaults to the CURRENT project's tree; a wide scope is explicit, because 82
files and 1.4MB is a sub-second regex scan per project and the fleet-wide cost is
unmeasured.

### 4.3 The contract the other sections depend on

These three command strings are quoted verbatim in §5's refusal text and §6's prose, and
they are fixed by this section:

```
jarvis spec toc <path>
jarvis spec section <path> <n|name>
jarvis spec search "<words>"
```

They are repeated as literals in `hooks.py` rather than imported: §2.1's rule about what
`hooks` may import.

### 4.4 The pins this section owes

- `spec_index.section(md, which) == sections.extract_section(md, which)` over a table
  covering: a number matching a numbered heading; a number matching NO numbered heading
  (both `None`, never "the Nth heading"); a name matched case-insensitively as a
  substring; a nested `###` inside the target (included) and the next `##` (not).
  `tests/test_question_diet.py::test_extract_section_by_number_and_name` and
  `::test_find_heading_is_the_one_matcher_extract_section_uses` are the existing models.
- A source pin in the idiom of
  `tests/test_navigation.py::test_the_eval_uses_the_shipped_classifier`: no heading regex is
  defined in `spec_index.py`, and `toc` derives from `sections.HEADING_RE` by identity.
- `toc` over TWO documents: a tidy fixture, and this spec file itself, so the numbered
  sections and the unnumbered `Agent profile` appendix are both exercised. One tidy
  fixture is where this suite goes vacuous.
- `hooks.is_jarvis_command_chain('jarvis spec search "a|b"')` is `False`, and the `--help`
  text says so.
- Path traversal: a path outside the resolved project root exits non-zero naming the root
  it refused to leave.
- `jarvis fo spec --help` and `jarvis spec --help` both exit 0, and
  `tests/test_feature_orders.py`, `tests/test_spec_page.py` and `tests/test_search.py`
  stay green — a new top-level verb can reorder argparse subcommands.

---

## 5. The Read branch and the dump refusals

Owner of `hooks.py`, the new catalog key, `dispatch.py`'s env, `ops.py`'s config-effect
table, and the unit assertions. Everything here ships `off`, so merging it changes the
behaviour of no running worker.

### 5.1 Two keys, not one

| policy | key | default | why separate |
|---|---|---|---|
| markdown: whole-file `Read`, and `cat`/`head`/`sed -n` | `worker.doc_nav_hook` | `off` | no flip blocker of its own |
| `.py`: whole-file or large `Read` | `worker.py_nav_hook` (existing) | `off` | flip sequenced behind issue 936, kn-76f7d3f6 |

The `.py` `Read` arm goes INSIDE the existing `py_nav_decision` — same
`_PY_NAV_DENY` text, same `JARVIS_WO_ID` gate, same `.serena/project.yml` precondition —
and `py_nav_decision`'s `tool_name` test widens from `"Bash"` to `("Bash", "Read")` with
its Bash path byte-identical. Putting it behind the new key would make one flip change two
policies whose blockers differ.

### 5.2 `doc_nav_decision`

```python
def doc_nav_decision(payload, env) -> dict[str, Any] | None: ...
```

THE LINE LIMIT IS A CATALOG SETTING AND NEVER A MODULE CONSTANT. `worker.doc_read_limit_lines`,
resolvable fleet-wide and per project, enumerated in §5.3 exactly as the switch is. The
standing rulings on `fleet_health.*` and on the backstop ceiling are unambiguous on this,
and the user's own scope text says "over a configurable line/token threshold" — a
conservative default does not earn an exception. `catalog.DEFAULT_WORKER_DOC_READ_LIMIT_LINES`
is the FALLBACK VALUE of that key (200) and is not the resolved one: the hook reads the
resolved number out of the environment, and a project that sets its own gets its own.

Gated on `JARVIS_DOC_NAV_HOOK == "on"` and on `JARVIS_WO_ID` being set, so an interactive
session in a managed project is untouched. NOT gated on `.serena/project.yml`: the
mitigation it names is a `jarvis` command, which every managed project has, unlike a
symbol index.

It denies when, and only when:

- `tool_name == "Read"`, `navigation.is_spec_path(file_path)`, and `tool_input` has no
  `limit` key, or a `limit` over `DEFAULT_DOC_READ_LIMIT`; or
- `tool_name == "Bash"` and `navigation.dumps_doc(command)` — but only for a command whose
  named `.md` paths are all `is_spec_path`.

A missing `limit` is whole-file: that is §1(a)'s finding and the reason this hook adds no
disk access to any `Read`. Verify the `Read` `tool_input` shape (`file_path`, `offset`,
`limit`) against the installed Claude Code rather than assuming it.

The deny text names §4.3's three commands and nothing else. It is the mitigation, so it
has to be actionable from the refusal alone.

### 5.3 Wiring

TWO KEYS, each enumerated in full. Neither is a module constant, and this list is the
checklist: a key that is missing one row of it is half-wired and the setting silently does
nothing.

**`worker.doc_nav_hook`** — the switch, `"off"` | `"on"`, default `"off"`:

| where | what |
|---|---|
| `catalog.py` | `DEFAULT_WORKER_DOC_NAV_HOOK = "off"` and `VALID_DOC_NAV_HOOK` |
| `catalog.py` | `WorkerDefaults.doc_nav_hook`, beside `py_nav_hook` at line 459 |
| `catalog.py` | `OsConfig.default_doc_nav_hook`, beside line 1403 |
| `catalog.py` | the `os.defaults` validation near line 2306 |
| `catalog.py` | the per-project validation near line 2355 |
| `dispatch.py` | writes `JARVIS_DOC_NAV_HOOK`, beside `JARVIS_PY_NAV_HOOK` at line 215 |
| `ops.py` | `("*.doc_nav_hook", "next-dispatch")`, beside line 10991 |

**`worker.doc_read_limit_lines`** — the threshold, an `int`, fallback 200. THE SAME SEVEN
ROWS, and not one fewer: `DEFAULT_WORKER_DOC_READ_LIMIT_LINES = 200`,
`WorkerDefaults.doc_read_limit_lines`, `OsConfig.default_doc_read_limit_lines`, both
validations (an `int` above zero; an invalid value names the key and says so), the
`dispatch.py` env write beside the switch, and `("*.doc_read_limit_lines",
"next-dispatch")` in `ops.py`. `WorkerDefaults.mcp_tool_timeout_ms` is the existing model
for a numeric worker key — follow its shape rather than inventing one.

Both validations must name the KEY in their message. A number resolved from a project's
catalog and a number hardcoded in `hooks.py` are indistinguishable in a passing test, so
the test that matters is the one that sets a per-project value and sees the hook honour it.

### 5.4 What this section must prove

The DONE WHEN's mechanical half, and it belongs in this PR because a refusal whose test
lands later is a refusal nobody verified: a whole-file `Read` of a spec is refused, a
40-line targeted read of the same file passes, a `grep -rn` over `docs/` passes, a
`sed -n '1,2000p'` of a spec is refused, and a `Read` of
`.jarvis/features/fo-x/sections/wo-y.md` passes at any size. All five with the key `on`;
all five pass with it `off`.

SIXTH, and it is the one that proves §5.3's second key is wired rather than declared: a
project whose catalog sets `worker.doc_read_limit_lines` to a value OTHER than 200 gets
that number honoured by `doc_nav_decision` — a `Read` with a `limit` between the project's
value and 200 flips its verdict when the key changes. A test that only exercises the
fallback cannot tell a resolved setting from a hardcoded one. Also pin
`_write_worker_settings` writing the resolved number, and both validations refusing a
non-integer and a zero while naming the key.

A fixture exercising any jarvis `PreToolUse` hook must create a `.jarvis/` directory at
its repo root — `hooks.find_project_root` resolves the project by it (kn-6c033672).
`tests/test_py_nav_hook.py`'s `repo` fixture is the model for the whole file.

### 5.5 The pins this section owes

REACHABILITY IS THE ONE THAT DECIDES WHETHER THIS SECTION DID ANYTHING. Every assertion
below goes through `hooks.preflight_decision`, not through the decision function:

- the Bash doc arm denies a `cd <repo> && sed -n '1,2000p' docs/specs/x.md`, which proves
  it sits before the `is_jarvis_command_chain` auto-allow.
  `tests/test_py_nav_hook.py::test_the_refusal_is_reached_before_the_jarvis_auto_allow` is
  the existing test of exactly this shape, and it exists because the arm after that allow
  is dead in production.
- the `Read` branch denies through `preflight_decision` with `tool_name == "Read"`. There
  is no `Read` branch there today, so a unit test on the decision function alone is green
  with the branch never wired in.
- `tests/test_py_nav_hook.py::test_py_nav_runs_after_the_investigator_refusal` pins that an
  investigator's refusal wins over a navigation one; the equivalent holds for the new arms.
- AST enclosure over `doc_nav_decision`: it CALLS `is_spec_path` and `dumps_doc`, and no
  local `startswith`, `"docs" in` literal or `re.compile` stands in for either.
- AST enclosure over both `doc_nav_decision` and `py_nav_decision`: `_allow` is called in
  neither. Today that contract is only a docstring claim.
- No import of `spec_index` or `catalog` anywhere in `hooks.py`, and §4.3's three command
  strings are string LITERALS there. `tests/test_concision_mechanisms.py` is the
  existing AST-over-imports model — find it as the test that asserts no module ending in
  `catalog` is imported by `concision`.
- `py_nav_decision`'s Bash path is byte-identical: every existing parametrisation in
  `tests/test_py_nav_hook.py` returns what it returns now, and `navigates_source` is still
  called with `SOURCE_SUFFIXES` by identity.
- `_write_worker_settings` writes `JARVIS_DOC_NAV_HOOK == "off"`
  (`tests/test_py_nav_hook.py::test_the_key_ships_off` is the model). This is the single
  best anti-flip pin and
  the reason the env write belongs to this section rather than to §6.
- Every suite that routes through `preflight_decision` stays green:
  `tests/test_py_nav_hook.py`, `test_gate_enforcement.py`, `test_background_refusal.py`,
  `test_heredoc_writes.py`, `test_spec_shape.py`.

There is NO end-to-end fleet evidence available in this section, by design: everything
ships `off`. The paired assertion — all five §5.4 cases deny with the key `on`, all five
return `None` with it `off` or absent — is the whole of the proof. Do not hunt for a live
one.

---

## 6. What an agent is told

Owner of `dispatch.py`'s spec prose, `worker_brief.py`'s navigation posture and
`concision.py`'s subagent injection. This is the half that changes the FIRST call, which
is the half a `PreToolUse` refusal provably cannot reach: kn-76f7d3f6 measured that with
Serena's tools present the first code-navigation call was a symbol call 0/3 with
`py_nav_hook` off and 0/2 with it on. A hook redirects the second call; the prompt decides
the first.

Three edits:

**`dispatch.py:464-467`** says today: *"The whole spec is at {design_doc['path']} if the
section is not enough."* That sentence is the instruction that produces a 5,187-token
read. It becomes a pointer to `jarvis spec toc <path>` and `jarvis spec search`, naming
the file as the argument to a command rather than as a thing to open.
`dispatch._planner_prompt` and `_common_briefing` get the same treatment: the planner is
the heaviest spec reader in the fleet and already receives the full navigation section
inline, so it is the cheapest place to win.

**`worker_brief.navigation_core`** gains the markdown half of the posture beside the
symbol half. NOT in `core_contract`: `CORE_BUDGET_CHARS` is a tested budget with 38 chars
of headroom and the core is A/B-graded as a unit by
`evals/llm/test_worker_contract_ab.py`, which is exactly why `navigation_core` is composed
outside it. If a new `jarvis brief` section is added instead, `SECTION_HOOKS`,
`section_names` and `section_index` must all agree or the index advertises a section
`render_section` refuses.

**`concision.subagent_context`** injects the house style and the project's standing
instructions and NO navigation posture at all. A Task subagent inherits CLAUDE.md,
skills, settings and hooks but none of its parent's `--append-system-prompt`, `--agent`
persona or `SessionStart` `additionalContext` (measured on 2.1.278). So the subagents
doing §1(b)'s whole-spec reads have never been told anything about navigation by any
mechanism — that, and not the worker brief, is the cause. Keep the added block to a few
lines: it is injected on every `SubagentStart` in every project and is pure overhead for a
subagent that never opens a spec.

### 6.1 The pins this section owes, and the two tests that go red

TWO tests in `tests/test_concision_mechanisms.py` assert
`subagent_context({}) == concision.house_style()` — find them by that equality, not by
line. Adding the navigation block breaks both
equalities, and that is the signal the hardest half was actually done. REWRITE them to
assert containment plus the navigation substring; do not delete them, and leave the
AST-over-imports assertion in the same test intact. A worker that edits only `dispatch.py`
and `worker_brief.py` has shipped the prose to the one seat that already had it and nothing
to the seats that never did.

- `tests/test_question_diet.py::test_children_of_a_design_doc_plan_get_the_doc_materialised`
  stays green UNMODIFIED, including its `str(section) in child_prompt` assertion and its
  assertion that the section TEXT is not pasted into the prompt. The snapshot path stays in
  the prompt — as a command argument, not as a thing to open.
- The prompt no longer contains `The whole spec is at` or `if the section is not enough`,
  and still contains the materialised section path with `read it first`.
- The three command strings in the prompt are byte-identical to §4.3's, and
  `hooks.is_jarvis_command_chain("jarvis spec toc docs/specs/x.md")` is `True`. Prose
  naming `jarvis spec show` passes any substring test the worker writes for itself and is
  dead in the field.
- `_planner_prompt` and `_common_briefing` are asserted too, not only the child path.
- Decide and PIN whether the markdown half is emitted when `serena=False`. Markdown
  navigation needs no symbol index, so swallowing it inside the Serena branch is a defect —
  but `tests/test_worker_brief.py`'s navigation-block family asserts the whole block is
  absent there. One of those has to change deliberately.
- `tests/test_worker_brief.py`'s `core_contract` budget test, `tests/test_prompt_ceiling.py`,
  `tests/test_stable_prefix.py` and the `tests/test_question_diet.py` test asserting a
  non-feature work order's prompt has no spec heading at all all stay green.

PRECONDITION this section's worker must check first: `jarvis spec --help` exits 0 on its
branch. `waiting_pr_merge` is not merged, and prose naming a command that does not exist
points every feature child at an error.

This section's done condition is TEXTUAL by necessity. kn-76f7d3f6's measurement is the
evidence that the prompt decides the first call, but no test here proves the prose caused a
behaviour. §7 is not this section's proof.

---

## 7. The behavioural eval

Owner of `evals/llm/`. The DONE WHEN's other half: a fresh feature child that needs spec
context beyond its given section uses `jarvis spec toc`/`section`/`search` and makes no
whole-file `Read`, `cat` or `sed` of a spec.

`evals/llm/test_navigation_judgment.py` already has the harness: a `PreToolUse` recorder
that attributes each call to the seat that made it, because the payload carries
`agent_type` for a subagent's calls and no such key for the lead's own. Reuse that
module's recorder and its arm-matrix shape rather than writing a second one — the eval
that grades TOOL CALLS rather than prose is the only kind that catches a rewording
regression.

Reuse by name: `RECORDER`, `run_and_record` (and its reasons for `permission_mode="auto"`
and for importing `bash_first_env`/`tool_search_env`/`serena_allow_rules` rather than
retyping them), `seat_calls` (lead-vs-subagent attribution by the ABSENCE of `agent_type`),
`ACTIVATION`, `STEER_ARMS`' arm-matrix shape, `first_navigation_call` (an ORDER assertion,
not set membership), the module-scoped `repo` fixture and its reason for living OUTSIDE
this checkout — a fixture inside it makes the subject read this repo's CLAUDE.md, which
already preaches navigation, and the eval then grades that file.

### 7.1 "Makes no whole-file read" is satisfied by reading nothing

That is the vacuity, and it is why the clause splits into a positive and a negative over
the SAME run:

**POSITIVE — a canary, never a tool-name count.** The fixture plants a
`docs/specs/<name>.md` of ~10 numbered sections with a unique token (e.g.
`GREEN-OTTER-41`) in a section that is NOT the child's assigned one, and the assigned
section at `.jarvis/features/fo-x/sections/wo-y.md` says nothing about it. The question
cannot be answered without the token. Assert BOTH that the answer contains the token (the
information travelled) AND that the log holds at least one `Bash` whose command starts
`jarvis spec ` (it travelled by the intended route). Counting `jarvis spec` calls alone is
passable by a model that runs `toc` and then `cat`s the file anyway; the canary alone is
passable by a dump. Together they are not.

**NEGATIVE.** Re-apply the shipped `hooks.doc_nav_decision` in Python to every recorded
`Read` and `Bash` payload and assert nothing in the log would have been denied — §2.5's
third idiom, because the recorder sees the attempt and not the verdict.

### 7.2 Two negative controls, and they are different

- **The child's own section.** A `Read` of `.jarvis/features/fo-x/sections/wo-y.md` with
  no `limit` is in the log and `doc_nav_decision` returns `None` for it. This proves the
  path the fixture actually built is the path `is_spec_path` sees.
- **Markdown text search stays legal.** Same shape as
  `test_a_genuine_text_question_may_still_use_text_search`: a literal-word question over
  the docs tree, answered, with no `grep` refused by the re-applied predicate.

### 7.3 What makes it non-flaky

- **`jarvis` on `PATH` is a PRECONDITION ASSERTION, not an assumption.**
  `bootstrap.jarvis_hook_command()` falls back to `sys.executable -m jarvis.cli _hook`
  when `shutil.which("jarvis")` is None — so the hook works while the mitigation its deny
  text names does not exist for the subject, and the eval measures nothing and PASSES.
  Write a `jarvis` shim into the fixture repo, prepend its directory to `PATH` in the
  settings `env`, and before grading run `jarvis spec toc <fixture spec>` from the fixture
  cwd and assert exit 0. If that fails the eval FAILS; it does not skip.
- **Arm discipline.** Assert hard on the `doc_nav_hook="on"` arm, where the refusal
  mechanically forces the recovery. Record the `off` arm with `xfail(strict=False)` and a
  reason string in `STEER_ARMS`' idiom saying it is recorded, not asserted — it is the
  evidence a later order flips the key with. That keeps CI off a model's coin flip while
  still producing the before/after pair. Add `doc_nav_hook` as a dimension of the arm
  tuple rather than hardcoding it.
- **Every assertion is over tool calls and the canary string, never over the model's prose
  about navigation.** That module's own docstring is the argument.
- Extend `tests/test_navigation.py::test_the_eval_uses_the_shipped_classifier` so
  the new eval imports `DOC_SUFFIXES`, `dumps_doc` and `is_spec_path` BY IDENTITY and keeps
  no retyped copy. That one is a `tests/` test, so it runs in CI without
  `JARVIS_EVALS_LLM` — the only part of this section that is cheap and always green.

If this section finds a defect in a sibling's file, it REPORTS it. It does not patch it.

---

## 8. Out of scope, filed

- `.png` reads: 8.04M tokens, the largest single `Read` cost in the fleet. Dashboard
  screenshots, not file dumps; no table of contents helps.
- Flipping `worker.doc_nav_hook` on. §7 produces the evidence; a later order decides.
- `sed … .py` at 8.72M tokens: already `py_nav_decision`'s territory, flip sequenced
  behind issue 936.
- A graph or vector index over the docs. Deferred until the counters of §3 show spec reads
  still dominating after this ships.

---

## Agent profile

You are a Jarvis OS engineer working on how agents READ — the classifiers that say what a
file is, the commands that let an agent open a part of one, the `PreToolUse` refusals that
stop it opening the whole thing, and the prose that makes it want to. You work in the dev
checkout of `~/workspace/agentic_os`, in your own git worktree, and you finish with a pull
request against `main`.

**What you must know about this codebase.**

Jarvis is a stdlib-only Python package in `src/jarvis/`, layered strictly downward: leaves
(`navigation.py`, `sections.py`, `paths.py`) import nothing from `jarvis`; stores import
`db` and `paths`; adapters sit above them; `ops.py` holds the logic the CLI and the
dashboard share; `cli.py` and `daemon.py` are the top. `hooks.py` is the `jarvis _hook`
endpoint and is executed on every tool call a managed worker makes — an import added there
is a cost the whole fleet pays per command, so it imports `navigation` and little else.

The architecture is already mapped. Run `list_memories` and read `codebase-map`,
`work-order-lifecycle` and `testing` before you explore; use Serena's `find_symbol`,
`get_symbols_overview` and `find_referencing_symbols` rather than grep for symbols, and
keep text search for genuine text questions. Do not spawn a subagent to rediscover the
architecture.

**Conventions you follow.**

A module docstring states the DESIGN RATIONALE, not what the code does, and cites the spec
section it implements. A `#:` comment above a constant says why that value and not another
one. A function that two readers with opposite error costs will call says so in its
docstring. Comments in this tree carry rulings — when you find one, it is load-bearing, and
when you make a decision a later reader could undo by accident, you write the reason down
in the same style.

Tests are per-feature files under `tests/`, named for the thing they pin
(`tests/test_py_nav_hook.py` is the model for a hook). Write the failing test first. Run
only the TARGETED tests for what you changed — never the full suite locally: it takes ~21
minutes, the prompt cache TTL is 5 minutes, so every full run guarantees your whole
conversation is re-sent at the cache-write rate, and CI already runs `pytest tests -q` on
three Python versions plus `pytest evals -q`. Cite CI for the suite. `pytest addopts` is
already `-q`, so adding your own makes it `-qq` and prints no summary line at all.

Any fixture that exercises a jarvis `PreToolUse` hook must create a `.jarvis/` directory
at its repo root: `hooks.find_project_root` resolves the project by it.

**The traps on this feature specifically.**

`navigation.navigates_source` is read by measurement code and by an enforcement hook, and
the fleet's published baseline was measured with its current meaning. You never edit its
body and you never change what an existing counter means; a doc classifier is a NEW
sibling function and an enforcement-only narrowing is a default-off keyword argument at
the hook's call site. Two spellings of one predicate is the failure mode this tree has
already paid for twice — a copied body passes equality and then drifts.

Inside `hooks.preflight_decision`, position is enforcement. A Bash arm placed after the
`is_jarvis_command_chain` auto-allow is unreachable in production however green its unit
test. A navigation decision function has NO `_allow` branch, ever: it denies or it returns
`None`, so it can never hand out something a gate would have caught.

A refusal costs a worker a whole turn, so a hook that fires on a legitimate call is worse
than the cost it saves. Markdown text search — `grep`, `rg`, `find` — stays legal without
exception. A small targeted read stays legal. And the one file a feature child is told to
read first, its own materialised spec section, stays readable at any size.

**What you never do.** You do not add a dependency; this package is stdlib-only by
decision. You do not flip a new catalog key on in the order that builds it — it ships
`off` and an eval produces the evidence a later order flips it with. You do not reach for
`ops` or `project_store` from a declared leaf module. You do not re-implement a matcher
that already exists one import away.

Your prose everywhere — commit messages, PR bodies, the work-order record, comments — is
compressed and lead-with-the-answer: no preamble, no recap, each thing said once, and
error strings, numbers and commands verbatim.
