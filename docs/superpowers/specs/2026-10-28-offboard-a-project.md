# Offboard a project

`jarvis adopt <path>` makes a project OS-ready. Nothing takes a project out. This spec adds
the inverse, so a project that was onboarded by mistake — or has simply finished — stops
costing the user attention, and the OS stops spending anything on it.

The motivating case is `hermes_sandbox`, registered alongside `jarvis_os`,
`openclaw_sandbox`, `painforwisdom`, `shared_schedule` and `tesis_grado`. The capability
ships; the user runs it. Nothing in this feature is ever run against production by its
implementers.

## 1. What offboarding is, and the two modes

**`jarvis adopt` is not the inverse to mirror.** `cli.cmd_adopt` (`cli.py:3270`) only calls
`bootstrap_project`: it writes no catalog entry and no `projects` row. Registration happens
in `ops.start_os` (`ops.py:156-167`), which bootstraps every catalog project and then
`upsert_project`s it. There is no `adopt_project` function to turn around. Onboarding is
three writes in three places, and offboarding is three inverses:

| Where | Written by | Holds |
|---|---|---|
| the catalog file (`catalogs/*.json`, untracked, user-owned) | the user by hand, or `ops.set_config` | the project's entry: name, path, model, per-project settings |
| `$JARVIS_HOME/os.db` and `$JARVIS_HOME/neo.db` | `ops.start_os` → `CentralStore.upsert_project`, then every subsystem | the `projects` row, plus project-scoped rows in eleven other `os.db` tables and three in `neo.db` |
| the project's own repo | `bootstrap.bootstrap_project` (`bootstrap.py:410`) | `.claude/settings.json` hooks, `.claude/skills/`, `OPERATION.md`, a `README.md` stub, `.jarvis/`, a `.gitignore` line, and a trust entry in `~/.claude.json` |

Two modes, and the default is the reversible one:

**Archive** (the default). The `projects` row stays, at `status='archived'`, with its
`catalog_json` intact. The project's own `.jarvis/jarvis.db` is untouched, so every work
order, feature order, assumption, alarm and bill it ever had is still there and still
reachable through `jarvis search`. Central rows — knowledge, the steward ledger, Neo's
learnings — stay too. The catalog entry is removed and the repo is stripped. Re-adoption is
putting the catalog entry back and running `jarvis start`.

**Purge** (`--purge --yes`). Irreversible. The `projects` row and every project-scoped row
in `os.db` and `neo.db` are deleted, and `<project>/.jarvis/` is removed from the repo along
with its `.gitignore` line — which destroys every work order, event and turn the project
ever had. `jarvis search` will not find it, because there is nothing left to find.

Archive is the default because the OS's record model is append-and-settle, never delete:
`jarvis search`, `jarvis cost`, the steward ledger and Neo's learnings all answer questions
about work that is already finished, and the common reason to offboard — the wrong path, the
wrong name, a sandbox that served its purpose — is not a reason to lose the history. Purge
exists because the user asked for a way to undo an onboarding completely, and because an
archived project still leaves `<project>/.jarvis/` on disk.

### 1.1 Vocabulary

- **offboarded** — the OS no longer acts on this project: either `status='archived'` or no
  `projects` row at all. One predicate, both modes.
- **active** — `status='active'`, which is what `ops.registered_project_paths()` already
  means by a registered project.
- `central_store.ARCHIVED = "archived"` is the new constant.
  `CentralStore.set_project_status` (`central_store.py:679`) already exists and has no
  callers anywhere in the tree; it is the primitive that writes the flag.
- `status='stopped'` appears in the `projects` schema comment
  (`central_store.py:119`) and nothing in the tree ever writes it. Leave it alone; do not
  reuse it for archived.
- Do not touch the `'active'` string literal itself: `fleetcost.py:882` holds a second copy
  of that filter in raw SQL, by design.

### 1.2 What is NOT in scope

- No `jarvis project` command group and no `jarvis project list`. The OS uses flat top-level
  verbs for project-level operations (`start`, `stop`, `status`, `doctor`, `cost`, `adopt`)
  and groups only where a noun has many verbs. Offboarding has one, and `adopt` is already
  overloaded (`jarvis adopt`, `jarvis config adopt`) — a third place to look for project
  lifecycle is worse than a flat verb.
- No automatic offboarding. Nothing in the OS decides on its own that a project is finished.
- No un-archive verb. Putting the catalog entry back and running `jarvis start` is the
  documented path, and `upsert_project` already resets `status='active'` on conflict.
- No `--keep-repo`, and **no opt-out of the strip at all**. The strip runs on both modes.
  The ask says to "offer to strip it", and in a non-interactive CLI the offer is `--dry-run`:
  it lists every file the strip would touch and every refusal with its reason, and writes
  nothing. That is what an operator needs to decide with. A flag that leaves the OS's hooks
  and `OPERATION.md` in a repo the OS no longer manages produces a repo whose
  `.claude/settings.json` points a `PreToolUse` hook at a project the OS has never heard of —
  the one outcome offboarding exists to prevent. The strip already refuses every file the
  repo owns, so there is nothing a `--keep-repo` would protect that is not protected anyway.
- Deleting the project's git repository, or anything in it the OS did not put there.
- Fixing `morpheus`'s fallback checkout resolver or `schedule.os_owner`'s fallback owner in
  general. Both are pre-existing and filed in the backlog; this feature only stops them
  firing for an offboarded project.

## 2. The registry: archived-aware readers

`ops.registered_project_paths()` (`ops.py:814`) is the fleet's chokepoint for "which
projects are there" — roughly forty callers across `cli.py`, `ui/app.py`, `search.py`,
`issues.py`, `remedies.py`, `invariants.py` and `ops.py` — and it already filters
`p["status"] == "active"`. So the registry is where "offboarded" is decided, once, and
almost every surface inherits the answer for free.

**`ops.registered_project_paths(*, include_archived: bool = False)`.** The default keeps
today's behaviour byte for byte — active only — so no existing caller changes meaning.
`True` additionally returns archived projects. A purged project has no row and appears under
neither.

Two of your siblings code against that signature before they can read your PR, so pin it:
a test asserting `inspect.signature(ops.registered_project_paths)` has exactly one
keyword-only parameter named `include_archived` defaulting to `False`. Pin the other half
too — calling it with `include_archived=True` must write nothing, because a read that writes
would make even a perfect `--dry-run` dirty.

**Do not add the parameter to `CentralStore.list_projects`** (`central_store.py:682`). It
already returns every row, and each of its seven callers filters status itself:
`notify.py:52`, `ops.run_doctor` (`ops.py:313`), `ops.os_status` (`ops.py:560`),
`invariants.py:3305`, `invariants.py:3392`, `registered_project_paths` itself, and
`project_name_for_path`. Audit each one and leave it as it is unless this section's
reasoning says otherwise. Two in particular:

- **`CentralStore.project_name_for_path` (`central_store.py:692`) stays unfiltered**, on
  purpose: path-to-name must still resolve for an archived project or search and the hooks
  lose the mapping.
- **`fleetcost.registered_paths` (`fleetcost.py:877`) needs no change.** Active-only is
  correct there — it reports what the fleet is spending now. Do not "fix" it.

**`search` reaches archived projects.** `search._project_paths` (`search.py:57`) goes
through `registered_project_paths()`, so without this an archived project's history becomes
unfindable and the archive promise is empty. Pass `include_archived=True` there, and mark
each hit from an archived project so a caller can tell the record is readable and the
project is not running.

**The dashboard reads an archived project and operates nothing.** `jarvis search` must not
hand out links that dead-end, so the read-only pages that resolve a project name resolve
archived ones too, and say on the page that the project is archived: `/project/{name}`
(`ui/app.py:1136`), the work-order page, the feature-order page and the spec page. Every
POST action and the nav list (`nav_projects`, `ui/app.py:685`) stay active-only — archived
is readable, never operable. One of these is a latent 500 today:
`ui/app.py:1227` is a bare `ProjectStore(ops.registered_project_paths()[detail["project"]])`
subscript, and `INV-UI-HEALTHY` watches UI 500s, so an unguarded archive turns
`jarvis doctor` red. The guarded pattern to copy is immediately below it at
`ui/app.py:1250-1254`. A purged project is correctly "unknown project".

## 3. Purging the OS's own state

`CentralStore.purge_project(name) -> dict[str, int]` and
`NeoStore.purge_project(name) -> dict[str, int]`, both returning per-table rowcounts the way
`CentralStore.purge_work_order` (`central_store.py:818`) does. Nothing here reads the
project's status; a name is all they need.

Neither takes a `dry_run` parameter, and a test asserts so with `inspect.signature`. A store
method with a no-op mode is a second code path nobody exercises; honouring `--dry-run` is
section 7's job, by not calling you.

An unknown name returns all-zero counts and raises nothing, so a second purge is safe.

**A `project` column does not mean the row belongs to the project.** Two tables document
theirs as provenance — `gate_rules` (`central_store.py:274`, `-- provenance, never scope`)
and `detectors` (`central_store.py:310`, `-- provenance AND optional scope; '' = fleet-wide`).
Their rows are fleet-wide gate rules and detectors that merely *originated* in some project.
Deleting them because a sandbox is being purged would silently remove gates that guard every
other project, and the only trace would be a rowcount. **Never delete from either table.**
Both also carry the schema's append-mostly discipline in as many words: *"Rows here are NEVER
deleted and never rewritten in place except the counters, the timestamps and the arm/retract
fields"* — what the OS believed and when is evidence.

So there are two sets, and the distinction is the whole of this section.

**Scope — deleted.** Nine tables in `os.db`: `inbox`, `backlog`, `knowledge`,
`knowledge_reads`, `agent_calls`, `rule_fires`, `rule_recurrences`, `steward_decisions`,
`morpheus_items`. `rule_fires.project` is `NOT NULL` with no default, beside `order_id` and
`order_kind` (`central_store.py:372`): one fire against one order in one project, so it is
scope and it goes. `knowledge_read_hits` carries no `project` column and hangs off
`knowledge_reads`, so it cascades by parent — check whether the foreign key does that for you
or the delete has to. In `neo.db`: `questions`, `learnings`, and the `panel_opinions` rows
belonging to those questions.

**Provenance — never deleted.** Two tables, and they need *different* treatment, because the
columns are named alike and do not mean the same thing:

- `gate_rules.project` is provenance only. Clear it: set it to `''`. The column is
  `NOT NULL DEFAULT ''`, so NULL is not available and `''` is the schema's own value for
  "no provenance". The rule keeps guarding the fleet, and `reason` still holds the reviewer's
  words verbatim, so why the OS believes it survives the purge.
- `detectors.project` is provenance **and optional scope**, where `''` means fleet-wide and a
  name means that one project. Blanking one would *widen* a project-scoped detector to the
  whole fleet — the opposite error, and worse than leaving it. So: rows already at `''` are
  fleet-wide and are **not touched at all**; rows naming the purged project can never fire
  again and are **retracted** through the table's own documented mechanism,
  `CentralStore.retract_detector(detector_id, reason)` (`central_store.py:1518`), which
  retires the detector and every live remedy rule under it. Pass a reason naming the purge.
  A retracted row **keeps** its `project` name: that is the evidence of what was scoped
  where, and blanking it after retraction would read as a fleet-wide rule retired. A row
  already retired is left as it is — `retract_detector` refuses it, and it cannot fire.

**"Leaves no row behind" means no scope row.** After a purge, no row in the nine scope tables
or in `neo.db` names the project. `gate_rules` names it nowhere. `detectors` still names it
on retired rows only, by the rule above. Section 6's invariant must read it the same way:
a retired detector naming an offboarded project is not a leak.

Report both sets in the returned dict, under keys that say which happened — a count of rows
deleted and a count of rows whose provenance was cleared or retracted are different facts and
must not be added together.

**Derive the scope list from the live schema, do not hard-code it**, and exclude the
provenance tables by an explicit named constant (`PROVENANCE_TABLES = ("gate_rules",
"detectors")`) rather than by omission. Introspect which tables carry a `project` column
(`PRAGMA table_info`, or the equivalent over `sqlite_master`) and delete from each one that
is not in that constant. A hand-written delete list silently misses a table added after this
spec, which is the exact leak section 6's invariant exists to catch, and making the purge
self-maintaining is what the OS's own rule asks for (`kn-7698ad89`). But the same
self-maintaining rule is a hazard in the other direction: a provenance-only table added
later would be picked up and deleted automatically. The named exclusion is what makes the
derivation safe, so the constant is not a tidiness preference.

The pinning test names **both** lists — the nine scope tables and the two provenance tables —
and asserts their union equals the eleven tables carrying a `project` column today. A new
table then fails the test with a message saying it must be classified as scope or provenance
before purge may touch it. Classifying it is a decision, not a default.

The `knowledge_fts` index has `AFTER DELETE` and `AFTER UPDATE` triggers
(`central_store.py:555-567`), so a plain `DELETE FROM knowledge` keeps search consistent by
itself. Confirm that with a test rather than adding index maintenance.

## 4. The repo strip: the inverse of bootstrap

`bootstrap.unbootstrap_project(project: ProjectSpec, *, purge: bool, dry_run: bool) ->
BootstrapReport` takes the seven things `bootstrap_project` (`bootstrap.py:410`) writes back
out, with the same return type and the same `dry_run` contract it already has
(`bootstrap.py:419-428`).

It runs on both modes, because `.claude/settings.json` wires `jarvis _hook` into every tool
call of the **user's own** sessions in that repo, and `hooks.preflight_decision`
(`hooks.py:1737`) runs its gate classifier before any project-root check — so a repo left
bootstrapped for a project the OS no longer knows keeps paying hook latency and can still
have a command blocked by an OS that is no longer managing it. Verify that against
`hooks.gate_decision` before writing the test; if the classifier does not in fact fire
without a `JARVIS_WO_ID`, say so in the PR and the argument becomes latency alone.

What it takes out, and the rule for each — **the rule is never to destroy a user edit**
(for `OPERATION.md`, read as amended 2026-10-29 below):

| Artifact | Archive | Purge | Rule |
|---|---|---|---|
| `.claude/settings.json` | restore `.claude/settings.json.pre-jarvis` over it and delete the backup; with no backup, delete the file | same | `settings_drift()` (`bootstrap.py:76`) decides. `"manually edited since injection"` means leave it and warn, exactly as `inject_settings` (`bootstrap.py:180-186`) refuses without `--force-config` |
| `.claude/skills/<name>` (from `assets/project-skills`) | remove each tree that still matches the asset | same | `_tree_matches` decides; a modified tree is the user's and stays |
| `OPERATION.md` | remove when its bytes are recoverable (untracked and generated, or committed with no local change); otherwise move it to `.jarvis/OPERATION.md.offboarded` | remove when recoverable; otherwise leave it and warn with the exact `rm` | it must not linger (user ruling, Neo Q2500). A committed file's deletion is left unstaged, with a warning naming the command; the OS never commits it |
| `README.md` | never removed | never | bootstrap only ever creates a stub for a project that had none; it is the project's file now |
| trust entry in `~/.claude.json` | remove this path's `hasTrustDialogAccepted` | same | write through a temp file and `replace`, as `ensure_trust` (`bootstrap.py:373`) does. A malformed or unreadable `~/.claude.json` is a warning, never a crash |
| `.jarvis/` | **kept** | removed | the archived order history lives in `.jarvis/jarvis.db`, and section 2 promises `jarvis search` still reaches it |
| the `.jarvis/` line in `.gitignore` | **kept** | removed, with the `# Jarvis OS local state` comment above it if that is all that is left of the block | the directory still exists on archive, so the ignore line is still right |

> **Amendment 2026-10-29 (wo-5e2e4651, issue #1330, user ruling option B via Neo Q2500).**
> Supersedes the original `OPERATION.md` row ("remove if it is the generated file and
> untracked"; "`tracked_in_vcs` decides; committed, or edited past the `SPECIFICS_MARKER`
> block, means it stays") and the "never destroy a user edit" framing as it applied to that
> row. Current rule: offboard deletes the generated `OPERATION.md` in every case. When its
> bytes are NOT recoverable from version control (untracked and edited, or tracked with
> uncommitted local changes), archive first keeps them at `.jarvis/OPERATION.md.offboarded`;
> purge, which removes `.jarvis/`, leaves such a file in place and warns with the exact `rm`
> command. When tracked, a warning says the deletion is left unstaged in the working tree for
> the user to commit — the OS never commits it. For `OPERATION.md`, "never destroy a user
> edit" now means "never destroy bytes version control cannot return", not "never delete an
> edited `OPERATION.md`". Trigger: hermes_sandbox, committed plus local edits, archive mode.

`.claude/` and `.claude/skills/` are removed only if they end up empty. Everything is
idempotent: a second run is a report full of "already absent", never an error. `--dry-run`
lists every file it would touch *and* every file it would refuse to touch and why, and
writes nothing — that listing is what "offer to strip it" means on a command line.

## 5. Quiescing a project's live work

`ops.quiesce_project(name: str) -> dict[str, Any]` stops everything a project has in flight,
through the OS's own paths and **never by killing a process**. It is separate from the
command in section 7 because "stop everything this project is doing" is worth having and
worth testing on its own, and because the command's refusal has to describe what quiescing
would do before it does it.

Two halves:

**Report what is open, without changing anything.** Work orders in
`project_store.OPEN_STATUSES` (`project_store.py:33`), open feature orders, open improvement
orders and live investigations, each as `(id, kind, status, title)`. A count is not enough:
section 7 refuses by *listing* these, and a user cannot decide from a number.
`waiting_pr_merge` is an open status and is the case that will actually happen — a parked
order whose PR is still unmerged.

**Cancel them.** `ops.cancel(wo_id)` for work orders, `ops.cancel_feature_order`,
`ops.cancel_improvement_order`, `ops.cancel_investigation_order`. These already take the
worker session down through `worker_session.cancel`. Cancel children before parents, so a
feature's manager does not re-dispatch mid-sweep. Record the reason on each, so the work
order record says why it stopped.

**Stop the project asking for the user's attention.** Two central tables keep surfacing a
project whose orders are all settled, because neither listing filters on registration:

- `CentralStore.list_backlog` (`central_store.py:898`) filters by project and status, never
  by whether the project still exists. Mark this project's `open` rows `dropped`
  (`mark_backlog`, `SETTLED_BACKLOG_STATUSES`) with the offboard as the reason.
- `CentralStore.unacked_inbox` (`central_store.py:839`) is fleet-wide and
  `ack_inbox` (`central_store.py:861`) takes an id or everything — there is no per-project
  ack. Add one, and ack this project's unacked rows.

Both are the same thing offboarding is for: the OS stops asking about this project. Both are
also two of the rows `INV-OFFBOARDED-ACTIVITY` (section 6.3) looks for, so doing it here is
what keeps a legitimate archive out of `jarvis doctor`.

Return `{"open": [...], "cancelled": [...], "sessions_stopped": n, "backlog_dropped": n,
"inbox_acked": n}`, with everything that writes behind a parameter so the same function
serves both section 7's refusal and its `--force` path.

What this does **not** touch: queued Morpheus items and unanswered Neo questions. Those are
not cancelled, they are *ignored*, and that belongs to section 6 — settling them here would
either lose the record of a PR that did reach production or spend a model call on a project
the user just offboarded.

You will add methods to `central_store.py`, as will sections 2 and 3. Yours belong beside
the backlog and inbox methods, nowhere near the projects registry those two touch.

## 6. The live fleet: dropping a project without a restart

This is the part that makes the feature more than a database update, and the trap a worker
will walk into if it reads only sections 2 and 7.

`ops.registered_project_paths()` is **not** how the daemon finds projects. `daemon.py`
iterates `self.catalog.projects` in roughly sixty reads — `tick`, `dispatch_pending`,
`morpheus_tick`, the Neo paths, the health sweeps, `_fleet_stores`, the schedule owner. So
marking the registry does nothing to a running fleet.

And the catalog rewrite in section 7 does not help either, twice over:

- `Daemon.reload_catalog` (`daemon.py:772`) **refuses the whole reload** when the catalog's
  project-name set changes — "added or removed a project, and that is not a setting"
  (`daemon.py:799-805`, checked before `self.catalog = fresh` at `daemon.py:806`) — and a
  refusal is total, so unrelated settings in the same file do not land either. This is
  deliberate (`kn-351fe918`). `_refuse_reload` files one inbox row per daemon run.
- `ops.start_os` (`ops.py:142`) only spawns a daemon when none is running; against a live
  daemon it returns `already-running` and the daemon keeps its in-memory catalog. So the
  "Run `jarvis start`" that refusal suggests does not apply a project-set change to a
  running fleet either.

A daemon restart is a **gated** `service_restart`. An offboard that needs one is an offboard
the user cannot run.

### 6.1 Filter the catalog where it is assigned

`self.catalog` is assigned in exactly two places — `daemon.py:613` (constructor) and
`daemon.py:806` (reload) — and the only disk reads of the catalog file are
`daemon.py:793` and the boot path. So read the offboarded names out of `os.db` (the daemon
already holds `self.central`) and exclude them there, via a
`Catalog.without_projects(names)` that returns a copy with those entries dropped.
`catalog.py` is a stdlib-only leaf and must stay one: it may not read a database, so the
daemon passes the names in. Follow `fleet.load_pause` (`fleet.py:58`) for the shape of a live
`os.db` read, including its fail-closed rule.

`Catalog` is a plain dataclass (`catalog.py:1179`), so `dataclasses.replace` is all
`without_projects` needs.

`Daemon.catalog` **must stay a plain attribute** replaced at the top of `tick`, not a
re-reading property: `Fleet.reconfigure` and several tests assign `daemon.catalog` in memory,
and `kn-351fe918` records that a property breaks them.

**Staging the test is where this section is actually won**, and there are three ways to pass
for the wrong reason. `Daemon.__init__` seeds `self._catalog_stamp` at `daemon.py:660`, so a
`Daemon` built *after* the catalog was rewritten short-circuits the reload at
`daemon.py:782-791` and reloads nothing — your assertion then proves only that the file
changed. So: build the `Daemon` while the project is still **active**, prove the precondition
with a tick that dispatches for it, and only then archive it and rewrite the catalog. Build
it afterwards and the set comparison at `daemon.py:799` is filtered-against-filtered, the
sets match, and the new applicable-reload case is never exercised at all. Third, the
unrelated setting must change in the **same** write as the project removal, or two reloads
hide a refusal that ate the settings half. `tests/test_config_reload.py` has the pieces:
`write_catalog(..., projects=["proj_a", "proj_b"])` at `:27` for a two-project catalog,
`_rewrite` at `:60-64` which forges a future mtime with `os.utime` because the guard is a
real `stat()`, and `inbox()` at `:67`. Assert all three together — the project is gone from
`daemon.catalog.projects`, the setting landed, and **no** inbox row titled
`a catalog change was NOT applied`. The third is the one that catches the refusal.

One consumer does not tolerate a vanishing project: `Catalog.project(name)`
(`catalog.py:1185`) **raises** `CatalogError`, and `Daemon._supervisor_config`
(`daemon.py:3516`) calls it with a name it got from somewhere else. Walk the `self.catalog`
consumers that take a name rather than iterating the list, and decide for each whether a
project disappearing between ticks is a skip or a raise. A `CatalogError` out of a daemon
tick is a fleet-wide outage, not a missing project.

Then teach `reload_catalog` one new case: a project-set change whose **only** difference is
names that are already offboarded is applicable, not a refusal. Any other addition or
removal keeps today's total refusal, word for word. Without this, a user who offboards one
project cannot change any setting in the same file until the daemon is restarted. The
mtime-and-digest short-circuit (`daemon.py:782-791`) does fire for an offboard, because
`_write_document` goes through `os.replace`.

### 6.2 Five things the filter does not cover

Catalog filtering stops the daemon *looking up* a project. These five keep working anyway,
and each needs its own guard:

1. **Morpheus runs off a global queue.** `morpheus.sweep` (`daemon.py:3010`) and the owed
   actions pass (`daemon.py:3014`) iterate every `morpheus_items` row, and the checkout
   resolver is `dirs.get(item["project"] or "", fallback)` with
   `fallback = next(iter(dirs.values()), Path.cwd())` (`daemon.py:3009`). Once the project
   is filtered out of `dirs`, an offboarded project's queued item **runs its validation
   scripts inside another project's checkout.** Skip items whose project is offboarded; do
   not mark them failed, because `morpheus.act()` on a failure files an investigation or a
   Neo question and that is spend on a project the user just removed. Purge deletes the rows
   outright (section 3), so this is the archive path only.
2. **Neo drains globally.** `neo_tick` (`daemon.py:3029`) keys off `store.counts()`, and
   `_neo_drain` resolves the path as `paths.get(q["project"])` and tolerates `None`
   (`daemon.py:3171`). A queued question for an offboarded project is still answered — real
   model spend — and delivered to nothing. Skip it.
3. **`INV-NEO-ESCALATION-STALE`** (`invariants.py:1973-2016`) matches user-held questions
   against `registered_project_paths()`; an archived project's held question matches no store
   and is never settled, forever. Decide what it should say and make it say it once.
4. **`Daemon.stores`** (`daemon.py:618`, used at `daemon.py:666-669`) caches an open sqlite
   handle on `<project>/.jarvis/jarvis.db`. Filtering stops new lookups, but the handle
   survives, and under `--purge` on a live daemon the file is deleted beneath it — the next
   write goes to an unlinked inode or raises `sqlite3.OperationalError: disk I/O error`
   inside the tick. Eviction must `self.stores.pop(name, None)` and close it, and a tick
   after the directory is gone must raise nothing. Nobody else can test this: section 3's
   suite builds no daemon and section 7's builds one too late. (`stuck_swept` and
   `rules_cursor`, `daemon.py:622-624`, only go stale: harmless.)
5. **`schedule.os_owner(..., fallback=True)`** (`schedule.py:109-121`) returns the *first*
   project when no checkout owns the OS's code, so offboarding could silently move fleet-wide
   OS job ownership. Section 7's refusal to offboard the OS-owning project is what closes
   this; confirm it does, and if it does not, guard it here.

### 6.3 The invariant

`jarvis doctor` gains one OS-wide check, `INV-OFFBOARDED-ACTIVITY`, in
`invariants.OS_INVARIANTS` (`invariants.py:3603`) — doctor-only, not on the reconcile tick,
and **not repairable**, for the same reason `check_config_drift` is not: the two repairs are
opposites (finish the offboard, or put the project back) and only the user can choose.

**It fires only for a project with no `projects` row** — purged, or never registered. An
archived project's rows are never a violation. Getting this boundary wrong is the difference
between a useful check and one that is permanently red on a correctly offboarded fleet, so it
is the first thing to write and the first thing to test.

The reason is that an archive deliberately leaves things behind. Section 6.2 items 1 and 2
say the daemon *ignores* an archived project's queued `morpheus_items` and unanswered Neo
questions rather than settling them, because `morpheus.act()` on a failure files an
investigation or a Neo question and answering a question costs a model call — spend on a
project the user just removed. Section 3 retracts armed `detectors` only on purge. Those are
design decisions, not leaks: archive is reversible, and settling that state would make the
reversible mode quietly destructive, which is the opposite of why it is the default. An
invariant that flagged them would be flagging the spec.

What is genuinely wrong is a **purge that left rows behind**: a row naming a project the OS
has no record of, which the OS would still act on if the name ever came back. That is exactly
the leak section 3's schema-derived table list exists to catch — a `project`-column table
added after this feature and never classified. The invariant is the backstop for that
derivation, and it is worth having for that alone.

So it yields a violation when a row the OS would still act on names a project with no
`projects` row: `morpheus_items` not in a terminal state, unanswered `questions` in `neo.db`,
**armed** `detectors`, open `rule_fires`, unacked `inbox` rows, open `backlog` rows. The
detail names the project, the table and the row count, and says the two ways out: finish the
purge, or re-register the project.

Three exclusions, each of which would otherwise cry wolf. A `detectors` row at `project=''`
is fleet-wide, names no project, and is never a violation. A `gate_rules` row is never a
violation whatever its `project` holds: provenance records where a rule came from, not
activity the OS would still take. And every row belonging to an archived project is excluded
by the rule above, whatever table it sits in.

The accepted consequence, stated so nobody treats it as an oversight: an archived project can
accumulate unanswered Neo questions and queued Morpheus items indefinitely and nothing
reports it. They cost nothing — the daemon skips them — and they are what makes a re-adopt
pick up where it left off. The archived project's own page already says `archived` (section
2); if the counts ever need surfacing, that page is where they belong, not `jarvis doctor`.

**Amended 2026-10-30 (issue #1333).** "Purged, or never registered" are now two findings.
`offboard --purge` writes a tombstone to `offboarded_projects`; a name with a tombstone
reports `INV-OFFBOARDED-ACTIVITY` as above. A name with neither a `projects` row nor a
tombstone is a typo and reports `INV-UNKNOWN-PROJECT`. `jarvis doctor --repair` re-points
its rows when exactly one registered name matches it, ignoring case and `-` versus `_`.
Commands that take a project name typed by the user (`backlog add`, `bug report --project`,
`notify --project`) refuse a name the OS does not know, and so does `NeoStore.ask`. A row
about the OS itself carries the OS project's registered name, and `os` only when no catalog
is loaded.

Finally, `ops.start_os` must not `bootstrap_project` or `upsert_project` an offboarded
project. With section 7 removing the catalog entry this should already hold by construction —
prove it with a test rather than assuming, because "a removed project is not re-bootstrapped
on restart" is acceptance the user named by hand.

## 7. The offboard command

```
jarvis offboard <name> [--purge --yes] [--force] [--dry-run]
                       [--reason "…"] [--catalog <path>] [--json]
```

Help text: *stop the OS doing anything for a project — archive its record, cancel its
orders, and remove the OS's files from its repo*. It takes a **name**, not a path, so it is
not literally symmetric with `adopt <path>`; do not describe it as adopt's inverse.

`ops.offboard_project(name, *, purge=False, force=False, dry_run=False,
catalog_path=None, reason="") -> dict[str, Any]` is the operation, and **the order of its
steps is a safety constraint, not a preference**:

1. **Resolve.** The project must exist in the registry, archived included
   (`include_archived=True` — purging an already-archived project is a supported second
   step). An unknown name is an `OpsError` naming the known projects.
2. **Refuse the OS-owning project.** `schedule.os_project` / `Daemon._os_project` decide
   which project owns the OS's own code. Morpheus's release queue (`daemon.py:2994-2998`),
   the steward and the OS health sweep all hang off it, and `ops.py:10770-10795` already
   refuses a config write that would turn its sweep off. Same refusal, same reason, and no
   flag overrides it.
3. **Build the plan** — what would be stopped, deleted, rewritten and stripped, from
   `quiesce_project`'s report, the purge table counts and the strip's dry run. This is a
   pure read and it is the entire return value under `--dry-run`. Same dict shape in every
   mode, so a caller never parses two.
4. **Refuse on open orders** unless `--force`, listing them by id and status, and naming
   `--force` in the message.
5. **Quiesce** (section 5): cancel the open orders, take their sessions down, drop the
   project's open backlog rows and ack its unacked inbox rows.
6. **Strip the repo** (section 4). After quiescing and before any purge: a live worker's
   gate runs through `.claude/settings.json`, and removing it mid-turn would leave that
   worker running with no `PreToolUse` hook and therefore no gate at all. A missing or
   already-moved project directory is a warning, not a failure — the registry must still be
   cleaned.
7. **Mark or purge the registry**: `set_project_status(name, ARCHIVED)`, or
   `CentralStore.purge_project` + `NeoStore.purge_project` (section 3).
8. **Rewrite the catalog**, through `ops._commit_document` (`ops.py:10739`) and nothing
   else. Drop the project's entry from the document's `projects` list and any
   `projects.<name>` settings block, then let `_commit_document` validate, write the file
   and record a `config_versions` row with the reason; `config_version.diff` already emits
   `{"kind": "removed"}` rows, so no new code is needed for the version to be well formed.
   This step is **not optional in either mode**: `upsert_project` (`central_store.py:665`)
   hard-resets `status='active'` on conflict and `start_os` re-bootstraps every catalog
   project, so an archived project left in the catalog is resurrected *and* re-injected on
   the next `jarvis start` — the exact failure the user named. Going through
   `_commit_document` is also what keeps `INV-CONFIG-DRIFT` from reporting permanent drift
   against a file the OS itself changed (`kn-351fe918`). A project that is not in the catalog
   file at all is reported, not an error.
9. **Record it.** One `CentralStore.add_inbox` row under project `"os"`, so the offboard is
   on the record the user reads.

Partial failure: **the registry and the catalog move together, and the strip may fail after
them.** A project out of the registry but still in the catalog is re-adopted on the next
start; a project out of the catalog but still active in the registry is a daemon working on a
project with no configuration. Both are worse than a repo with a stale
`.claude/settings.json`, which is inert and which a second `jarvis offboard` fixes.

**A worker may not offboard a project.** `ops.offboard_project` refuses from a worker session,
the way `set_config` and `adopt_config` do through `ops._refuse_worker_write`
(`ops.py:10678`) — the fleet's project list is the user's, and a worker that could remove a
project could remove the one it is working on. The existing helper's message says
`jarvis config <verb>`, so word your own refusal for this verb. Consequence for your tests:
they run inside a worker session, so clear `JARVIS_WO_ID` with
`monkeypatch.delenv("JARVIS_WO_ID", raising=False)` (`tests/test_config_reload.py:21`) or
every one of them fails on the refusal.

**`--dry-run` must be provable from this side alone.** Your sections' dependencies each honour
their own `dry_run`, and asserting the filesystem is unchanged passes even when one of them
ignores it, as long as you never reached that step. So assert the stronger thing: monkeypatch
`bootstrap.unbootstrap_project`, `CentralStore.purge_project`, `NeoStore.purge_project`,
`CentralStore.set_project_status` and `ops._commit_document` to recorders, run with
`dry_run=True`, and assert **every recorder is empty**. That proves you never call a writer,
rather than trusting five of them to behave.

Surface rules:

- `--purge` without `--yes` is refused at the CLI, before `ops.offboard_project` is called,
  the way `wo delete --yes` is. The refusal says in full that purging deletes
  `<project>/.jarvis/` and with it every work order, event and turn the project ever had.
- `--dry-run` prints the plan, exits 0 and changes nothing — no registry write, no catalog
  write, no file touched. Same output shape as a real run, which is what makes it
  trustworthy. It mirrors `adopt --dry-run`.
- `--reason` reaches the config version row. Follow whatever `jarvis config set` does about
  requiring one.
- Human output is counts first, then detail: orders cancelled, tables cleared, files removed,
  files refused and why. `--json` returns the dict unchanged.

Docs, in the same change:

- A new operator doc `docs/os/operator/projects.md` — adopting, offboarding, archive versus
  purge, what each mode keeps, how to re-adopt — plus its line in the "Operating the OS" list
  in `docs/os/index.md`.
- One line in the `CLAUDE.md` crib sheet, directly under `jarvis adopt <path>`. `CLAUDE.md`
  is loaded as a bare system prompt by `evals/llm/test_jarvis_judgment.py`: keep the operator
  content first and dominant, and add a line, not a section.

A note for whoever writes the tests: `jarvis doctor --catalog <file>` (`ops.py:296-298`)
synthesises `status: "active"` straight from the catalog file and bypasses the registry
entirely. Harmless once the entry is gone, and surprising if you do not expect it.

## 8. Acceptance

Proven by tests under `tests/`:

Each one is phrased so it is observable from a test. Where the obvious phrasing was not,
the restatement below is the criterion and the obvious phrasing is not.

- Archiving leaves the `projects` row at `status='archived'` and leaves
  `<project>/.jarvis/jarvis.db` on disk, and `search.search(<the order's distinctive words>)`
  returns a hit whose `id` is that work order, whose `project` is the archived project, and
  whose `url` still resolves. ("`jarvis search` still finds it" is a CLI process and not a
  unit assertion.)
- Purging removes the `projects` row and every project-**scoped** row in `os.db` and `neo.db`,
  removes `<project>/.jarvis/`, and afterwards `search.search(<those same words>)` is empty.
- Purging deletes **no** `gate_rules` or `detectors` row, and a gate rule recorded with the
  purged project's provenance **still blocks the same command for another project** after
  `purge_project` — the rule's `project` is now `''` and it is still in force. A detector at
  `project=''` is untouched and still fires; one naming the purged project is retracted with
  a reason, not blanked, so it has not silently become fleet-wide.
- Either mode with an open work order raises `OpsError` whose message carries the order's
  **id**, its status and the string `--force`, and nothing has moved — status still active,
  the project still in the catalog file, `.claude/settings.json` still present. With
  `--force` the order ends `cancelled` and the offboard completes. `waiting_pr_merge` is
  covered explicitly.
- `--dry-run` calls no writer at all — asserted by recorders on
  `bootstrap.unbootstrap_project`, both `purge_project`s, `set_project_status` and
  `_commit_document` — and returns the same keys as a real run, whose reported effects match
  the plan.
- An offboarded project is **not** re-bootstrapped or re-registered by a later
  `jarvis start --catalog`: the stripped `.claude/settings.json` is still absent and the row
  is still `archived`. (Not with `live_daemon`: the bootstrap loop at `ops.py:157-167` runs
  before the pidfile check, and `upsert_project`'s `status='active'` reset is the failure
  being proven impossible.)
- **The same** `Daemon` instance, built before the offboard and never rebuilt, after exactly
  one further `tick()`: the project is absent from `daemon.catalog.projects`, no
  `fake_claude` call names it, its `morpheus_items` row is in its original state, its queued
  Neo question is unanswered with no model call recorded, its `Daemon.stores` entry is
  evicted and closed, and no `a catalog change was NOT applied` inbox row was filed while an
  unrelated setting from the same write did land. Six assertions on one unrestarted object
  is the honest form of "without a restart", and "stops dispatching, sweeping, draining Neo
  and sweeping Morpheus" is four of them, not one.
- `unbootstrap_project` restores `.claude/settings.json.pre-jarvis`; and for a hand-edited
  `settings.json` and a modified skill tree, the file's bytes are
  unchanged **and** `report.warnings` names it. `BootstrapReport` has only `actions` and
  `warnings`, so the warning list is the only channel a refusal can be observed through.
- Offboarding the OS-owning project raises `OpsError` for all four combinations of `--force`
  and `--purge`, with `schedule.os_project([...]) == <that project>` asserted as an explicit
  precondition — without it the test passes because `os_project` returned `None` and the
  refusal never ran.
- `INV-OFFBOARDED-ACTIVITY` yields exactly one violation for each of its six sources seeded
  alone **for a project with no `projects` row** — non-terminal `morpheus_items`, unanswered
  `questions`, armed `detectors`, open `rule_fires`, unacked `inbox`, open `backlog` — each
  detail naming the project, the table and the count, each with `repaired` falsy.
- **A clean archive yields zero violations.** Offboard a project with no `--purge`, after
  quiesce, with a non-terminal `morpheus_items` row, an unanswered Neo question and an armed
  detector naming it still in place — all three left by design — and
  `ops.run_doctor()["os"]` contains no `INV-OFFBOARDED-ACTIVITY`. Seed the same three rows
  against an archived project one at a time and assert zero each time. This is the criterion
  that stops the invariant contradicting sections 3 and 6.2; without it the default mode
  leaves `jarvis doctor` permanently red. Zero on a clean fleet with no offboarded project
  at all, too.

Two things cannot be criteria here, and should not be smuggled in as tests. Whether
`hooks.preflight_decision`'s classifier fires with no `JARVIS_WO_ID` is a **finding** section
4 reports in its PR body, not an assertion — do not write a test for a behaviour you did not
confirm. And "`--dry-run` behaves correctly against the production fleet" is the
`## How to validate in production` paragraph the user runs, by construction never a test.

Run only the test files covering what you changed (`uv run pytest tests/<file> -q`). CI runs
the full suite on three Python versions; a local full run is slower and strictly less
(`kn-356c724b`).

Every pull request body needs a `## How to validate in production` section —
`scripts/check_pr_validation.py` enforces it in CI. For this feature the honest answer is
usually `jarvis offboard hermes_sandbox --dry-run` against the production fleet, which writes
nothing, plus what the operator should see in `jarvis status` and `jarvis doctor` afterwards.
**Do not offboard anything in production.**

## Agent profile

You are a Jarvis OS engineer working on one section of the "offboard a project" feature.
Jarvis OS is a stdlib-only Python package (`src/jarvis/`, argparse + sqlite3 + json) that
runs a fleet of Claude Code workers across the user's projects. You are changing the part of
it that takes a project *out* of the OS.

What you must know before you write a line:

- **Read the Serena memories first.** `codebase-map` (every module, the layering, the three
  SQLite databases, the `jarvis start` call chain), `testing` (the suite, the isolation gate,
  the `fake_claude` fixture, the coverage map), `work-order-lifecycle`,
  `dev-vs-prod-environments`. Use `find_symbol`, `find_referencing_symbols` and
  `get_symbols_overview` — not grep — for Python symbols; a hook refuses text search over
  `.py` files. Grep is for genuine text questions: a config key, an error string, a schema
  comment.
- **The layering runs strictly downward**: leaves (`paths`, `db`, `catalog`, `claude_cli`,
  `probes`) → stores (`central_store`, `project_store`, `neo_store`) → adapters →
  `dispatch`/`ops` → `daemon`/`cli`/`ui`. Do not add an upward import. `catalog.py` is a
  stdlib-only leaf and may never read a database.
- **Three databases**: `$JARVIS_HOME/os.db` (central), `$JARVIS_HOME/neo.db` (Neo), and
  `<project>/.jarvis/jarvis.db` — the per-project one lives **inside the project's own
  repo**, which is why archiving can keep a project's whole history without keeping anything
  under `$JARVIS_HOME`. Everything opens through `db.connect()`; nothing calls
  `sqlite3.connect` directly.
- **`ops.registered_project_paths()` is the fleet's chokepoint** for "which projects are
  there" — about forty callers — and it filters on `status == 'active'`. The daemon is the
  exception: it iterates `self.catalog.projects`. Knowing which of the two a surface uses is
  usually the whole answer to "why did my change not take effect".
- **`jarvis adopt` registers nothing.** It only bootstraps the repo. Do not go looking for an
  `adopt_project` to mirror; registration happens in `ops.start_os`.

The test fixtures, because every section needs the same handful and they are not what you
would guess:

- `jarvis_home` is **autouse** — every test already has its own `$JARVIS_HOME`. Name it as a
  parameter only when you want the path. **Never call `monkeypatch.undo()`**: one shared
  monkeypatch per test, and undo hands the real fleet's state directory back.
- `project` (`src/jarvis/testing.py:2617`) is a git repo named **`proj_a`**, trusted through
  `claude_json`. `catalog_file` (`src/jarvis/testing.py:2786`) holds **exactly one** project.
  For two projects write your own: `write_catalog(path, project, projects=["proj_a",
  "proj_b"])`, the idiom at `tests/test_config_reload.py:27`.
- `started` is **not** a shared fixture — about seventy modules declare their own two-liner.
  Copy `tests/test_wo_cancel.py:13-15`: `ops.start_os(str(catalog_file), foreground=True)`
  then `Daemon(load_catalog(catalog_file))`.
- Anything that dispatches needs `fake_claude`. Read argv with
  `fake_claude.wait_calls(match, count=…)`, never `.calls` — the fake records itself a moment
  after the launching call returns. Settle an order with `assert settle_turns(store)`.
- `live_daemon` (`src/jarvis/testing.py:2278`) writes a pidfile for the **test process**, so
  `ops.daemon_running()` is truthy and `ops.start_os` returns `already-running`. Use it to
  mean "a daemon is up"; never in a test about restarting.
- `uv sync --extra dev` first in a fresh worktree, then `uv run pytest tests/<your file> -q`.

Conventions you follow:

- **The smallest change that fully solves your section.** No new option, abstraction or
  config nobody asked for; reuse what is there. Name in your PR what you deliberately did
  not do. Deletion beats addition.
- Comment only the *why* the code cannot show, in one line. Match the surrounding style: this
  codebase writes short docstrings that state the rule, not the mechanics.
- New non-trivial logic leaves one small test. Run **only** the test files covering what you
  changed (`uv run pytest tests/<file> -q`); CI runs the whole suite on three Python versions
  and a local full run is strictly less (`kn-356c724b`).
- Work in your own worktree. Open one pull request with the repository's template, filled in
  via the `open-a-pull-request` skill, including a `## How to validate in production`
  section — CI fails the PR body without one.
- House style is compressed prose: drop articles and hedging, never drop a
  `not`/`never`/`only`, and never abbreviate or compress an error string, a number, a symbol
  name or a CLI command. Your `jarvis wo finish --summary` is capped at 120 words.

Traps in this feature specifically:

- **A registry change does not reach a running daemon**, and a catalog project-set change is
  refused *whole* by `Daemon.reload_catalog` — settings in the same file included. A daemon
  restart is a gated action, so "tell the user to restart" is not an answer.
- **`CentralStore.upsert_project` hard-resets `status='active'`** on conflict, and
  `ops.start_os` re-bootstraps every catalog project, so a project left in the catalog comes
  straight back on the next `jarvis start`.
- **The catalog file belongs to the user.** Write it only through `ops._commit_document`,
  which validates, writes and records a `config_versions` row; anything else leaves
  `INV-CONFIG-DRIFT` permanently red.
- **Never kill a process to stop work.** Cancel through `ops.cancel`,
  `ops.cancel_feature_order`, `ops.cancel_improvement_order`,
  `ops.cancel_investigation_order`, which take the worker session down properly.
- **Never destroy a user edit** when stripping the repo. `bootstrap.settings_drift` and
  `bootstrap.tracked_in_vcs` are how you tell the OS's file from the user's.
- **A `--dry-run` that writes anything is a bug**, and it is the flag the user will reach for
  first in production.
- Morpheus and Neo both work off global queues, not the catalog. Filtering the catalog does
  not stop either of them.

What you must never do:

- Never offboard, archive or purge anything in the production fleet, and never write anywhere
  under `~/workspace/production`. You ship the capability; the user runs it.
- Never touch a sibling's section. Your work order's description names what you must not
  change and who owns it.
- Never widen a purge beyond the tables your section's rule derives, and never make purge the
  default anywhere.
- Never poke a SQLite database by hand to check something. Go through the stores.
