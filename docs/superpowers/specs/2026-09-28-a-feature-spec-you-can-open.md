# A feature spec you can open

Work order wo-ea2bc1d7, GitHub issue #826. Neo question 907 ruled the renderer: **no new
dependency**. §2 restates that ruling and is not open.

## The problem

**The spec is the feature's one artifact, and no surface can show it.** The plan review
judges against it, every child's brief is only the margin around one of its sections, every
validation round quotes it — and the dashboard renders its PATH as text. The document the
OS is holding, in `plan["design_doc_content"]`, reaches no page and no terminal.

Live case: planner **wo-1170d758** of **fo-69ba1cc4**. `design_doc` is
`docs/specs/2026-09-27-self-evolution.md`. `pr_url` is None, so that path exists in exactly
two places — the planner's worktree, and the plan snapshot in the project database. A user
reading the feature order page is shown a repo path that does not resolve in their
checkout.

1. **No route renders a spec at all.** `src/jarvis/ui/app.py` serves `/`, `/search`,
   `/project`, `/fo`, `/io`, `/wo`, `/wo/debug`, `/cost`, `/inbox`, `/backlog`,
   `/knowledge`, `/neo`, `/gates`, `/config`, `/alarms`. Nothing else.
2. **The planner's page — the one that WROTE the spec — says nothing.**
   `src/jarvis/ui/templates/work_order.html:107-111` renders its spec line under
   `{% if spec and spec.section %}`, and `spec` is `specs.spec_of(store, wo)`
   (`src/jarvis/ui/app.py:1143`), which returns `None` for `kind in ("planner",
   "manager")` by design — `src/jarvis/specs.py:170`. That early return is correct and
   stays: three readers (worker prompt, materialised section, panel packet) depend on
   "a planner has no section of its own".
3. **The feature order page prints a dead path.**
   `src/jarvis/ui/templates/feature_order.html:95-98` —
   `📄 Spec: <span class="mono">{{ fo.plan.design_doc }}</span>`. Plain text, and while the
   plan is under review that path is not on `main`.
4. **A child's line names a section it cannot take you to.** Same block at
   `work_order.html:107-111`: "Spec section 4 of docs/…". No link, no anchor, nothing to
   click.
5. **The CLI is no better.** `jarvis wo show` carries the bare `spec_section` COLUMN,
   smuggled in by `**wo` at `src/jarvis/cli.py:2674` — a number or a heading substring with
   no path, no provenance and no way to read the text. A planner's column is NULL, so it
   gets not even that. `jarvis fo show` prints `Design document: <path>`
   (`src/jarvis/plans.py:544-545`) and never the document.
6. **Which revision is on screen is unrecorded.** `landing.committed_text`
   (`src/jarvis/landing.py:324`) returns `(text, source)` with `source` human-readable —
   `branch wo-1170d758 @ 4f2a1c9` or `origin/main @ a1b2c3d`. Both sites that set the
   snapshot drop it into the Neo question and keep nothing: `src/jarvis/ops.py:7219`
   (`plan["design_doc_content"], source = found`) and `src/jarvis/ops.py:7335`
   (`plan["design_doc_content"] = text`). The plan therefore holds a document with no
   statement of which revision it is.

**Root cause: the spec was given a producer and three machine consumers and no human
reader.** Everything built in `2026-08-29-spec-driven-feature-orders.md` and
`2026-09-25-plan-review-reads-the-spec-the-os-holds.md` moves the text towards prompts and
packets. Nothing was ever pointed at a person. This is not a template bug — the page cannot
render what no route serves and no projection exposes.

## The fix

One route, one stdlib renderer, one anchor resolver, three links and one CLI verb. The
document rendered is always the one the OS HOLDS, so it works before any PR exists.

### 1. The route: `GET /spec/{project}/{fo_id}`

In `src/jarvis/ui/app.py`, beside `feature_order` (line 991), rendering a new
`src/jarvis/ui/templates/spec.html`.

**Keyed on the FEATURE, not on a work order.** The spec belongs to the feature; a planner,
a manager and nine children would otherwise give one document ten URLs, and an anchor is
only useful if everyone links to the same one. A child's page does not get its own spec
page — it gets a fragment into this one.

Body:

```python
path = ops.registered_project_paths()[project]      # KeyError -> error.html
store = ProjectStore(path)
plan = specs.plan_of(store, fo_id)                  # {} for missing or unplanned
```

`plan_of` already returns `{}` for a missing feature order and for one with no plan
(`src/jarvis/specs.py:150`), which is the whole degradation contract — `specs.py`'s module
docstring: everything here degrades to None or a no-op.

Three outcomes, all HTTP 200, none a 500:

| state | page |
|---|---|
| `design_doc_content` present | the rendered document, provenance line, links |
| plan present, no content | the path, and "this plan was stored before the OS snapshotted spec text" |
| `{}` — unplanned, still `planning`, or no such feature | the feature's status in words and a link back to `/fo/{project}/{fo_id}` |

An unknown project name is the existing `ops.OpsError` -> `error.html` path every other
route takes (`app.py:1002-1003`).

**Deep links are FRAGMENTS, resolved when the link is BUILT, not query parameters.** The
child page emits `/spec/{project}/{fo_id}#data-model`; the server does nothing with it. The
resolution — heading name or number to anchor — happens once, server-side, in §3, because
only the server holds the document. See "Rejected alternatives" for `?section=N`.

No cache, no clipping. `sections.clip_at_heading` exists for a prompt budget; a page whose
entire job is to be readable does not have one.

### 2. The renderer: `src/jarvis/ui/markdown.py`, escape-first, stdlib only

New module. No FastAPI, no Jinja, no I/O — pure functions over text, so it is unit-testable
without a client and callable from `ops` for the CLI.

Neo 907 is binding: **no new dependency.** Core is stdlib-only by design; the `ui` extra
carries fastapi, uvicorn, jinja2 and python-multipart and nothing else. kn-a1e8cb59 is the
other half of the argument — "markdown + a sanitiser is a dependency and a hope": a full
parser plus a bleach-style allow-list is two packages whose joint behaviour on
planner-model-authored bytes nobody in this repo can predict.

**THE ORDER IS THE SECURITY PROPERTY.** `html.escape(doc)` over the WHOLE document is step
one, before any tokenising. Every `<`, `>`, `&`, `"` and `'` in planner prose is inert
before a single rule runs. From then on the only markup in the output is markup this module
emitted. There is no sanitiser, nothing to bypass, and no allow-list to keep current.

```python
def render(doc: str) -> str: ...        # a safe HTML fragment
def slug(heading_text: str) -> str: ...
def anchors(doc: str) -> list[tuple[str, str]]: ...   # (heading text, slug), doc order
def anchor_for(doc: str, which: str) -> str | None: ...
```

The subset, and nothing outside it:

1. **Fenced code** — ``` ```lang ``` ... ``` ``` ``` -> `<pre><code>`. Its body is emitted
   verbatim (already escaped) with **no inline pass**: a `*` in a shell command is a glob.
   An unclosed fence runs to end of document rather than being dropped.
2. **ATX headings** — matched with `sections.HEADING_RE` (`src/jarvis/sections.py:36`) and
   no other pattern, so `anchors()` and `sections.extract_section` cannot disagree about
   what a heading is. Emitted as `<h{level} id="{slug}">`, levels clamped to h2–h6 (the
   page's own `<h1>` is the chrome's).
3. **Lists** — a line starting `- `, `* ` or `1. ` opens `<ul>`/`<ol>`; one level only.
   Nested items render as flat items. That is the stated subset boundary, not a bug to fix
   later.
4. **Inline**, in this order: `` `code` `` first (so `**` inside backticks stays literal),
   then `**bold**`, then `*em*` / `_em_`.
5. **Autolinks** — a bare `https://…` or `http://…` becomes `<a href>`. **Scheme
   allow-list of exactly those two**; anything else stays text. The URL is already escaped,
   so quote-breaking out of the attribute is impossible, and `javascript:` never reaches an
   `href`.
6. Everything else is a paragraph. Blank line separates.

The template renders `{{ body | safe }}`. That is the only `| safe` this change adds, and
the escape-first rule is what licenses it — say so in a comment at the call site.

#### The slug rule, exactly

Computed from the **raw** heading text (`HEADING_RE` group 2), which is the one place raw
bytes are read. Safe by WHITELIST, not by escaping — step 4 deletes every character that
could break an attribute:

1. strip trailing `#` and whitespace;
2. `unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()` — stdlib, so `§`
   and accents fold instead of vanishing into one long dash;
3. lowercase;
4. every run of characters outside `[a-z0-9]` becomes a single `-`;
5. strip leading and trailing `-`;
6. empty result becomes `section`.

**Collisions: first wins bare, the Nth duplicate gets `-{n-1}`** — `data-model`,
`data-model-1`, `data-model-2` — counted over headings in document order. GitHub's rule, so
a reader who knows GitHub anchors is not surprised, and §5's blob link lands on the same
place. One counter, inside `anchors()`; `render` and `anchor_for` both call it, so the id
written into the page and the id linked to cannot drift.

### 3. Resolving a child's `spec_section` to an anchor — the subtle part, specified once

`spec_section` is a NUMBER matched against numbered headings, or a NAME matched
case-insensitively as a SUBSTRING of the heading text. That rule lives in
`sections.extract_section` (`src/jarvis/sections.py:49-78`) and **may not be reimplemented**
— a second matcher is a child whose brief gets section 4 and whose link lands on section 5.

Split the existing loop out, no behaviour change:

```python
# src/jarvis/sections.py
def find_heading(markdown: str, which: str) -> int | None:
    """Index into HEADING_RE's matches for `which`, or None. The one matcher."""
```

`extract_section` becomes `find_heading` plus the slicing it already does.
`markdown.anchor_for(doc, which)` is `find_heading` plus `anchors(doc)[i][1]`. Same
`None` semantics throughout: unresolvable means no fragment, and the link still opens the
document at the top rather than not rendering.

### 4. Which version is on screen — persist it, never invent it

Both sites already have the string and throw it away. Set it in the SAME statement as the
content so the two cannot disagree:

- `src/jarvis/ops.py:7219` — `plan["design_doc_content"], plan["design_doc_source"] = found`
- `src/jarvis/ops.py:7335` — `plan["design_doc_content"], plan["design_doc_source"] = text, source`

The page prints it verbatim under the title: **`docs/specs/x.md — branch wo-1170d758 @
4f2a1c9`**. `landing.committed_text`'s strings are already human-readable and already say
which rung answered; re-wording them here would make the page and the Neo question describe
one snapshot two ways.

**A plan snapshotted before this ships has no key, and the page says exactly that:**
`the revision this text came from was not recorded`. It does NOT fall back to
`origin/main`, to the planner's branch, or to re-reading git — the stored text may predate
either, and a fabricated provenance line is worse than none on the one page whose purpose
is to show what the reviewer read.

### 5. The links

All four are `<a href="/spec/{{ project }}/{{ fo_id }}">`, fragment appended when §3
resolves one.

**(a) The planner's page — the case with nothing today.** `specs.spec_of` stays as it is.
Add a second, narrow projection next to it, in `specs.py` because that module owns what the
OS does with a spec:

```python
def spec_link(store: ProjectStore, wo: dict[str, Any]) -> dict[str, str] | None:
    """Where this order's spec can be READ. Any order with a parent — planner, manager
    or child. `anchor` is "" unless a child's `spec_section` resolves."""
```

Returns `{"fo_id", "repo_path", "source", "anchor"}`, or None for a standalone work order.
It does NOT carry `content` or `section_text`: the page is the pointer, not a second copy —
`app.py:1140-1142` already states that rule for `spec_of`. Passed to the template as
`spec_link` from `app.py:1143`'s block.

The planner block reads: *the spec this planner wrote*, path, provenance, link. The
existing child block at `work_order.html:107-111` keeps its wording and gains the href.

**(b) `feature_order.html:95-98`** — the `mono` path becomes the link. Sentence unchanged.

**(c) Each plan child row, `feature_order.html:107`** — `§ {{ c.spec_section }}` becomes a
link with that child's fragment, resolved by §3 against `fo.plan.design_doc_content`. Same
fact as (a)'s child case; one resolver, two call sites.

**(d) `jarvis wo show`** — §6.

### 6. The GitHub blob link, only when it can be derived

Once the spec is on the default branch, the page also offers *view on GitHub*. Built in
`src/jarvis/github.py` beside `origin_repo` (line 160), which already handles both remote
spellings:

```python
def blob_url(cwd: Path, repo_path: str) -> str | None:
```

`origin_repo(cwd)` for `(owner, repo)`; `landing.base_ref(cwd)` for the default branch,
`origin/` stripped; `landing.default_branch_head(cwd)` for the sha, and the URL is built on
the **sha** — `https://github.com/{owner}/{repo}/blob/{sha}/{repo_path}` — so it keeps
showing what the page showed after main moves. Local git only; it must NOT call
`landing._fetch_ref` — a page render may not touch the network.

**None at every unreadable step, and the link is then OMITTED rather than guessed.** No
origin, no default branch, no head sha, or a stored `design_doc_source` that names a branch
(the document is not on main yet) — no link. A 404 on GitHub from a URL the OS assembled
reads as the spec having been deleted.

### 7. CLI

**`jarvis fo spec <fo-id> [--section N] [--project P] [--json]`**, registered in
`src/jarvis/cli.py` beside `fo agent` (line 831), on `ops.feature_spec(fo_id, project_name,
section=None)`.

Returns `{"project", "fo_id", "repo_path", "source", "content"}`; with `--section`,
`content` is `sections.extract_section`'s output and the key `section` is added. Human
output is one header line — `docs/specs/x.md — branch wo-1170d758 @ 4f2a1c9` — then the
markdown raw to stdout. **The terminal gets markdown, not the HTML renderer**: `markdown.py`
serves the page.

`OpsError` with the state named when the feature has no plan yet, and when `--section` does
not resolve — the latter listing the headings that exist, from `anchors()`, which is the
same courtesy `plans.spec_problems` gives a planner.

**`jarvis wo show` gains a `spec` key**, on the never-always rule its neighbours state at
`cli.py:2712-2726` — absent, not empty, when there is nothing to say:

```python
**({"spec": link} if (link := specs.spec_link(store, wo)) else {}),
```

This fixes planners AND children: a child's `spec_section` rides in today only as a bare
column value via `**wo`. Human output names the document, its revision and
`jarvis fo spec <fo-id>` — **the command, not a URL**: the CLI does not know the
dashboard's host, and `jarvis search` already sets the precedent of printing the command
that shows a record.

## Tests

`tests/test_ui.py` for the route and the links, a new `tests/test_spec_page.py` for the
renderer and the slugs, `tests/test_feature_orders.py` for persistence and the CLI,
`tests/test_question_diet.py` for the `find_heading` split (it already owns
`test_extract_section_by_number_and_name`, line 244).

1. **The route renders a held spec with NO pull request** — `pr_url` None, plan carrying
   `design_doc_content`. The page contains a heading from the document. This is
   wo-1170d758's shape and the one that must not regress.
2. **Escape-first**: a spec whose text contains `<script>alert(1)</script>`,
   `<img src=x onerror=alert(1)>` and `[x](javascript:alert(1))` renders with no `<script`,
   no `onerror=` and no `javascript:` in an `href` — assert on the HTML string.
3. **Three links**: the planner's page, the feature page and the child's page each contain
   `/spec/{project}/{fo_id}`.
4. **The child's link carries its section's anchor**, and that exact `id=` is present in
   the rendered page. Assert the pair, not either half — a matching fragment pointing at no
   id is the bug this catches.
5. **Slug collisions**: two identical headings produce `x` and `x-1`, in document order,
   in both `render` and `anchor_for`.
6. **The CLI prints a section**: `jarvis fo spec <fo-id> --section 3` prints that section
   and not the whole document.
7. **Degradation**: a feature with no plan returns 200 and names its status; a plan with
   no `design_doc_source` renders the "not recorded" sentence and never a fabricated one.

Build fixtures through the real path — `ops.submit_plan` — not by writing a plan JSON into
the row. The provenance key is written by that function, and a hand-built plan would pass
test 7 against code that never persists it.

## Not covered

- **Any second renderer of spec text.** The worker prompt, the Neo question and the panel
  packet keep passing markdown; nothing about them changes.
- **Editing a spec from the page.** Read-only, and the spec is a committed file — the
  planner's session is what revises it.
- **Tables, block quotes, images, nested lists, HTML passthrough.** Outside the subset.
  `render` degrades them to paragraph text; it never fails on them.
- **Making `spec_of` answer for planners.** Deliberately untouched, §the problem, 2.
- **A spec page for an improvement order or a standalone work order.** Neither has a plan.
- **JavaScript.** The dashboard has none (`_validation.html:10-11`) and a markdown page is
  not what introduces it. Anchor navigation is the browser's.

## Rejected alternatives

**Add `markdown` (or `mistune`) plus `bleach`.** Refused by Neo 907. Two dependencies in
the `ui` extra, an allow-list that must be kept current against a parser's output, and
kn-a1e8cb59's point: a sanitiser is a hope about bytes a planner model wrote. Escape-first
has no bypass surface at all.

**Serve the raw markdown as `text/plain`.** A route and no renderer. It is also the answer
to the wrong question: the complaint in #826 is that the spec cannot be READ, and a 40KB
wall of `##` with no anchors cannot be deep-linked, which kills §5(c) outright.

**`?section=N`, a page showing one section.** Two addressings of one document, and they
drift: the fragment is built from `anchors()` while the query would be re-resolved per
request against a snapshot that `refresh_plan_spec` can change underneath it. It also needs
a second render path and gives a reader no way to see the section's surroundings — which is
exactly what a child worker's reviewer is looking for. `#anchor` costs the server nothing.

**Link the GitHub blob and skip the held document.** This is the live bug, not an
alternative: fo-69ba1cc4's spec is on no branch GitHub has seen. The held document is the
only copy that always exists, and it is the one the reviewer actually read.

**Resolve `spec_section` to an anchor in the template.** Jinja would need the matching rule
— number-or-substring, `sections.py:60-69` — spelled a second time in HTML, where it could
not be unit-tested and could not be reused by the CLI. §3 is the reason it is one function.

**Key the route on the work order (`/spec/wo/{wo_id}`).** One document, N URLs, N sets of
anchors; the feature page and a child page would link to different addresses for the same
text, and nothing could say "this is the spec" in one place.
