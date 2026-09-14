# The config console: versioned, auditable configuration

Design for feature order `fo-306b8f48`. Written 2026-08-27 against `jarvis 0.7.3`
(`d5af1f2`), reviewed with the `jarvis-architect` seat.

---

## 1. What the user asked, and which half of the premise is wrong

> "How do I turn on validation? I do not want to ship a new version just to flip a
> config variable."

Flipping `os.validation.enabled` never needed a release. It is a key in the user's own
catalog JSON — untracked, outside the repo, theirs to edit. Half the premise is already
false.

The other half is true, and worse than "it needs a restart":

1. **The edit does not take effect until jarvisd restarts.** `daemon.run_daemon` calls
   `catalog.load_catalog(path)` **once** and hands the object to `Daemon(catalog)`, which
   holds it for the life of the process (`src/jarvis/daemon.py:158`, `:2163-2175`). No
   reload, no watch.
2. **Nothing records what the configuration WAS.** Not who changed it, not when, not what
   a given work order ran under. `central_store.projects.catalog_json` holds the
   *current* raw project dict, overwritten on every `jarvis start`
   (`central_store.py:354-363`). Six months later the question is unanswerable.

### 1.1 And today, flipping it live is worse than doing nothing

Traced and verified in this session. Edit the file to `enabled: true` without restarting:

- `ops.finish` → `ops.validation_config()` (`ops.py:1121`) re-reads the catalog **fresh
  from disk**, sees `enabled`, opens round 1, parks the unit in `validating`.
- `Daemon.validation_tick` (`daemon.py:806`) deliberately does **not** check the kill
  switch — that is the drain design — and queues the round.
- `Daemon._validate_work_order` (`daemon.py:839`) reads `cfg = self.catalog.os.validation`
  — the **stale in-memory copy**, still `enabled=False`. `_validator(cfg)`
  (`daemon.py:1027`) returns `None`.
- The round is closed **`failed`** with `NO_VALIDATOR_REASON` — "Nobody has judged the
  work" — and a `validation_failed` event is written (`daemon.py:869-880`).

So every unit finished between the edit and the next restart acquires a false `failed`
round on its permanent record. Daemon reload is therefore a **prerequisite of the
acceptance criterion**, not a convenience. (Filed separately as a pre-existing defect.)

### 1.2 The prerequisite the ask does not name

The acceptance criterion is "turn validation on **for one project**". That is not
expressible today at any layer:

- `ValidationConfig` hangs off `OsConfig`, not `ProjectSpec` (`catalog.py:200-267`).
- `ops.validation_config()` (`ops.py:1121`) **takes no argument**, though all three
  callers hold the project name (`ops.py:1313`, `:1376`, `:1852`).
- The daemon's three reads are `self.catalog.os.validation` (`daemon.py:559`, `:855`,
  `:1101`), each on a thread that already holds a `ProjectSpec`.

Per-project validation resolution is a hard prerequisite. It is also **completely
independent of the config store**, which is why it is its own work order with no `needs`
edge (§10, child `perproject`).

### 1.3 The acceptance shape, in one paragraph

`jarvis config set jarvis_os os.validation.enabled true --reason "trying it"` writes a
version row and rewrites the catalog file. The running daemon picks it up on its next
tick, no restart. `jarvis config history` says who, when and why. Six months later
`jarvis wo show wo-xxxx` prints `config: cfg-a1b2c3d4…` and `jarvis validation show`
prints the version each round was judged under.

Nothing here changes what a setting means, and no default is flipped —
`os.validation.enabled` stays `false`.

---

## 2. The unit of change: a whole snapshot, content-addressed

**Decision.** The stored unit is a **whole configuration document**. The user's *act* is a
per-key edit. Both live in one row.

Whole document, because `parse_catalog` (`catalog.py:400`) is a whole-document function —
there is no per-key parser, and writing one is a second vocabulary to keep in sync. And a
work order does not run "under `os.validation.enabled=true`"; it runs under a `Catalog`.

A version row carries **two** JSON fields with two different jobs:

| field | what it is | job |
|---|---|---|
| `document_json` | the catalog file's content, canonicalised (sorted keys, 2-space indent) | what the file is rewritten *from*; what the id hashes |
| `resolved_json` | `parse_catalog(document)` flattened to `path -> value` with **every default materialised at write time** | what "which config judged this work order" reads; what diffs are computed over |

`document_json` must be the **raw JSON document, not `asdict(Catalog)`**. `parse_catalog`
preserves the project's raw dict in `ProjectSpec.raw` (`catalog.py:138`, `:499`) and
ignores unknown top-level keys, so a round-trip through the dataclasses would eat
forward-compatible keys the user wrote deliberately.

Because §6 forbids ever re-parsing a historical document with `parse_catalog`, the module
must also expose the way back: **`validation_config_from_resolved(resolved, project)`**,
building a `catalog.ValidationConfig` out of a stored `resolved_json` map. That is the
only way `_validate_work_order` can judge a round under the version it was stamped with
(§5), and without it that read has no legal implementation.

**Where the merge lives, and where it does not.** `resolve()` and
`validation_config_from_resolved()` perform **no inheritance of their own**. `resolve()`
flattens whatever `parse_catalog` already returned, and the `project` argument is a lookup
prefix, not a merge. Project-vs-OS validation inheritance belongs entirely to the
`perproject` piece (§1.2), which adds it inside `catalog._parse_validation`. This matters
operationally: the two are independent work with no shared file, so they run in parallel —
but only if the ledger side never reaches into the catalog merge. Until `perproject`
lands, `resolve()` simply yields `os.validation.*` paths and no project ones; afterwards
it yields both, for free and with no change to the ledger.

**The version id is content-addressed**: `cfg-` + `sha256(canonical document_json)[:16]`,
the same move as `evidence.fingerprint` (`evidence.py:100`). Consequences that are
features: an edit that changes nothing writes no row, and re-applying an old
configuration lands back on its old id.

Alongside: `changes_json` (the path/old/new triples the user actually asked for),
`actor`, `reason`, `ts`, `schema_version` (`bugreport.jarvis_version()`).

**Where the obvious answer is wrong.** "Store the sparse user document only" or "store a
diff from shipped defaults" both fail on a default change. `DEFAULT_AUTOCOMPACT_WINDOW`
moved 150,000 → 400,000 in this repo, and the argument is written out at
`catalog.py:66-78`. Under diff-from-defaults every historical row that omitted the key
would silently change meaning on that release. `resolved_json` with defaults frozen at
write time is the only shape that survives it — §5 reaching back to settle §2.

**Fleet-wide, not per project.** One monotone history covering the `os` block and every
project, because project settings *resolve against* `os.defaults` at parse time
(`w.get("model") or p.get("model") or os_cfg.default_model`), several settings are
fleet-scoped with no project to belong to, and traceability wants **one id** stamped on a
work order. "Tune each project's config" is served as a *view*
(`jarvis config show <project>`, `jarvis config history --project p`), not as separate
counters.

---

## 3. Where the truth lives: the DB is the record, the file is a materialised view

Neither can lose outright, and pretending one can is how this feature goes wrong.

**The file cannot lose.** `ops.start_os(catalog_path)` takes a path; `_spawn_daemon`
passes `--catalog` to the detached daemon; `run_daemon` calls
`load_catalog(catalog_path)`; `install.sh` scaffolds `$JARVIS_HOME/catalog.json` on a
fresh machine. And ~35 test files plus 3 eval files reference `load_catalog` — nearly
every fleet fixture in the suite is `ops.start_os(str(catalog_file));
Daemon(load_catalog(catalog_file))`. A design that removes the file breaks bootstrap and
most of the test suite.

**The DB cannot lose either.** A JSON file the user hand-edits is not an auditable
history, and the catalogs are untracked, so git is not the history either.

**Decision.** `os_config_versions` in `os.db` is **the record**. `jarvis config set`
writes the version row and rewrites the catalog file atomically from `document_json`. The
daemon and `ops.resolve_catalog` keep reading the file and **change not at all**.
`os_state["catalog_path"]` is already the pointer (`ops.py:113`, `daemon.py:242-243`).

Consequences, stated rather than discovered:

- **Hand-editing still works.** Adoption is content-addressed, the way
  `CentralStore._seed_gate_rules` is (`central_store.py:965-987`): a file whose canonical
  hash already equals the head version writes nothing; one that differs raises a version
  with `actor="file"`.
- **The first `jarvis config set` reflows a hand-maintained catalog** (sorted keys, fixed
  indent). Acceptable; it belongs in the release note.
- **Write order is a real decision, not a detail:** validate → write the file atomically
  (temp + `os.replace`) → write the row. If the row write fails you have a running config
  with no record, which the drift invariant catches and `adopt` fixes. The other order
  gives you a head version nobody is running, which nothing detects.

**`INV-CONFIG-DRIFT`** goes in `invariants.OS_INVARIANTS` (`invariants.py:1499`, beside
`check_ui_healthy` / `check_gate_canaries`) — **not** in `check_catalog`
(`invariants.py:1330`), which takes an already-parsed catalog and answers a different
question. **Not repairable**: repairing means choosing whose edit to destroy. It reports
both hashes and names the two commands (`jarvis config adopt` /
`jarvis config restore <version>`).

**It is a `jarvis doctor` check, NOT a per-tick one, and that split is deliberate.**
`check_os()` has exactly one caller — `ops.run_doctor` (`ops.py:175`, `:207`) — and
`check_gate_canaries`'s own docstring spells the rule out at `invariants.py:1374-1376`.
Do not wire this into `Daemon.check_invariants`; the daemon's reconcile tick runs the
per-project `INVARIANTS` tuple only. `jarvis doctor` is the evidence.

---

## 4. What a running fleet does with a change: reload once per tick, guarded

`Daemon.self.catalog` has 16 read sites, all through the attribute (`daemon.py:158`,
`213`, `242`, `274`, `353`, `408`, `417`, `559`, `855`, `1101`, `1349`, `1375`, `1376`,
`1456`, `1484`, `2090`). That is a tight enough seam.

**Decision.** One reload at the top of `Daemon.tick()`, guarded on mtime + content hash,
so an unchanged file costs one `stat`. `self.catalog` stays a **plain attribute** and
every downstream read is untouched.

**Do NOT make `Daemon.catalog` a property that re-reads.** Two concrete reasons:

- `_validate_work_order` (`daemon.py:839`) and the Neo drain (`daemon.py:1349`) run on
  other threads and hold `cfg` for the length of a round or a drain. A re-reading property
  lets `diff_chars` and `max_rounds` change under a live round — judged at one truncation
  limit, settled against another.
- `tests/test_validation_loop.py:110-117` and `tests/test_feature_validation.py:160-168`
  (`Fleet.reconfigure`) **assign `daemon.catalog` directly**, verified. The harness for
  reload already exists; a property breaks it.

**Failure is sticky-last-good.** A `load_catalog` that raises inside `tick()` must not
take down every project in the fleet over one bad file. Keep the last good catalog, raise
**exactly one** inbox item, and do not repeat it every five seconds — the `pr_poll_warned`
pattern at `daemon.py:196` is the precedent.

**Scope limit.** Reload covers **settings only**. If `catalog.projects` gained or lost a
name, refuse the reload with an inbox item telling the user to `jarvis start`: an added
project has never been through `bootstrap_project`, and a removed one leaves a stale open
`ProjectStore` in `self.stores`.

### 4.1 The drain property, and how hot-reload threatens it

`os.validation.enabled` gates **opening** a round and never **settling** one
(`daemon.py:806-814`), so turning it off drains what is in flight. Today that holds partly
*by accident*: the daemon's copy is frozen at boot, so the settle side cannot follow a
disable.

Add naive hot-reload and disabling at 3am makes `_validator(cfg)` return `None` for every
open round, closing them `failed` / "nobody judged the work" (`daemon.py:869-880`). The
unit still lands via `ops.land_finished`, so nothing is stranded — but the record acquires
a false `failed` round on every in-flight unit. Exactly the §1.1 bug, arriving by a new
door.

**The fix is not to skip reload.** `_validate_work_order` resolves its validator from
**the config version the round was opened under**, not from the live catalog. That is the
second, load-bearing reason a round needs a config stamp, and it is why the stamp work
order must land **before** the reload work order.

### 4.2 Not every key can be hot

`jarvis config set` prints which class the key is in, so the user is never left guessing:

| class | meaning | examples |
|---|---|---|
| `hot` | in force on the next tick | `os.validation.*`, `os.neo.*`, `max_concurrent`, knowledge budgets, notification sinks |
| `next-dispatch` | applies to work orders dispatched from now on; running workers keep what they were dispatched with | `model`, `effort`, `permission_mode`, `autocompact_window`, `append_system_prompt` |
| `restart` | needs `jarvis start` or a UI restart | `os.ui.port`, a project's `path`, `settings_overrides` |

`next-dispatch` is not new: `dispatch.dispatch_work_order` already freezes
model/effort/permission_mode onto the row before the first turn, "so every later turn
rebuilds the same briefing from the record rather than re-reading a catalog that may have
moved" (`dispatch.py:785-792`). `settings_overrides` is `restart` because nothing re-runs
`bootstrap_project`; the command must **say so** rather than accept it silently.

---

## 5. Traceability: stamp the unit, and USE the stamp

A join through time is the wrong answer: it needs a total order of versions *and* the
assumption that nothing else moved, and it cannot answer for a work order that ran across
three edits. An event alone is not enough either — `wo_events` passes through
`timeline.build_timeline` and `DEBUG_KINDS` filtering, so a reader may never see it. An
event is the right *second* record, not the first.

**Two columns, both through `ADDED_COLUMNS`:**

- **`work_orders.config_version`** (`project_store.py:401`). Written in
  `dispatch.dispatch_work_order`'s existing `resolved` dict (`dispatch.py:785-792`) and
  its `dispatched` event payload. One key added to a move the function already makes.
- **`validation_rounds.config_version`** — a different question, and the one the user
  actually asked. A work order can have three rounds under three configs. The table
  already carries a per-round `fingerprint` of the *evidence*
  (`project_store.py:256`); this is the same idea about the other input.

**`NULL` means "ran before the console existed"** and must render as *not recorded*, never
as version 1 — the same honesty boundary `pr_state` and `bill_json` document at
`project_store.py:424`, `:446`, and `knowledge_reads.observed_from` (`kn-0281d10b`).

**The trap the brief must name:** `validation_rounds` already ships, so
`CREATE TABLE IF NOT EXISTS` will **not** add the column. It must go in `ADDED_COLUMNS`.
This is precisely the failure `tests/test_schema_upgrade.py:181` (`usage_json`) and `:242`
(`base_sha`) exist to catch; that file's docstring is the instruction.

**The stamp is not only for display.** `_validate_work_order` resolves its validator from
the round's stamped version (§4.1). That is what keeps the drain property true once the
daemon reloads.

**Rendering costs almost nothing** because the funnel exists: `ops.round_line`
(`ops.py:1045`) is the single formatter behind `jarvis wo show` (`cli.py:52`),
`jarvis validation show` (`cli.py:2007`) and `jarvis fo show` (`cli.py:1402`) — its
docstring says so. Add the version there once. `jarvis wo show` also gains a header line:
`config: cfg-a1b2c3d4e5f6 (3 versions since)`.

No third column on `feature_orders`: its rounds live in the same polymorphic
`validation_rounds` table via `fo_id`, so the round stamp covers it.

---

## 6. The upgrade problem — where the obvious answer is flatly wrong

The obvious answer is "migrate stored snapshots forward on each release". That destroys
the only thing they are for.

**A stored snapshot is not configuration you need to run. It is evidence of what ran.**
Six months later, "what configuration judged this work order" must return what the config
*was*, not a translation into today's vocabulary.

> **The ruling: historical versions are immutable. They are never migrated, never
> re-parsed by `parse_catalog`, and never re-validated. They are rendered, diffed and
> explained.**

That splits the world in two, and the split makes every sub-question easy:

1. **HEAD is live config.** It must parse under today's `parse_catalog`. It is the file.
2. **Every non-head version is a document.** Never fed to the parser. Rendered as JSON
   plus a diff against its predecessor, with `schema_version` shown beside it.

Sub-answers:

- **A release adds a key.** Old snapshots lack it; the diff says "introduced in 0.6.0".
  Display concern, no data change.
- **A release changes a default.** Settled by `resolved_json` materialising defaults at
  write time (§2). This is the argument that forces whole-snapshot.
- **A release renames or removes a key.** The historical snapshot keeps the old name
  **for ever**. Only *display* needs help — a `catalog.RENAMES` map, display-only, never
  rewriting a row. Nothing has been renamed yet, so the **rule** ships here in prose and
  the **map** is backlogged: an empty dict with no consumer is speculative.
- **A live document names a key that no longer exists.** Today `parse_catalog` **silently
  ignores** unknown top-level and section keys — it is `.get()` throughout; it only
  rejects unknown roster/seat names, panel kinds, permission modes and duplicate project
  names. **Do not make it stricter.** A user upgrading with a stale key must not get a
  fleet that refuses to boot because an audit feature got opinionated. Surface it as a
  `jarvis doctor` warning. There is a shipped example to test against: `NeoConfig.timeout`
  is "parsed and never read" with a filed backlog id (`catalog.py:235-237`).

**`parse_catalog`'s existing strictness stays.** Its arguments at `catalog.py:306-318` and
`:353-364` are right: a roster naming an unknown seat is a typo that silently removes a
safety check, and catching it at boot is worth a loud failure. That rule is about *values
inside known keys* and is a different thing from unknown keys.

### 6.1 A release that changes what the fleet runs writes a version of its own

`resolved_json` is materialised at write time, so after an upgrade the **head** row's
`resolved_json` may no longer describe what is running. On daemon start, re-resolve the
head's `document_json` under the running build; if it differs, write a new version with
`actor="release"` and a generated reason naming each default that moved
(`upgrade 0.7.3 → 0.7.4: os.defaults.autocompact_window 400000 → 300000`).

This is what makes the ledger honest: it stops being "changes the user made" and becomes
**"every change to what the fleet actually runs"**. Without it, an upgrade is a behaviour
change with no row.

### 6.2 History is append-only

Versions are never rewritten, never garbage-collected. `jarvis config restore cfg-abc`
does not rewind — it writes a **new** version whose document equals that one's, authored
by the user with their reason (and, being content-addressed, landing back on the same id).
Same discipline as knowledge retraction (`kn-5f018158`): an audit trail that rewrites
itself is not one.

---

## 7. Who may change what, and where the recursion actually is

**No *user* config change is a gated privileged action.** The gate machinery is a
`PreToolUse` hook on a *worker's command* (`hooks.gate_decision` / `hooks._resolve_gate`,
first in `preflight_decision`). A user typing `jarvis config set` never passes through it.
Routing user edits into `approvals` would also corrupt the `dismissed` count, which is the
OS's classifier false-positive rate.

**A WORKER changing config must be stopped, and this is the real hole.** Verified in
`hooks.preflight_decision` (`hooks.py:465-466`): after the gate check, any
`is_jarvis_command_chain(...)` is `_allow`ed. Ship the console naively and every worker in
the fleet has a write path to `permission_mode`, `gates` and `validation`. Two layers, and
note which is load-bearing:

1. **In code, in `ops`.** `ops.set_config` refuses when `JARVIS_WO_ID` is set and there is
   no live grant. **This is the layer that counts**, because `ProjectSpec.gates` is empty
   by default (`catalog.py:136-137`) — on an ungated project a hook-level gate protects
   nobody.
2. **In the rule base**, for the audit trail and for gated projects: a `config_write` gate
   kind with a `match` in `gate_rules.SEED_MATCHES` on
   `jarvis config (set|unset|restore|adopt)`, **plus a `canary` in `SEED_CANARIES`**. The
   canary is what cuts the recursion: the console can disable gates, but the command that
   disables gates can never acquire a learned exemption — `check_canaries` re-derives that
   from the live table and `INV-GATE-CANARY` runs it every tick
   (`gate_rules.py:26-39`). **`gate_rules.SEED_VERSION` must be bumped from `"1"`** or the
   new builtin never reaches a live `os.db` (`central_store.py:977`, verified).

**Money vs safety: one write path, two presentations.** Two classes with two write paths
is a second vocabulary to keep in sync with `catalog.py`, and the boundary is arguable
(`max_concurrent` is money *and* blast radius). What earns its place is one flat
`catalog.SAFETY_KEYS` list (`*.permission_mode`, `*.gates.*`, `os.validation.*`,
`os.neo.enabled`) used for exactly two things: a louder confirmation, and a **mandatory
`--reason`** on the version row.

**May Neo change config? No, and it is not close.** Neo has no `ops` write path today; its
containment is that it answers and reviews. Giving it config writes makes the reviewer of
gate dismissals also the thing that can turn gates off. If it ever should, the shape
exists — `gate_rules.propose_exemption` / `Proposal`: it *proposes*, the user decides.
Backlogged.

---

## 8. The surface: the CLI is the write path, the UI is a form over `ops`

Prime directive 1 is satisfied by `ops` being the single implementation. `jarvis config`
(a `sub.add_parser("config")` group, modelled on the `gate` group at `cli.py:388-444`) is
the write path; the `/config` tab's POST calls the same `ops` function, like every other
POST under `ui/app.py:770-960`.

```
jarvis config show [project] [--version cfg-…] [--json]   # effective config + provenance
jarvis config get <path> [--project p]
jarvis config set [project] <path> <value> --reason "..."
jarvis config unset [project] <path> --reason "..."
jarvis config history [--project p] [--limit n]
jarvis config diff <a> <b>
jarvis config restore <cfg-id> --reason "..."             # writes forward
jarvis config adopt --reason "..."                        # a hand-edited file -> a version
```

**One scope cut, explicitly.** The v1 UI is **read-only plus boolean toggles**: history,
version diff, and a toggle for `enabled`-shaped keys. A generic structured-JSON editor in
the dashboard is a different and much larger job and is where this feature would blow its
budget. Full editing stays on the CLI, where the value is already text. `/config` sits in
the `base.html` nav beside `/gates` and `/knowledge`; if it needs tabs, `kn-97cadc92` is
the mechanism and the rule (never paste the script back onto a page).

---

## 9. Data model

```sql
-- $JARVIS_HOME/os.db  (a NEW table: arrives free on CREATE TABLE IF NOT EXISTS)
CREATE TABLE IF NOT EXISTS os_config_versions (
    id             TEXT PRIMARY KEY,        -- cfg-<sha256(document_json)[:16]>
    ts             REAL NOT NULL,
    actor          TEXT NOT NULL,           -- user | file | release | <wo-id>
    reason         TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL,           -- bugreport.jarvis_version() at write time
    document_json  TEXT NOT NULL,           -- canonical catalog document; APPLIED  (§2)
    resolved_json  TEXT NOT NULL,           -- path -> value, defaults frozen; EVIDENCE (§2)
    changes_json   TEXT NOT NULL DEFAULT '[]',  -- the edits the actor asked for
    source_path    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_os_config_versions_ts ON os_config_versions(ts);
```

Head is the most recent `ts`. The two `config_version` columns on `work_orders` and
`validation_rounds` go through the respective `ADDED_COLUMNS` maps, and
`tests/test_schema_upgrade.py` must cover both (`kn-5f018158`).

**Precedent to read before writing any of this:** `src/jarvis/gate_rules.py` made exactly
this move one layer down — module constants became rows in `os.db`, seeded on first
`CentralStore` open, with `INV-GATE-CANARY` re-deriving safety from the live table every
tick.

---

## 10. Decomposition

Six children. `perproject` has no dependencies and starts on day one alongside `store`.

```
store ──┬── cli ──┬── ui
        │         │
        └── stamp ┴── reload
perproject ───────────┘
```

Real edges: `store → cli` and `store → stamp` (nothing to call otherwise); `cli → ui`
(the route is a call to `ops.set_config`). `stamp → reload` is a different kind of real
edge — `reload` compiles fine alone, but merged first it makes disabling validation close
every in-flight round `failed` (§4.1), regressing on `main` the exact property the user
needs preserved. `cli → reload` and `perproject → reload` exist because `reload` owns the
end-to-end test (§10.3), and that test is written against the per-project form
(`jarvis config set proj_a validation.enabled true`) because that is the sentence the user
actually asked for. Without the `perproject` edge a `reload` worker either weakens the
test to the fleet-wide switch or implements `perproject` itself.

Edges deliberately **not** drawn: `perproject` depends on nothing upstream; `stamp` does
not depend on `perproject`; `ui` does not depend on `stamp` or `reload`.

`ui` is the only piece droppable without failing the acceptance walk.

### 10.3 The one end-to-end assertion, owned by `reload`

With the OS started, validation off, and a `Daemon` that is **never reconstructed and
never assigned to**: finish a work order — it settles `completed` with no row in
`validation_rounds`. Run
`jarvis config set proj_a validation.enabled true --reason "trying it"`. Tick once. Finish
a second work order — it parks in `validating`, and its round's `config_version` equals
the id `jarvis config history --json` reports as head. No restart happened, no
`daemon.catalog` was assigned, and the history shows one row with `actor="user"` and that
reason.

The before/after pairing is what makes it falsifiable: "it validated" alone is satisfied
by a fleet that was already validating.

### 10.1 What must not be split

1. **The file rewrite and the version row** — two work orders is a guaranteed drift bug
   with no interface worth having.
2. **An `ADDED_COLUMNS` entry and the code that reads its column** — silent at write time,
   fatal at read time, on live data only.
3. **`ops.validation_config`'s signature and all its call sites** — `main` is broken
   between the merges otherwise.
4. **The reload and its "stable for one tick" guarantee** — one reviewed decision.
5. **`jarvis config` argparse and `ops.set_config`** — the CLI half cannot be tested alone.

### 10.2 The riskiest session

`stamp`. It touches two tables' migration semantics (one of which already ships and so
will *not* get its column from `CREATE TABLE IF NOT EXISTS`), the dispatch path every work
order goes through, and `_validate_work_order`'s validator resolution — the machinery the
drain property rests on. One of those three failure modes never fails in a test that
builds its database fresh.

---

## 11. What this design does NOT know yet

1. **The real production catalog is unreadable from here** — catalogs are untracked. If
   the user's file carries key ordering or pseudo-comment keys they value, the canonical
   rewrite in `cli` will eat them. Worth one look at `$JARVIS_HOME/catalog.json` before
   that child starts.
2. **Whether `validation_rounds.config_version` should fall back to the work order's stamp
   when NULL.** Leaning yes, but a feature-order round (`fo_id`) has no `dispatched` event
   behind it, so the fallback is not symmetric.
3. **Whether fleet-wide versioning will chafe.** §2 says two projects are never on
   different versions; the `--project` filter is the mitigation and it is unmeasured.
4. **Whether `release`-authored versions (§6.1) read as signal or noise.** Believed
   signal — it is the only record of a behaviour change — but nobody has lived with it.
5. **`SAFETY_KEYS` is drawn by intuition.** `settings_overrides` can add `deny` rules (see
   `INV-GATE-DENY-CONFLICT`) and arguably belongs in it.
6. **No cost model for the per-tick `stat`.** Believed negligible; unmeasured.
7. **Secrets.** Nothing in the catalog is a secret today — Telegram credentials are env
   var *names* (`telegram_token_env`), not values. If a key ever holds a value, this
   ledger is the wrong place for it and nothing here guards against that.
