# A console that uses the screen, and that you can move through

*2026-10-22*

## Problem

Two complaints from the user, on a 34" ultrawide (3440px):

1. **The width is wasted.** Every page lives in a `max-width: 1080px` column, so two
   thirds of the screen is empty and every page is a single long stack.
2. **Getting to a thing means scrolling to it.** `/wo/<p>/<id>/debug` is five blocks,
   one under the other, with turn-by-turn spans and per-turn context tables that run for
   screens; there is no deep link into any of it, and nothing on one page links to the
   page you want next. The user's own example of a path they cannot walk today:

   > dashboard → a work order → debug → how long it waited for Neo → that Neo question →
   > the PR the question came from → back to debug → context-window fill → the project's
   > config → the worker prompt for that project.

## What changes

### 1. The shell (base.html) — every page

- **Fluid width.** The 1080px cap goes. A left **sidebar** holds the navigation (same
  links, grouped), the instance/daemon chips, the registered **projects**, and the
  viewer's **recent pages**. The top bar keeps search.
- **"On this page" rail.** Every `h2` on every page gets an id (its own, or a slug) and a
  `#` link on hover, so every section is deep-linkable. On screens ≥ 1400px a sticky rail
  on the right lists them with scroll-spy; below that it collapses into a "Sections"
  picker. Headings inside a closed tab still work: the existing tab script opens the
  panel that holds a hash target.
- **Every id is a link.** A client-side pass turns any bare `wo-`/`fo-`/`io-`/`inv-`/
  `kn-`/`al-`/`mv-` id in the page's text into a link to `/go/<id>`. Sentences stay the
  payload's (nothing is reworded, the server HTML is unchanged); they just become
  navigable.
- **`/go/<ref>`** — one resolver for every id the OS mints: orders (via `order_path`),
  knowledge entries, alarms, Neo questions (`q123`, `#123` is a tracker issue), gates
  (`gate-12`). Unknown → the search page with the text filled in.
- **Command palette** (`Ctrl/⌘-K`): pages, projects, this page's sections, recent pages,
  and "go to <id>" for anything id-shaped. `/` focuses search, `g` + letter jumps to a
  page (`g d` dashboard, `g n` neo, `g g` gates, `g a` alarms, `g i` inbox, `g c` config,
  `g $` cost, `g s` stuck, `g b` backlog, `g k` knowledge, `g m` morpheus, `g e`
  evolution), `?` lists them.
- **Layout utilities**: `.grid` (auto-fit columns of `--col` min width), `.span-all`,
  `.sec` (a section that is one grid cell), `.sticky-col`.

### 2. An order's own navigation

Every page about one work order — the record, debug, the worker prompt, the bill —
carries the same sub-nav: **record · debug (why / now / time / context) · prompt · cost ·
PR ↗ · project · project config · parent feature**. "Back to debugging" is one click from
anywhere in the order, and each debug block is a direct link.

### 3. The debug page

- Diagnosis, "clearing it" and the live snapshot sit side by side at the top; the record,
  holds and the way through in the next band; anatomy and context below.
- **Holds link to what held them.** `holds.Hold` carries the correlating key the timeline
  already pairs on (`neo_question_id`, `approval_id`, `round`, `msg_id`), and the debug
  row links it: a Neo hold → `/neo/question/<n>`, a gate → `/gates#gate-<n>`, a round →
  the order's `#round-<n>`, a message → `#msg-<n>`.
- **Turns fold.** Each turn in "turn by turn" and in the context ledger is a
  `<details id="turn-<seq>">` / `id="ctx-<seq>"`, open by default only for the last few,
  so the page is one screen of summaries; a hash opens the one you want. Each turn links
  to its context ledger row and back.

### 4. The worker prompt, on screen

- `/wo/<p>/<id>/prompt`: every prompt the OS sent this order's conversation, turn by
  turn (`wo_turns.prompt`, already recorded), each with an anchor.
- `/project/<p>/prompt`: the prompt a NEW work order on this project would be dispatched
  with today — `dispatch.build_worker_prompt` with a placeholder order and the project's
  live knowledge brief, plus the project's OPERATION.md. Linked from the project page,
  the config page's project scope and every order's sub-nav.

### 5. Page layouts at width

Dashboard: attention + inbox + open work | projects + new order + brake. Work order:
the summary card beside the asks (harvest, retry, assumptions, gates, re-judge,
review), tabs full width under them. Neo question: question | answer + deliberation,
with the originating order, its PR and its debug page linked at the top. Neo, gates,
alarms, stuck, evolution, bill, neo stats, cost, morpheus, feature/investigation order:
wrapped in `.flow`, a script turns each `h2` section into a grid cell. Project: work
orders | improvement/investigation/feature orders. Config: the tree is sticky, history |
diff side by side, and a project scope links its project and its worker prompt.

Improvement order (asked for by the session working on the findings' length): counts →
findings as folded cards (key, standing, symptom; pending ones open) with evidence and
proposed orders folded inside → beside them the brief (folded), the evidence refs as
links, and the analyst order with jumps to its asks, questions and debug page.

**Rule learned building it:** nothing may span an auto-fit grid. A `grid-column: 1/-1`
item keeps every empty track alive, so the cells stop stretching and the page is
half-empty again. Full-width blocks sit above or below a grid, never in it.

## Not changed

- No sentence on any page is reworded; the pages still print what the payloads say.
- No route changes shape; old URLs and anchors still resolve.
- No build step, no framework: inline CSS/JS in `base.html` as before.

## Assumptions (for review)

1. **Fluid, not capped.** Pages use the full width; long-form text blocks (prompts,
   descriptions, specs) keep a readable measure (~120ch) inside their cell.
2. **Breakpoints:** sidebar collapses to a top strip below 1100px; the section rail shows
   from 1400px; three-column bands appear from ~1900px. Tuned for 3440×1440 and
   1920×1080, sanity-checked at phone width.
3. **Recent pages live in `localStorage`** — a per-browser convenience, not OS state; it
   is wrapped in try/catch and the page works without it.
4. **Autolinking is client-side** so no template has to change its wording, and so it
   reaches every payload sentence. It skips `a`, `textarea`, `input`, `pre`, `script`,
   `style` and buttons. An id that no longer resolves lands on search, not a 404.
5. **`/go/<id>` walks every registered project** (same cost as `find_work_order` today).
6. **The project prompt preview is a preview**: a placeholder order, no feature or spec
   section — the real per-order prompt is on `/wo/<p>/<id>/prompt`.
7. **Turn folding:** on the debug page the last 3 turns (anatomy and context ledger) are
   open, the rest folded — so the page opens on what is happening now. On the prompts
   page the first prompt (the brief) and the last are open.
8. **Keyboard shortcuts** are ignored while typing in a field.
9. **Holds gained a `key`** (`holds.Hold.key`, `compare=False`), and `jarvis wo why
   --json` now carries `key` on each hold episode. Equality and every duration are
   unchanged.
10. **`dispatch.knowledge_for`** was extracted from `dispatch_work_order` so the prompt
    preview and dispatch build the knowledge brief with one call. Behaviour unchanged.
11. **Not done:** order pages still light "dashboard" in the sidebar (the route's
    `active` default, as the old header did); the order sub-nav and the project entry
    carry the "where am I" instead.
