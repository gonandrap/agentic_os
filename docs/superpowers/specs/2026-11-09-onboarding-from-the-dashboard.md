# Onboarding from the dashboard: every section at once, jump anywhere, help on demand

Feature order `fo-dc277eea`. The user wants to onboard `~/workspace/auto_heycrypto`
(production copy at `~/workspace/heycrypto-live`). Today's path, `jarvis onboard start`, is
a linear `greeting_agent` interview: identity, 27 behaviour areas, a reason — about 29
turns, in a fixed order. The ask:

1. Show **every section at once**, each pre-filled with its effective value (draft, else
   live catalog, else shipped default), and let the user **jump between them** at will.
2. Let the user **start from another project's configuration** ("mimic").
3. At any moment, a **help** action opens a NEW Claude session, run by a NEW agent type
   built for onboarding help, whose opening prompt is built from the exact view the user
   is looking at.
4. **Navigation between configuration properties is the guiding principle.** When two
   designs tie, pick the one that gets the user to a setting in fewer moves and tells
   them where they are.

---

## 0. What already exists, and what this feature freezes

Verified in the worktree; do not re-derive it.

- **The interview backend is landed and pure.** `src/jarvis/interview.py`: `new_draft`,
  `questions`, `answer(draft, area, "preset:<p>"|"defaults", fleet=…)`, `set_value`,
  `accept_defaults`, `accept_remaining`, `set_reason`, `fleet_warning`, `wiring_choices`,
  `wire`, `summary`, `match_posture`, `draft_file(name)` (=
  `paths.onboarding_dir()/<name>.json`). Every mutating verb ends in `_commit`, which
  validates (fleet-warning rule, OS-project sweep rule, `onboard_draft.validate`) and on
  refusal restores the in-memory draft and saves NOTHING, raising `InterviewError`.
- **The draft file** is frozen by `docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md`
  §0.3 (`schema`, `project{name,path,description,model}`, `reason`, `values`, `answers`).
  This feature adds NO top-level key. Provenance goes in `answers` as an extra
  `{"area": …, "mode": …}` entry, the precedent being `interview.FLEET_WARNED`; §3 of that
  spec (apply) ignores `answers`.
- **The registry**: `onboard_knobs.AREA_KEYS` (27 areas, registry order),
  `onboard_knobs.area_of(path) -> (area, tail) | None`, `asked(path)`;
  `onboard_prose.AREAS` carries each area's `title`, `scope` (`project`|`os`|`both`),
  `must_have`, `summary`, `on_costs`, `off_costs`, `presets`. `ops.onboard_questions()`
  shows them; `ops.onboard_explain(area|path)` grounds them.
- **The one write**: `ops.onboard_apply(draft_path, dry_run=…)` — one config version, then
  bootstrap, then `CentralStore.upsert_project`. It does NOT delete the draft. For a NEW
  project it returns `restart_needed` with `restart_why`: `Daemon.reload_catalog` refuses a
  changed project-name set, so the project does not exist for the daemon until the user
  runs `jarvis start` (learning `kn-bbe46570`). The dashboard never restarts anything.
- **The launcher**: `ops.onboard_session` / `launch_argv` / `GREETING_AGENT`; persona
  assets under `src/jarvis/assets/greeting-agent/`, installed by
  `bootstrap._rebuild(bootstrap.ASSETS / "greeting-agent", paths.onboarding_dir() / "agent", "agents")`.
  `claude_cli.spawn_background(prompt, cwd, name, agent=…, add_dirs=…)` runs `claude --bg`
  and returns the short job id if the CLI printed one; it has NO production caller today.
  `claude attach <id>` attaches a terminal to a background session; `claude agents` lists them.
- **Dashboard**: `src/jarvis/ui/app.py`, one `create_app()` closure. Module-level helpers
  `config_widget(value)` (app.py:982), `config_value_of(current, text)` (:1002),
  `config_type_ok(current, new)` (:1011) type a form string against a current value.
  `notify.ui_base_url(catalog)` is the dashboard's own base URL.

### 0.1 Non-goals

No change to the draft schema, to `onboard_apply`'s semantics, to `/config`, to the daemon,
to `hooks.py`, to `WO_KINDS` or `dispatch.py`. No hot-add of a project. No tabs on any
onboarding page (Playwright's `inner_text` cannot see a hidden panel — `kn-6aa0e6d8`). The
help session never writes anything (§4). Renaming a project inside a draft is not
supported: the name is the draft's key, so a different name is a different draft.

### 0.2 Section keys and their order — frozen

A **section** is one screen of the workspace. Keys, in rail order:

1. `identity` — group `Project`.
2. Every `AREA_KEYS` key whose area has `must_have: true`, registry order — group
   `Decide first` (today: evolution, validation, neo, rules, supervisor, investigation,
   morpheus, steward, gates, budgets, compaction, model — including the `os`-scoped ones).
3. Every other area with scope `project` or `both`, registry order — group `This project`
   (`wiring` is one of these; its knobs are levers, §2).
4. Every other area with scope `os`, registry order — group `Whole fleet`.
5. `review` — group `Finish`.

Each key appears once, in the first group it matches. A section key is exactly the area
key, verbatim — no prefix, no slug. Compute the groups from the registry; never hard-code
the list above.

### 0.3 URL and form contract — frozen

All GET query params are `str` (learning `kn-34d78a6e`).

| Route | Owner | Does |
|---|---|---|
| `GET /onboard` | §6 | drafts list + "onboard a project" form |
| `POST /onboard` | §6 | form `path`, `name`, `like`, `fresh` → `ops.onboard_open` → 303 `/onboard/{name}` |
| `GET /onboard/{name}` | §3 | 303 to the first section needing the user (first unanswered must-have, else first unanswered, else `review`) |
| `GET /onboard/{name}/review` | §5 | the review section |
| `GET /onboard/{name}/{section}?focus=<path>` | §3 | one section; unknown section → 404 page |
| `POST /onboard/{name}/edit` | §3 | form `verb, section, rev, path, value, mode, field, lever, wired, fleet` → `ops.onboard_edit` → 303 (see "Where an edit lands" below); on refusal re-render the section, status 400, the error shown and the typed value kept |
| `POST /onboard/{name}/apply` | §5 | form `rev`, `reason` → `ops.onboard_apply` |
| `POST /onboard/{name}/help` | §4 | form `section`, `focus` (knob path or empty) → `ops.onboard_help` → its own result page |

`<slug>` is the knob path with every non-`[A-Za-z0-9_-]` character replaced by `-`.

**Where an edit lands** (amended by io-74eab254, wo-917e2120). A section-level verb
(`answer` = a posture, `defaults`, `confirm` = keep these) answers the area, so it 303s
to the first section still needing the user (the `GET /onboard/{name}` rule, else
`review`) with `?note=saved: <section title> — <what>`, shown by `base.html`'s note
flash. `fleet_ack` is the exception: the ack unlocks editing THIS section, so it stays
on it with the same flash. A per-knob verb (`set`, `unset`, `identity`, `wire`) stays on
its row: `/onboard/{name}/{section}?focus=<path>&saved=1#k-<slug>`, the row marked
`saved`. Any edit to a copied draft (`project.like` set) returns to `review` with the
flash (§1 copy flow). A refusal is unchanged: 400, same section, typed value kept.

**Route modules.** Each UI child puts its routes in its own module under
`src/jarvis/ui/` exposing `register(app, …)` and adds ONE call to it inside
`create_app()`: §3 `ui/onboard_pages.py`, §4 `ui/onboard_help.py`, §5
`ui/onboard_review.py`, §6 `ui/onboard_index.py`. Because Starlette matches routes in
registration order, `onboard_review.register` and `onboard_index.register` are called
BEFORE `onboard_pages.register` (§3's `{section}` route would otherwise swallow `review`).
A route module imports `ui.app` helpers lazily inside the handler — `app.py` imports the
module, so a module-level import back is a cycle. Templates are `templates/onboard_*.html`
plus partials `templates/_onboard_*.html`; §3 owns `_onboard_rail.html` and
`onboard_section.html`, which §5 extends or includes rather than copies.

### 0.4 `ops` surface — frozen

```python
ops.onboard_open(path, name=None, *, like=None, fresh=False, catalog_path=None) -> dict   # §1
ops.onboard_drafts(*, catalog_path=None) -> list[dict]                                     # §2
ops.onboard_view(name, *, catalog_path=None) -> dict                                       # §2
ops.onboard_edit(name, verb, *, rev, catalog_path=None, **fields) -> dict                  # §2
ops.onboard_help(name, section, focus="", *, catalog_path=None) -> dict                    # §4
```

Every failure is `OpsError` (an `InterviewError` / `DraftError` is re-raised as `OpsError`
with its text intact). `onboard_open` returns `onboard_view`'s dict.

The **view model** — what `onboard_view` returns, and what §3, §4, §5 read:

```json
{
  "project": {"name": "auto_heycrypto", "path": "/home/…/auto_heycrypto",
              "description": "", "model": "", "adopted": false, "like": "heycrypto"},
  "draft_path": "/home/…/.jarvis/onboarding/auto_heycrypto.json",
  "rev": "sha256 of the draft file's bytes",
  "reason": "",
  "others": ["jarvis_os", "shared_schedule"],
  "fleet_warning": "An os.* setting is the whole fleet's: …",
  "fleet_acked": false,
  "progress": {"answered": 3, "total": 27, "must_have_missing": ["validation", "gates"]},
  "problems": [],
  "sections": [
    {"key": "validation", "group": "Decide first", "title": "…", "scope": "project",
     "must_have": true, "status": "untouched",
     "summary": "…", "on_costs": "…", "off_costs": "…",
     "postures": {"balanced": {"projects.auto_heycrypto.validation.enabled": true}},
     "changed": 0,
     "knobs": [{"path": "projects.auto_heycrypto.validation.enabled",
                "tail": "validation.enabled", "kind": "asked",
                "line": "…", "example": "…",
                "value": true, "default": false, "source": "draft",
                "safety": true, "apply": "next-dispatch"}]}
  ],
  "review": {"rows": [], "must_have_missing": ["validation"], "reason_needed": true}
}
```

- `status`: `untouched` | `defaults` | `custom` | `preset:<posture>` — the area's `answers`
  mode, `untouched` when absent. `identity` and `review` are `untouched` until edited.
- `source`: `draft` (the path is in `draft.values`) | `catalog` (the live catalog document
  sets it — a re-run project, or a fleet `os.*` value) | `default` (the shipped default).
- `value` is the effective value: `resolve(candidate_document(draft, document))[path]`.
  `default` is the shipped default from a blank one-project catalog.
- `kind`: `asked` knobs first, then `classified`, both in tail order.
- `fleet_warning` is `null` when the catalog holds no other project.
- `review.rows` is `interview.summary(draft, document, lines=…)`.
- `problems` is `onboard_draft.validate(draft, document)`; a draft that does not validate
  still renders.

### 0.5 Three traps every section inherits

1. **`interview.fleet_warning` is the ack, not a getter**: it appends `FLEET_WARNED` and
   saves. Nothing that renders may call it; only the explicit `fleet_ack` verb (§2) does.
   Calling it from the view silently lifts `_commit`'s fleet guard.
2. **The draft has no lock.** UI, CLI greeting session and anything else are
   load–mutate–save, last writer wins. So every edit carries the `rev` it was rendered
   from and `onboard_edit` refuses a stale `rev` with "the draft changed since this page
   loaded — reload it". The help session is read-only for the same reason.
3. **Worker lockout in tests**: `ops._refuse_worker_write` reads `JARVIS_WO_ID`, which a
   worker's test run inherits. Every test file that writes a draft or config carries the
   autouse fixture from `tests/test_config_console.py` (`not_a_worker`) plus one line
   asserting `os.environ.get("JARVIS_WO_ID") is None` (learning `kn-650b6f24`). Run only
   the tests you touched (`kn-356c724b`).

---

## 1. Opening a draft, and mimicking another project

**Owns:** `ops.onboard_open`, `interview.like_draft`, the `--like` flag on
`jarvis onboard start`, and the line for it in `docs/os/operator/onboarding.md`.

`onboard_open(path, name=None, *, like=None, fresh=False)`:

- Refuses a path that is not a git repository (same message as `onboard_session`).
  `name` defaults to the directory name and must pass the catalog's own project-name
  validation (`parse_catalog` is the type system — build the candidate and let it refuse).
- **An existing draft is resumed, never overwritten**, unless `fresh=True` — or unless the
  project is already in the catalog and the head config version
  (`CentralStore.head_config_version()`) is newer than the draft file. That draft predates
  the live settings (typically: it was applied, then `jarvis config set` moved something),
  and applying it would silently revert them; rebuild it from live and say so — the
  returned dict carries `"rebuilt": "<why>"`, which §6's page and the CLI print. With
  `like` set and a draft already present and `fresh=False`, refuse:
  "a draft for <name> already exists — continue it, or start over with fresh".
- No draft (or `fresh`): adopted project → `interview.new_draft(…, current=…, areas=…)`
  exactly as `onboard_session` does today; `like` set → `like_draft`; else
  `new_draft(path, name)`.
- Returns `onboard_view(name)` (lazy lookup; §2 builds it in parallel, so it does not
  exist when this section is written). This section's tests monkeypatch
  `ops.onboard_view` with a stub and assert on the draft FILE; §6 is the integration check.

`interview.like_draft(path, name, source, *, document, areas) -> Draft`:

- `source` must be a project in `document`; else `InterviewError` naming the projects that exist.
- Copies only what the source's catalog entry **sets** (`ops._written_paths(document,
  resolved)` filtered to `projects.<source>.`), renamed to `projects.<name>.`. Accepting a
  default stays silent: a default the source never wrote is not copied.
- Never copies identity: `path`, `description`, `production.path`, `bugs.repo`,
  `worker.launcher`. These name the SOURCE's own repository, deploy copy, tracker and
  wrapper; copying them would point the new project at someone else's.
- Copies `model` into `draft.project.model`; leaves `description` empty.
- Never copies an `os.*` value: those are already the fleet's.
- Answers each area that is NOT `must_have` with `match_posture` over the candidate's
  resolved map (as the re-run does), and appends the provenance entry
  `{"area": "like", "mode": "<source>"}`. **Must-have areas stay unanswered**, with the
  copied values in place: copying a project is a starting point, not twelve decisions, so
  the user still confirms each must-have area (§2's `confirm` verb keeps the copied values).
- Ends in `_commit`, so a copied value that does not validate for the new project is
  refused with the parser's text rather than saved.

`onboard_session` builds its draft through `onboard_open` instead of calling
`interview.new_draft` directly, which today rebuilds — and so wipes — any draft of that
name on every `start`, including one the user is half-way through in the dashboard. It
gains `like=` and `fresh=`; `jarvis onboard start <path> [--like <project>] [--fresh]`
passes them through. The `onboard` subcommand set stays exactly `start`, `questions`,
`explain`, `apply` (greeting spec §0.2); this adds two flags, not a command.

**Tests** (`tests/test_onboard_open.py`):
1. `like_draft` from a source whose document sets `validation.enabled`,
   `production.path`, `bugs.repo` and one `os.*` value: the new draft has
   `projects.<new>.validation.enabled` and NO `production.path`, `path`, `bugs.repo`,
   `worker.launcher`, and no `os.` key — the control being that the source document DID
   set each of them in the same test. The provenance entry `{"area":"like","mode":<source>}`
   is present, and no must-have area is in `answers`.
2. A source default it never wrote is absent from the new draft's `values` — control: that
   path IS in `config_version.resolve(parse_catalog(source_doc))`.
2b. An unknown `source` raises, naming the projects that exist; `onboard_session(like=…)`
   produces the copied value in the draft file.
3. `onboard_open` twice: the second call returns the first draft's edits (resume);
   with `fresh=True` they are gone; with `like` and no `fresh` it refuses.
4. The CLI parser accepts `onboard start <path> --like x --fresh` and still has exactly
   the four subcommands.
5. `ops.onboard_session` on a name whose draft holds an edit keeps the edit (the old
   rebuild-on-start is gone); with `fresh=True` it is gone.
6. An adopted project whose draft is older than a config version committed after it is
   rebuilt from live with `rebuilt` set — and as a control, the same draft with no newer
   version is resumed.

## 2. The draft as a page: `onboard_view`, `onboard_edit`, `onboard_drafts`

**Owns:** the three `ops` functions in §0.4 and `interview.unset_value`. No UI.

`onboard_view(name)` builds the §0.4 dict from the draft on disk and the live catalog
document, in the §0.2 section order. It reuses `interview` internals (`_spell` for each
knob's resolved spelling at `fleet=False`, `_others`, `_owns_os`, `_resolved`) and the
registry; it adds no prose of its own — every human sentence is `onboard_prose`'s or
`onboard_knobs`' line/example. Knobs per area: every tail `area_of` maps to that area,
`asked` first. Identity section knobs: `project.path`, `project.description`,
`project.model` (first-class, `source` `draft`), then `production.path`,
`production.ships`, `bugs.repo` as ordinary value knobs — the same paths the `delivery`
section lists, shown in both places on purpose: the user should not have to know which
area owns where production lives. The `wiring` section's knobs come from
`interview.wiring_choices(draft, document=…)`, one entry per lever with its wired state.
For the project that owns the OS (`_owns_os`), postures that turn the supervisor sweep
off are omitted, as `interview.questions` does.

`onboard_edit(name, verb, *, rev, **fields)` loads the draft fresh, refuses a stale `rev`
(§0.5.2), applies one verb, and returns the new view. Verbs:

| verb | fields | calls |
|---|---|---|
| `answer` | `section`, `mode` (`preset:<p>`), `fleet` (`"1"`/empty) | `interview.answer` |
| `defaults` | `section` | `interview.accept_defaults` |
| `confirm` | `section` | `interview.confirm`: answered, values UNCHANGED, mode = `match_posture` or `custom` |
| `remaining` | — | `interview.accept_remaining` |
| `set` | `path`, `value` (form text) | type the text, then `interview.set_value` |
| `unset` | `path` | `interview.unset_value` |
| `identity` | `field` ∈ `path`,`description`,`model`; `value` | edit `draft.project`, validate, save |
| `reason` | `value` | `interview.set_reason` |
| `fleet_ack` | — | `interview.fleet_warning` (the ONLY caller) |
| `wire` | `lever`, `wired` | `interview.wire` |

Typing `set`'s text: `ui.app.config_value_of(current, text)` / `config_type_ok` already
encode the dashboard's rule — but they live in the UI layer, which `ops` must not import.
Move them (unchanged, same names re-exported from `ui.app` so `/config` keeps working) to
`config_version` or a small leaf, and type against the knob's **default**, because a new
project has no live value. `identity` `path` re-runs the git-repository check.
`interview.unset_value(draft, path, *, document)` removes one path from `values`, marks
the area `custom`, and goes through `_commit`. `interview.confirm(draft, area, *, document)`
records the area as answered with the mode its current values match (`match_posture`,
else `custom`) and touches no value — "keep what is shown", the verb a pre-filled
(mimicked or re-run) section needs, since `defaults` would drop the copied values.

`onboard_drafts()` lists `paths.onboarding_dir()/*.json` that load as drafts: `name`,
`path`, answered count / 27, `adopted`, `like`, file mtime. A file that does not load is
listed with its error, never skipped silently.

**Tests** (`tests/test_onboard_view.py`):
1. Section keys equal §0.2's order computed independently in the test from `AREA_KEYS`
   and `onboard_prose.AREAS`.
2. `value`/`default`/`source` for one knob in each of the three sources, with the
   expected values computed by the test from `config_version.resolve(parse_catalog(…))`.
3. **The view never acks the fleet**: with a second project in the catalog, call
   `onboard_view` twice, then `onboard_edit(…, "set", path=<an os. path>)` raises
   `OpsError`; after `onboard_edit(…, "fleet_ack")` the same `set` succeeds. Without the
   first half the rule is decorative.
4. A stale `rev` is refused and the draft file's bytes are unchanged.
5. A refused `set` (a value the parser rejects) leaves the draft file byte-identical and
   the `OpsError` carries the parser's text.
6. `set` of `"false"` on a boolean knob stores `False`, not the string.
7. `set` then `unset`: the path is gone from `values` and the view shows `source` default.
   `confirm` on an area with values keeps every value and records the area answered.
8. A hand-written draft that fails `onboard_draft.validate` goes through `onboard_view`
   without raising, with `problems` non-empty; a corrupt `*.json` in `onboarding_dir`
   appears in `onboard_drafts()` with its error.

## 3. The workspace: rail, sections, moving through them

**Owns:** `ui/onboard_pages.py`, `templates/onboard_section.html`,
`templates/_onboard_rail.html`, the `GET /onboard/{name}`, `GET /onboard/{name}/{section}`
and `POST /onboard/{name}/edit` routes, and the help FORM (not its route).

**The page** is the shell (`base.html`) plus two columns:

- **The rail** (left, sticky, always visible): progress line ("12 of 27 answered · 2 to
  decide first"), a **filter box**, then every section grouped as §0.2, each entry a link
  showing title + status badge (`untouched`, `defaults`, `custom`, the posture name) +
  `changed` count + a fleet mark on `Whole fleet` entries while `fleet_acked` is false.
  The current section is highlighted. The filter matches section titles AND knob paths,
  tails and lines across ALL sections (the page embeds a small JSON index of every knob:
  path, tail, line, section) and lists matching knobs as links to
  `/onboard/{name}/{section}?focus=<path>#k-<slug>`. This is the main navigation primitive:
  the user types `production` or `budget` and lands on the setting, wherever it lives.
- **The section pane**: title, scope, status, summary, on/off costs, the fleet warning
  (with a `fleet_ack` button) on `os`/`both` sections while not acked; an action row —
  one button per posture (showing what it sets on hover/expanded), "accept defaults",
  and "Ask for help"; then the `asked` knobs as rows (path, line, editor, default, source
  badge, safety badge, apply class, a per-row "?" help button), then the `classified`
  knobs inside one open-by-default `<details>` ("More settings in this area"). Each row has
  `id="k-<slug>"`; `?focus=` marks that row `aria-current="true"` and scrolls to it. A
  pre-filled section (status `untouched`, some `source` draft or catalog) offers
  "keep these" (`confirm`) beside "accept defaults". Editors come from
  `config_widget`; a row with `source=draft` has an "unset" (back to default) action.
- **Moving**: "previous / next section" links at the bottom and keys `[` / `]`; `f`
  focuses the filter; every section is its own URL, so back/forward and the shell's
  recent-pages list work. Never tabs.
- **Every form posts `rev`.** A refusal (`OpsError`) re-renders the same section with
  status 400, the error above the row, and the typed value still in the editor.
- **Help form**: `<form method="post" action="/onboard/{name}/help">` with hidden
  `section` and `focus` (empty for the section button, the row's path for "?"). §4 owns
  the route; until it lands the form posts to a 405/404, and this section does not test
  the response.
- `identity` and `wiring` render through the same template with their own knob kinds
  (first-class fields; lever toggles). `review` is NOT rendered here (§5 owns it); the
  rail links to it.

**Not here:** the `/onboard` index and the `base.html` nav link (§6), review/apply (§5),
the help route (§4).

**Tests** (`tests/test_ui_onboard_pages.py`, TestClient):
1. A section page lists every knob of that section by `id="k-…"` and the rail lists every
   §0.2 key in order (compare against `ops.onboard_view`'s keys).
2. Posting `set` then reloading shows the new value with `source` draft; posting a bad
   value returns 400 with the parser's text AND the typed value in the input.
3. The filter index on any section contains a knob from a DIFFERENT section (so search
   reaches across), checked by path.
4. The help form posts `section` and `focus` exactly as named in §0.3.
5. A stale `rev` returns 400 with the reload message.
6. `GET /onboard/x` redirects to the first unanswered must-have section; an unknown
   section is a 404 page; `?focus=<path>` puts `aria-current="true"` on that row only;
   each section has previous/next links to its §0.2 neighbours.
7. **Pre-filled**: for an adopted project whose catalog sets a value, that row shows the
   catalog value with the `catalog` source badge.
One Playwright test in `tests_browser/test_ui_browser.py`: type into the filter, click a
match from another section, land on it with the row focused.

## 4. Help: the `onboard_helper` agent and the session it opens

**Owns:** the persona `src/jarvis/assets/onboard-helper/onboard_helper.md`,
`ops.onboard_help`, `ui/onboard_help.py` with `POST /onboard/{name}/help`, and
`templates/onboard_help.html`.

**The agent type** is new, separate from `greeting_agent`: frontmatter `name:
onboard_helper`, a one-line `description`, `tools: Bash, Read`. Its prose: the user is
filling in an onboarding form in the dashboard and asked for help about one section;
answer what they ask about THAT view; ground every claim in `jarvis onboard explain
<area|path>`, `jarvis onboard questions --area <a> --json`, `jarvis config show <p>` /
`jarvis config get` and `jarvis onboard apply --from <draft> --dry-run`, never in its own
memory; recommend a value and say WHERE to set it by giving the section URL with
`?focus=`. It is **read-only**: it never edits the draft, never applies, never runs
`jarvis config set`, never touches the project's files — the user makes the change in the
page, which keeps the draft's single writer the page (§0.5.2). If the user asks it to
change something, it says so and gives the link.

It is installed like the greeting persona but into its own directory, so neither clobbers
the other: `bootstrap._rebuild(bootstrap.ASSETS / "onboard-helper",
paths.onboarding_dir() / "helper", "agents")`.

`onboard_help(name, section, focus="")`:

- Builds the opening prompt **from `onboard_view(name)`**, never from page HTML: the
  section URL (`notify.ui_base_url(catalog)` + `/onboard/{name}/{section}`), project name
  and path, whether it is new or a re-run and the `like` source, the section's title,
  scope, status and summary, the fleet warning if pending, and for each knob of that
  section `path = value (default …, source …, safety, apply class)` — the `focus` knob
  first and marked "the user is asking about this one" — plus `problems` and the draft
  path. Deterministic for a given draft: same view, same prompt.
- Spawns `claude_cli.spawn_background(prompt, cwd=paths.jarvis_home(),
  name=f"[onboard] {name} · {section}", agent="onboard_helper",
  add_dirs=[persona_root, project_path])`. cwd is `$JARVIS_HOME`, not the project: the
  project's workspace trust is written only by the bootstrap. The name must NOT start
  with `[WO ` — `jarvis doctor` reports those as orphans.
- Returns `{session_name, job_id, attach, prompt, argv}` where `attach` is
  `claude attach <job_id>` (or `claude agents` when no id was printed).
- **Verify before building on it**: the dashboard runs under a systemd user unit, so
  `claude_cli.claude_bin()`'s resolution, `HOME` and the env `spawn_background` sets
  (`TURN_TRANSPORT_ENV`) may behave differently from a terminal. Check that the env var
  changes nothing for a session without `JARVIS_WO_ID`, and spawn one real helper session
  from the worktree (non-interactive `--bg`, trivial prompt), confirm `claude agents`
  lists it, then `claude stop` it. Record what you found as a learning
  (`jarvis learn add … --topic onboarding`); that learning is the acceptance for this
  check, which no unit test can make. The systemd-unit case stays a manual check for the
  user, named in the PR's validation section.

**The route** renders `onboard_help.html`: "Help is open in a new Claude session
<session_name>", the attach command, a note that it also appears in the Claude desktop
app's session list, and a link back to the section. **If the spawn fails** (no `claude`,
CLI error), the page says so with the error, and shows the prompt and argv so the user can
start it by hand — never a blank success.

**Tests** (`tests/test_onboard_help.py`, fake claude, never the real binary):
1. The prompt contains the focused knob's path and value FIRST among the knobs, and a knob
   from the same section; and as a control a knob from another section is ABSENT.
2. The spawned argv carries `--agent onboard_helper`, `--add-dir` the helper root (whose
   `.claude/agents/onboard_helper.md` exists) and cwd `jarvis_home`.
3. After a help spawn, `ops.run_doctor()`'s `orphaned_sessions` does not list it — and, as
   a control, a `[WO wo-gone] x` session spawned in the same test IS listed.
4. A spawn failure renders the fallback page with the prompt text, status 502. The
   success path: POST `section`+`focus`, the fake prints a job id, status 200, the page
   shows `[onboard] x · <section>` and `claude attach <id>`. Two `onboard_help` calls on an
   unchanged draft build equal prompts.
5. The persona file parses (`specs` / `panel.parse_definition`-style frontmatter split)
   with `name: onboard_helper` and no `Write`/`Edit` tool.

## 5. Review and apply

**Owns:** `ui/onboard_review.py`, `GET /onboard/{name}/review`, `POST /onboard/{name}/apply`,
`templates/onboard_review.html`, `templates/onboard_applied.html`. Renders inside §3's
layout (extends/includes `_onboard_rail.html`), so the rail stays and the user can jump
back to any section from the diff.

The review section shows, from `view["review"]` and a dry run
(`ops.onboard_apply(draft_path, dry_run=True)`):

- **Decide first, still open** — each `must_have_missing` area as a link to its section.
  Apply is refused while any remain (server-side, not only a disabled button); "accept
  defaults" on that section is one click away.
- **What will change** — every row of the real diff: path (linked to
  `/onboard/{name}/{section}?focus=<path>`), before → after, apply class, safety badge,
  the knob's line. Grouped by section, in rail order.
- **Why** — the reason field (`reason` verb). Required when any row is a safety key
  (`ops._require_reason` refuses otherwise; show that refusal verbatim, not a paraphrase).
- **What happens next** — the dry run's `restart_needed`/`restart_why`.
- `problems`, if any, verbatim.

`POST /onboard/{name}/apply` saves the reason if given, checks `rev` and the must-have
rule, then calls `ops.onboard_apply(draft_path)`. `onboard_applied.html` shows the config
version id (linked to `/config`), the change count, bootstrap actions and warnings, and —
when `restart_needed` — the reason and the exact command `jarvis start --catalog <file>`
to run, stated as something the user must do. The dashboard never restarts.

**Tests** (`tests/test_ui_onboard_review.py`):
1. `GET /onboard/x/review` renders the review (not §3's 404) — proves the registration order.
2. For a draft with two changes, each changed path renders as an
   `<a href=".../{section}?focus=<path>">` to its own section.
3. Accept defaults on every must-have but one; apply → refused, version head equal
   before/after; after `defaults` on the last → applied, a new head with actor `onboard`.
   A stale `rev` on apply is refused the same way. A safety change with no reason shows
   `ops._require_reason`'s text verbatim.
4. A new project's applied page contains `jarvis start --catalog` and the `restart_why` text.

## 6. The way in: the onboarding index and the mimic picker

**Owns:** `ui/onboard_index.py`, `GET /onboard`, `POST /onboard`,
`templates/onboard_index.html`, the `Onboard` link in `base.html`'s sidebar nav, and the
"Onboarding from the dashboard" section of `docs/os/operator/onboarding.md`.

- **Drafts in progress** — `ops.onboard_drafts()`: name, path, answered/27, mimic source,
  last edited, a "continue" link to `/onboard/{name}`; an unloadable draft is shown with its
  error. A line notes that the terminal interview (`jarvis onboard start`) continues the
  same draft, so the user can switch between the two.
- **Onboard a project** — form: repository path (required), name (optional, defaults to
  the directory name), **start from**: "shipped defaults" or "copy settings from <p>" for
  every catalog project (the mimic picker; one sentence says identity — path, production
  path, tracker, launcher — is never copied), and "start over" (`fresh`) when a draft of
  that name exists. Posts to `ops.onboard_open`; a refusal re-renders with the error and
  the typed fields kept.
- Already-onboarded projects are offered as "revise" entries that open their re-run draft.
- The operator doc section: how to open the page, the rail and filter, mimic, help,
  review/apply, and that a new project needs `jarvis start` after apply.

**Tests** (`tests/test_ui_onboard_index.py`):
1. `POST /onboard` with `like=<existing project>` answers 303 to `/onboard/{name}` and the
   draft file holds the source's copied value. An unloadable draft file is listed with its
   error.
2. A non-git path re-renders with the refusal text and the typed path kept.
3. The nav link appears as an anchor to `/onboard` (assert the `<a` tag, not the word —
   `kn-f6bb2ff4`).

---

## Agent profile

You are a Jarvis OS engineer implementing ONE section of "onboarding from the dashboard".
You work in your own git worktree on the OS's own repository, open one pull request, and
touch only the files your section names. Your section is your brief; §0 of this spec is
the contract every section shares, and you read it before writing anything.

**What you must know about this codebase.** The CLI is the OS: every read and write of
state goes through `ops.py`, which both `cli.py` and the FastAPI dashboard call. Never poke
SQLite, session files or the catalog JSON from anywhere but `ops`. Imports run strictly
downward — leaves (`paths`, `db`, `catalog`, `claude_cli`, `config_version`) then stores
then adapters (`interview`, `onboard_draft`, `onboard_knobs`, `onboard_prose`) then `ops`
then `cli`/`ui`. `ops` must never import `ui`. `cli.py` imports lazily inside handlers;
a `ui/onboard_*.py` route module imports `ui.app` helpers lazily inside its handlers,
because `app.py` imports the module.

Configuration is a content-addressed ledger: `ops._commit_document` is the only writer and
`ops.onboard_apply` is the only way an onboarding reaches it. `config_version.resolve()`
flattens a catalog to `dotted path -> value`; `catalog.parse_catalog` is the type system —
never write a second one. The onboarding draft lives at `paths.onboarding_dir()/<name>.json`,
never in the project, and its schema is frozen (greeting spec §0.3): add nothing at the
top level. `interview.py` is pure and already does every draft mutation through `_commit`;
wrap it, do not reimplement it.

**Traps that have cost work orders before.**
- `interview.fleet_warning()` is the user's ACK, not a getter — it records and saves.
  Only the `fleet_ack` verb may call it.
- The draft has no lock; every edit carries `rev` and a stale one is refused.
- `tests/` run by a worker inherit `JARVIS_WO_ID`, and `ops._refuse_worker_write` refuses
  every config/draft write then. Copy the autouse `not_a_worker` fixture from
  `tests/test_config_console.py` and add the one-line guard assertion. Never scrub it globally.
- Do NOT run the full suite: `uv run pytest tests/<your files> -q`. CI runs everything.
- No onboarding page uses tabs; Playwright's `inner_text` cannot see hidden panels.
  Dashboard GET query params are `str`. Headings render uppercase in the browser — lower
  the text before asserting on one. Asserting a CSS class name passes vacuously because
  `base.html` inlines the CSS; assert the element.
- Starlette matches routes in registration order: register `review` and the index before
  the `{section}` catch-all.
- `Daemon.reload_catalog` refuses a changed project set; a new project needs
  `jarvis start`. Tell the user; never restart a service, never hot-add a project.
- No test may exec the real `claude`; use `jarvis/testing.py`'s fake and assert on argv.
- A "must not appear" assertion needs a control in the same test showing where it DOES
  appear, or it cannot tell suppression from absence (`kn-6ea7bf84`).
- When the OS ends up doing nothing (a refusal, a failed spawn), the page says so with the
  real error. Silence is worse than a bad answer (`kn-53a20d3a`).

**Conventions.** House style is `caveman` and `ponytail` at level `full` for everything you
write. Comments state only the why, in one line. The smallest change that fully solves your
section, including callers and tests it breaks. New non-trivial logic leaves one small
test. Navigation is the guiding principle of this feature: every setting you render is
reachable by URL, every reference to a setting is a link to it.

**Never.** Do not implement a sibling's section, even a two-line piece of it — name the gap
in your finish summary. Do not change the draft schema, `onboard_apply`'s behaviour, the
`/config` tab, the daemon, `hooks.py`, `dispatch.py` or `WO_KINDS`. Do not add a CLI
subcommand (`onboard` stays `start`, `questions`, `explain`, `apply`) or a config knob the
spec does not name. Do not let the help agent write anything. Do not copy a default into a
draft because the user accepted it — accepting a default leaves the document silent. Do not
invent what a setting does: ground it in `onboard_explain`, the registry or a spec section.
