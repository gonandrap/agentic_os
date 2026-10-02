# Order autopsy durability: write down how an order ran, before the transcript expires

Feature order `fo-5242ee99`. Spec written by the planner, 2026-09-27. The parent spec is
`docs/specs/2026-09-24-order-observability.md` and every section reference of the form
"the parent spec's section N" points there.

Every debug payload the order-observability feature renders — turn anatomy, tool profile,
token classes, context total, delta, peak and composition, which are sections 3, 4, 6 and 7
of the parent spec — is derived AT READ TIME from the Claude Code transcript on disk. When
the transcript expires the autopsy is gone and every surface correctly reports "not
recorded". The order's record survives; the account of how it ran does not. This feature
writes the autopsy down while the transcript is still there.

Sections 3, 4, 5 and 6 are the work, one work order each, in that dependency order.
Sections 1 and 2 are the background every child needs and belong to nobody. Section 7 is
the boundary.

---

## 1. What is lost, and why reading harder cannot recover it

The order's record survives an expired transcript intact: its state, its timeline, its
messages, its assumptions, its bill. What does not survive is the account of how it ran.

Four surfaces go blank, and they go blank together because they all read the same file.
`jarvis inspect` loses the per-turn wall-clock partition, the tool profile, the classified
cache writes and the context peak. `jarvis wo why` loses the clock half of its diagnosis.
The debug page at `GET /wo/{name}/{wo_id}/debug` loses the whole anatomy block, which is the
third of its four blocks. And the evidence packet that a MODEL reads —
`src/jarvis/supervisor.py:351`, where `_session_lines` feeds the supervisor's packet — loses
the lines that tell it what the worker actually did, which is the one consumer here that
cannot ask a follow-up question.

Reading harder does not fix this, and the reason is worth stating so nobody spends a session
on it. The file is gone: there is no colder store behind it, no compacted form, no partial.
And the only alternative to reading it — inferring a number from a duration, so that a turn
of known length is reported as having generated for that length — is the exact defect issue
#227 records. The parent spec's section 2 states the rule it produced: absent is not zero.
So the loss cannot be read around and cannot be guessed around. It can only be written down
in time.

## 2. What already exists, the two standing rules, and the authority split

**The two standing rules. They are quoted here once and every section below is bound to
them.**

> (a) A persisted figure and a freshly derived one must NEVER disagree on the same order.

> (b) An order whose autopsy was never persisted renders as *not recorded*, never as zero.
> That is issue #227's rule.

**The blueprint is the sealed bill, for an identical reason.** Knowledge entry `kn-3629fa87`
records it: every source a bill is built from expires, so an order costed on demand gets
CHEAPER as it ages, not because it spent less but because the evidence went away. The
mechanism to copy is `work_orders.bill_json` plus `bill_sealed_at`, written by
`Daemon.seal_bills` (`src/jarvis/daemon.py:917-958`) on the reconcile tick, with
`bill.PAYLOAD_VERSION` and `bill._upgrade_seal` (`src/jarvis/bill.py:836-896`) carrying
versioned re-derivation. Read `bill.py` before writing any of this; it is the working
precedent and not merely an analogy.

**Part of the autopsy is already durable, and this feature does not re-persist it.** The
parent spec's section 5 already persists the context ledger as `wo_turns.context_json` and
per-turn accounting as `wo_turns.usage_json`, both in the per-project database
`<project>/.jarvis/jarvis.db`, both documented "NULL means not recorded ... never zero" at
`src/jarvis/project_store.py:888-935`. The genuinely transcript-only parts, and therefore
the whole of what this feature seals, are the parent spec's section 4 turn anatomy, the tool
profile, `classify_writes`' causes, and `context_report`'s `prefix_break` join.

**The authority split. This sentence must survive into the code as a comment.**

> The BILL is the authority for MONEY, because it prefers the result envelope via
> `ops._turn_usage`; the AUTOPSY is the authority for the CLOCK and for context, because it
> is transcript arithmetic.

They already disagree today on real orders, and the bill already discloses that it does.
Without that sentence written down where a reader of either module will hit it, a later
worker will "fix" the disagreement and break one of the two.

## 3. The seal: an `autopsy_json` column pair and the daemon step that writes it

Child 1, key `seal`. This section builds the writer and nothing that reads it.

**Where.** A nullable column PAIR on `work_orders` in the per-project database:
`autopsy_json` and `autopsy_sealed_at`, following `bill_json` and `bill_sealed_at` exactly.
It reaches a live database only through `ProjectStore._migrate` and `ADDED_COLUMNS`
(`src/jarvis/project_store.py:1039`, with the `wo_turns` block at
`src/jarvis/project_store.py:1173-1205` as the shape to copy). NO new table.

**There is NO `feature_orders` autopsy column, and that is decided.** A feature's autopsy is
read by reading each child's seal. State the cost plainly rather than leaving it to be
discovered: `ProjectStore.delete_work_order` (`src/jarvis/project_store.py:258`) erases a
child outright, so a feature that references its children's seals loses that unit where a
nested copy would have kept it. That loss is accepted, rather than nesting one to two
megabytes per child inside the parent.

**When.** A new `Daemon.seal_autopsies` tick step beside `seal_bills`, on SETTLED orders
only and never on a running one. Copy three things from `seal_bills`: a bounded batch, ONE
hoisted `usage.index_sessions()` per batch (`src/jarvis/daemon.py:940`), and on exception
seal an ERROR payload rather than park the offender at the head of the queue for ever
(`src/jarvis/daemon.py:947-950`). The queue is its OWN query, predicated on `autopsy_json IS
NULL` — NOT `unsealed_terminal_orders`, whose predicate is `bill_json IS NULL`. Sharing it
would let a bill failure silently stop autopsies.

**What.** A new module `src/jarvis/autopsy.py`, owning `PAYLOAD_VERSION`,
`to_seal(anatomy, *, level) -> dict` and `from_seal(payload, *, spans) -> Anatomy`.

**The seal is NOT `Anatomy.as_dict()`, and this is the trap the section exists to avoid.**
Measured: `Turn.as_dict` (`src/jarvis/inspection.py:691-722`) emits `"api_calls":
len(self.calls)` and a folded `"usage"`, and carries neither `Turn.calls` nor `active_ended`.
Rehydrate from it and `observed` goes False, `usage` empty, `context_peak` 0, `generating` 0
with the remainder falling into `unaccounted`, and `cache_ttl()` and `rewrite_excess()` all
zeros — so `src/jarvis/supervisor.py:366` would print the exact string `NO API CALL WAS EVER
MADE — it cost nothing. ` for a turn that made 32 calls. That is issue #227 recreated inside
the persistence this feature exists to add. So `to_seal` and `from_seal` are a dedicated
round-trippable pair carrying turn boundaries, `active_ended`, `triggers`, `spans`, `calls`,
classified `writes` and `subagents`, and `as_dict` stays the SINGLE render contract, computed
FROM the rehydrated object.

**Seal only what expires, and say why for each thing that is not sealed.** Holds are not
sealed, because `holds.held(store, wo_id)` reads the OS's own per-project database, which
never expires — sealing would freeze an episode that was OPEN at seal time. `from_seal` takes
`spans=` exactly as `read_session` does and re-runs `inspection._attach_holds`, keeping
`held`, `active` and `unexplained` live. `wo_turns.usage_json` and `wo_turns.context_json` are
not sealed: already durable, per section 2. The module legends `hold_causes`, `PARTS`,
`subagent_depth_read` and `param_caps` are not sealed either: they are re-derived at render
from the module, so a raised cap is reflected on an old seal.

**What a `normal` seal carries, so section 6 is the ONLY difference between the levels.**
Sealed at `normal`: turn boundaries, `active_ended`, `triggers` (the prompt text that started
each turn), span BOUNDARIES (tool name, start, end, seconds), `calls`, classified `writes` and
the subagent shape. NOT sealed at `normal`: span `params`, and nested subagent params — section
6, and the whole of what the level buys. Triggers and span detail are user-authored text and CAN
carry a secret, so the redaction battery of section 6 runs over a `normal` seal TOO: the
bash-token and credential-named-key cases must be absent from `json.dumps(to_seal(a,
level="normal"))`. A `normal` seal leaking a secret would be the same permanent store with none
of the gate.

**The payload records `autopsy_level` from day one, even though this child has no gate.** The
level is the CALLER'S argument: with no gate this child's `seal_autopsies` can only pass
`normal`, and section 5 is what makes `full` reachable by passing `level_for`'s answer through.
Without it, "no params" is ambiguous between "sealed at `normal`" and "this order ran no
tools", and that ambiguity is rule (b) reappearing one level down. A seal written before the
field existed reads as UNKNOWN, a third state distinct from `"normal"` and `"full"`.

**The payload records the floors it was taken at:** `write_floor` and `join_floor`. The
defaults are `report_write_floor` 20000 and `report_join_floor` 30 at
`src/jarvis/catalog.py:559` and `src/jarvis/catalog.py:564`. A seal taken at 20000 holds NO
write below 20000, ever.

**The caps. These are the spec's values, not suggestions.**

- `TURN_SPAN_LIMIT = 500`, keeping the dearest by seconds, with `spans_folded` carrying count
  AND folded seconds per tool name, so the folded seconds RECONCILE against `Turn.tools`.
- `TURN_CALL_LIMIT = 200`, reusing the value and the name from `src/jarvis/bill.py:63-69`,
  dearest per turn. Folding calls changes `usage`, `context_peak`, `cache_ttl()` and
  `rewrite_excess()`, so the folded remainder must be carried as NUMBERS or those four become
  lies. Nothing is ever silently dropped.
- A per-order payload ceiling of 1 MB, with an explicit DROP ORDER: `params` first, then
  subagent params, then subagent spans, then spans past the per-turn limit. Each drop is
  announced the way `params_truncated` and `params_dropped` already are. The `params` rung
  ships present but unreachable in this child, because params only exist at `full`, which is
  section 6; its test belongs to section 6.
- `SUBAGENT_LIMIT` per turn, value 20, stating how many were not sealed. Depth is already
  bounded by `SUBAGENT_DEPTH_READ`; breadth was not.

**The versioned re-seal, and the one place a naive copy of the bill is wrong.** Model it on
`bill._upgrade_seal` (`src/jarvis/bill.py:836-896`) and `bill._corrects_a_reading`
(`src/jarvis/bill.py:899-936`), and carry the lesson at `src/jarvis/bill.py:916-920`: compare
by STRUCTURED COUNTS, never by rendered prose. The difference is that an autopsy has NO
monotone quantity. A parser fix can yield FEWER spans; a raised floor can yield FEWER writes.
So the adoption rule is: `found` is true AND turns, spans, calls and writes are EACH greater
than or equal to the seal's, COMPARED AT THE SAME FLOORS. A fresh read at a higher floor
showing fewer writes is a floor artefact and is REFUSED. On adoption, `autopsy_sealed_at` is
PRESERVED and a `resealed_at` is added, exactly as `src/jarvis/bill.py:881-888` does.

**The gate seam, and it ships CLOSED.** This child ships
`autopsy.records_autopsy(wo, cfg) -> bool` as a named one-line function returning **False**,
called from `seal_autopsies`. Section 5 replaces its body with the real level check, and until
section 5 lands this writer seals NOTHING for anybody: it is complete, dark, and exercised only
by tests that force the predicate. This is the user's ruling and it is not a trade-off to be
re-opened — no autopsy is sealed fleet-wide ahead of the gate that governs it, whatever the
argument that tier-1 retains no content. A test asserts the SHIPPED predicate returns False, so
the dark state cannot be undone by accident.

**Acceptance.**

The round trip, with the correction that makes it true:
`from_seal(to_seal(a, level=...), spans=a.holds).as_dict() == a.as_dict()` over the committed
fixture session. `spans` is REQUIRED, so write the wrong form as
`from_seal(to_seal(a), spans=[]).as_dict() == a.as_dict()`: it is FALSE the moment a
hold exists, because `Anatomy.as_dict` (`src/jarvis/inspection.py:913-944`) emits `holds`,
`held_by` and a `partition` whose `active` and `held` buckets are hold-derived. So assert it
in BOTH shapes: with `a.holds == []` the equality holds with no exclusions; with holds
attached the equality holds after re-injecting the same spans. The seal must NOT carry them;
the rehydrate must ACCEPT them.

Alongside it: `back.found is True`; per-turn `len(t.calls)` preserved and summing to 68;
per-turn `usage.as_dict()` equality, not only the folded total; `cache_ttl()` and
`rewrite_excess()` equality; and the three measured writes surviving BY CAUSE exactly as
`tests/test_inspection.py:82` pins them — `(45_169, 0, COLD_START)`, `(157_098, 15_862,
PREFIX_MISS)`, `(193_139, 0, TTL_EXPIRY)`.

`to_seal(a) != a.as_dict()` and `"calls" in seal["turns"][0]`, so a later refactor cannot
collapse the two.

The older-row pair that `kn-c712a5d6` demands: copy the shape of
`tests/test_schema_upgrade.py::test_a_turn_that_predates_the_context_ledger_reads_as_not_recorded`
(line 483), which builds a database from the frozen asset
`tests/data/schema-jarvis-0.1.11.sql` and asserts the new column reads `None` and the read
path says so in words.

Every cap PROVEN on a synthetic transcript via the existing `write_transcript` fixture
(`tests/test_inspection.py:230`), which writes under `tmp_path` and never touches
`tests/data/`. NO new committed fixture: the parent spec's section 8 forbids one, and the only
committed transcript,
`tests/data/transcripts/-wo-5a6b2d6d/ec8236c7-b418-4f09-80f0-1edea61f099f.jsonl`, is pinned by
eleven existing tests. Proving a cap means the folded numbers RECONCILE, never that a key
appeared.

The daemon step, with the predicate FORCED TRUE by the test because the shipped one is False:
settled orders only; exactly one `usage.index_sessions()` for a batch; an order whose seal
raises gets an error payload and leaves the queue; a second tick seals nothing. Copy
`tests/test_bill.py::test_the_daemon_seals_every_settled_order_and_only_once`. Beside it, the
test that pins the dark state: with the predicate AS SHIPPED, a settled order's
`autopsy_json` is still `None` after a tick.

Survival is proven by DELETING the `tmp_path` `.jsonl` and re-reading, the idiom of
`tests/test_bill.py::test_an_old_seal_stands_once_the_evidence_is_gone`. Nothing in the suite
can age out a real Claude Code transcript, so the real case is checked by hand on one
production order after release.

**The honest limit of this child.** It CANNOT prove standing rule (a), because rule (a) is a
claim about a SURFACE and no surface prefers a seal until section 4 lands. Its criterion is
the function-level round trip.

## 4. The read side: one chokepoint that prefers the seal, and every surface saying which it read

Child 2, key `read-side`. There is NO single read chokepoint today, one must be manufactured,
and it must NOT be in `ops`.

The production callers of `inspection.read_session` are `ops.inspect_report`'s nested `unit()`
closure (`src/jarvis/ops.py:9478`), `ops.context_report` (`src/jarvis/ops.py:9608`),
`ops._diagnose_holds` (`src/jarvis/ops.py:1515`), `supervisor._session_lines`
(`src/jarvis/supervisor.py:351`), and `inspection.live_alarms`
(`src/jarvis/inspection.py:1544`). `supervisor.py` imports only `claude_cli` and `structured`
at `src/jarvis/supervisor.py:19`, so it sits BELOW `ops` and the helper cannot live in `ops`.
It belongs beside `inspection`, in `autopsy.py`.

`autopsy.anatomy_for(wo, cfg, *, spans, index=None, live=False) -> tuple[Anatomy, dict]`
returns the rehydrated seal when `wo["autopsy_json"]` is set, else `inspection.read_session`,
together with provenance `{"source": "sealed" or "derived", "sealed_at", "level",
"write_floor", "join_floor", "params": bool}` that every surface PRINTS. Every surface saying
which it read is the operational half of rule (a): a disagreement nobody can attribute is a
disagreement nobody can fix. `live=True` copies the parameter and the justification of
`bill.build(..., live=True)` (`src/jarvis/bill.py:804-813`).

`supervisor._session_lines` takes a full store row — it is called with `row` at
`src/jarvis/supervisor.py:599` — so no extra plumbing is needed there.

`inspection.live_alarms` must NEVER prefer a seal. It reads at a deliberately different floor,
`reading = replace(cfg, report_write_floor=cfg.alarm_write_tokens)` at
`src/jarvis/inspection.py:1544`, so a seal taken at `report_write_floor` cannot answer it; and
it is about a RUNNING session anyway.

**The AST pin, so forgetting a call site is impossible.** Copy
`tests/test_stores.py::test_only_db_write_transaction_opens_a_transaction` (line 411), which
walks every `*.py` under `src/jarvis` and carries ONE whitelist expressed as a line range from
`inspect.getsourcelines`. Also write the meta-test that proves the pin bites, in the shape of
`tests/test_supervisor.py::test_the_pin_would_catch_the_move_it_forbids` (line 1084). The
trap: there are TWO functions named `read_session` — `inspection.read_session`
(`src/jarvis/inspection.py:1131`) and `usage.read_session` (`src/jarvis/usage.py:935`). A pin
keyed on `ast.Attribute(attr="read_session")` alone fires on `usage_mod.read_session` at
`src/jarvis/ops.py:9021` and at `src/jarvis/bill.py:1116`, which are a DIFFERENT function on
the bill path and must pass. The pin keys on the MODULE QUALIFIER. Scope it to
`src/jarvis/ops.py` and `src/jarvis/supervisor.py`, and write the `live_alarms` exemption into
a comment anyway, because the next person will widen the scope and needs it already written
down.

**The floors, and the sentence a short list must never be allowed to tell.** `jarvis inspect
--writes-over N` and `--joins-over N` (`src/jarvis/cli.py:403` and `src/jarvis/cli.py:407`)
override the floors per run. Asked for a floor BELOW the sealed one, the surface must SAY the
sealed floor and never hand back a short list that reads as "there were none". Asked for one
ABOVE it, the seal answers and no warning is needed. Both directions are asserted. These two
flags have NO test today, so this is net-new coverage.

**The three-state rendering.** `autopsy_level` is UNKNOWN, `normal` or `full`, and the three
render as three DIFFERENT strings. Do not hard-code two: `full` is unreachable until section 6
and the rendering must already carry it.

**Acceptance.** Rule (a) at a surface, which only this child can prove: run
`ops.inspect_report` twice over one order, once before the seal and once after with the
transcript untouched, and assert the reports are equal once `provenance` is popped, and that
`provenance["source"]` is `"derived"` then `"sealed"`. The same for `ops.context_report`.

Rule (b) at all four surfaces, each saying *not recorded* and never 0 or 0.00, keeping the
three states *not recorded*, *no transcript* and *genuinely zero* distinguishable —
`src/jarvis/ops.py:9481` already builds `Anatomy(session_id="", ...)` for the no-session case
and that path must survive. Extend
`tests/test_ui_debug.py::test_an_order_with_no_session_says_so_instead_of_reporting_zeroes` and
`::test_an_order_predating_the_context_ledger_renders_the_forward_only_note`.
`tests/test_supervisor.py::test_the_work_order_packet_is_byte_for_byte_what_it_has_always_been`
must still pass: that packet is byte-pinned and `_session_lines` feeds it.

## 5. The gate, and the second write it now governs

Child 3, key `gate`. This is the ONLY section that imports `src/jarvis/observability.py`, and
it has NO prerequisite: that module is the parent spec's section 10, and pull request #803
MERGED it to `main` on 2026-09-27. Read it there. Do NOT copy it, vendor it or reimplement
`level_for`.

THIS SECTION IS WHAT TURNS THE FEATURE ON. Section 3's writer ships with its predicate
returning False, so until this child lands no order anywhere gains an autopsy. That is
deliberate.

**The ruling, and it is the USER'S** (Neo question 820, confirmed again on the plan's second
round): `normal` covers the autopsy, and the autopsy is "opt in per project".

**The mechanics, written out because the phrase and the behaviour do not match.** The predicate
is `level_for(wo, cfg) != OFF` — the exact shape `records_context` already has. So the fleet
default `normal` SEALS, and a project declines with `jarvis config set <project>
observability.level off`. That is opt-OUT mechanics under an opt-in name. Where the two
disagree, THIS PARAGRAPH is what gets built, and no test asserts a project must name a level
before its orders are sealed.

**One function keeps that name, not two.** This child replaces the BODY of section 3's
`autopsy.records_autopsy(wo, cfg)` with `observability.level_for(wo, cfg) != observability.OFF`.
It does NOT add a second `records_autopsy` to `src/jarvis/observability.py`: two functions with
one name across two modules is the `read_session` trap of section 4, invited deliberately.

**This child ALSO wires the level through, and without it `full` ships dark.** `seal_autopsies`
calls `observability.level_for(wo, cfg)` ONCE per order and uses the result TWICE: as the gate
(`!= OFF`) and as the argument to `to_seal(anatomy, level=...)`, which records it as
`autopsy_level`. Nothing else owns this. Section 3's writer has no gate, so on its own it can
only ever pass `normal`; section 6 teaches `to_seal` to emit `params` when the level is `full`
but may NOT import `observability.py`, because this section is the only one that does. Leave the
pass-through out and no order in production is ever sealed with `params` or with `autopsy_level`
`"full"` — the user's ask, giving `full` a meaning, would ship dark while every test passed.
Beyond the predicate body and this one argument, nothing about the writer changes.

The parent spec's section 10 said the gate governs exactly ONE write and no read. It now
governs TWO, so its wording, the `ObservabilityConfig` docstring, the `jarvis config` CLI help
and the tests that assert `full` and `normal` are identical are ALL revised here, in the same
breath, rather than left contradicting the behaviour. The docstring must say "`off` stops the
autopsy being sealed" in the same breath as its existing "`off` does not disable `jarvis
watch`, `jarvis inspect`, `jarvis wo why` or the debug page", because those two facts together
are the whole of what `off` means and a reader who learns one without the other draws the wrong
conclusion. And `full` stops being collapsed into `normal`: it now differs by exactly one
thing, the retained content of section 6.

A project with no `observability` block in its catalog gets the fleet default `normal` and
therefore SEALS. Assert that: it is the point where "opt in per project" and "the default is
`normal`" collide, and the assertion is what records that the default wins.

**Acceptance.** A shape test over the revised section 10 text in the idiom of
`tests/test_spec_shape.py`, asserting it names `full` and `normal`, names the retained content,
and contains NO sentence asserting the two behave identically. The config-console pin in the
idiom of `tests/test_supervisor.py::test_every_supervisor_setting_reaches_the_config_console`:
every `ObservabilityConfig` field reaches `jarvis config set` help, and `full`'s help line
DIFFERS from `normal`'s. And the behaviour: level `off` seals nothing, level `normal` seals
tier-1, and the test that previously pinned `full` and `normal` as identical is REPLACED and not
deleted. And the pass-through, end to end through the daemon: a project at level `full` seals
with `autopsy_level` `"full"`, a project at `normal` seals with `autopsy_level` `"normal"`.

This child must NOT touch `CLAUDE.md`: `evals/llm/test_jarvis_judgment.py:24` loads it as a bare
system prompt and LLM-grades 14 scenarios against it.

## 6. Retained detail at `full`: tool parameters and nested subagents

Child 4, key `retained-detail`. What `full` finally means: verbatim tool `params`, and nested
subagent anatomies with their params. NOTHING else changes between the levels.

**The reason it is gated is retained content, not bytes, and the spec says so because the
obvious argument is wrong.** Measured on the committed fixture session
`ec8236c7-b418-4f09-80f0-1edea61f099f` — 3 turns, 46/26/9 spans, 32/26/10 calls, 0 subagents,
81 spans, 68 calls, 3 writes, a 166061-byte transcript: `as_dict` is 30664 bytes, and 26899
with params stripped. Params are 3765 bytes, 12 percent. They are not the fat. What they are is
redacted file contents and Bash command lines in a store that does NOT expire, and a mistake
there puts a credential somewhere permanent. That is the whole of the gate's justification. For
scale: one `Call` is about 148 bytes, so the 68 calls the seal needs add about 10 KB, giving
roughly 37 KB for this 31-minute order and extrapolating to 0.5 to 2 MB for a 40-turn one.

**Redaction is extended, never reinvented.** About 35 redaction tests already live in
`tests/test_inspection.py`: `test_a_secret_in_a_bash_command_never_reaches_the_payload` (1464),
`test_a_secret_in_a_nested_edit_never_reaches_the_payload` (1858),
`test_detail_is_redacted_before_it_is_truncated` (1733),
`test_a_nested_input_is_redacted_before_it_is_capped` (1974). The criterion is that the SEALED
payload passes the same battery: for the bash-token, inline-assignment, nested-dict and
credential-named-key cases at minimum, the secret string is absent from `json.dumps(to_seal(a,
level="full"))`. Both orderings hold in the seal: redact BEFORE truncate, and redact BEFORE cap.
`ParamCaps` (`src/jarvis/inspection.py:395-416`, `per_value` 500, `per_span` 2000, `per_turn`
20000) is untouched and is deliberately NOT a `catalog.InspectConfig` setting.

This child activates the `params` rung of section 3's 1 MB ceiling and owns its test.

**Acceptance.** The same round trip as section 3, now at `level="full"` with params populated.
The `autopsy_level` disambiguation as two tests producing two DIFFERENT rendered strings:
`params == {}` with level `normal` renders *params not recorded at this level*; `params == {}`
with level `full` renders *this order ran no tools*. A seal predating the field renders as
unknown, distinct from both. At `normal` the payload contains NO `params` key at all rather than
empty ones. Nested subagents are built in `tmp_path` via `write_transcript(..., subagents={...})`
and `write_meta` (`tests/test_inspection.py:1573`), which is how every nested test in the file is
already built; the committed fixture is used ONLY for the "changes nothing" assertion, the idiom
of the existing `test_a_meta_only_directory_adds_nothing_to_the_real_session` and
`test_redacting_detail_changes_nothing_in_the_committed_session` — sealing it at `full` changes
none of the eleven pinned numbers.

And the reason this child has an edge on `gate` rather than on `seal` alone: a DAEMON TICK over a
project configured at level `full` writes `params` into `autopsy_json`, and the same tick over a
project at `normal` writes none. Section 3's writer plus section 6's `to_seal` cannot produce
that on their own — only section 5's pass-through can.

## 7. Out of scope, and why

Listed so nobody re-proposes them mid-feature.

**No `feature_orders` autopsy column.** Section 3 states the ruling and the cost it accepts.

**No seal for a RUNNING order, and nothing written per turn at turn end.** A settled order is
read once and a running one is read live, so a per-turn write buys nothing and adds a write path
to the hot path.

**`live_report` does NOT fall back to a seal.** It is a byte-cursor live reader of one file and
the parent spec's section 3 forbids persistence there outright. Its `state: "no-transcript"`
frame gains only a note pointing at `jarvis inspect`. That note is the ONE change this section
names, and it is NOT unowned: the `read-side` child builds it with the rest of section 4's
readers, and the same child owns its test.

**No OTEL.** Declined in the parent spec's section 9 on a measurement.

**No new transcript fixture.** The parent spec's section 8 forbids one and eleven tests pin the
committed session.

**No change to `bill_json`, `bill_sealed_at` or `unsealed_terminal_orders`.**

**No retention policy and no pruning of old seals — stated with the consequence.** A `full`
seal is PERMANENT and holds redacted tool parameters: file contents and Bash command lines.
Nothing in this feature deletes one, and `ProjectStore.delete_work_order`
(`src/jarvis/project_store.py:258`) erasing the whole order is the only removal there is. A
purge command and a retention window are FILED TO THE BACKLOG rather than smuggled in behind a
level name — the mistake the parent spec's section 10 refused to make. A project that does not
want permanent parameter retention sets `observability.level normal`, which seals no params at
all.

**No backfill of orders whose transcripts have already expired.** There is nothing to read, and
rule (b) already renders them correctly.

---

## Agent profile

You are implementing durable persistence of an order's autopsy in `jarvis_os`, a Python OS whose
CLI is the only sanctioned interface to its state. You are building one slice of this feature;
other workers build the others, and you will never see their sessions.

**What you must know about this codebase.**

There are three SQLite databases: `os.db` (central), `neo.db`, and one per project at
`<project>/.jarvis/jarvis.db`, which is the one you write to. `ProjectStore._migrate` and
`ADDED_COLUMNS` are the ONLY way a new column reaches a live database. `src/jarvis/inspection.py`
derives the anatomy, and `Anatomy.as_dict` is a RENDER contract, not a serialisation format.
`src/jarvis/bill.py` is the working precedent for everything you are about to write, and you read
it before you write. `supervisor.py` sits BELOW `ops` in the import order, so a helper both of
them call cannot live in `ops`. Serena's symbol tools and the committed memories `codebase-map`,
`work-order-lifecycle` and `testing` come before grep.

**Conventions.**

Test names are full sentences stating the behaviour. Tests are written FIRST and fail first. A
code comment is ONE line citing the spec section, never the explanation. Reference files by
ABSOLUTE path in chat. House style governs every byte you write.

**Traps, each of which has bitten before.**

Rehydrating an anatomy from `as_dict`. Sealing holds. The two functions named `read_session`. A
cap that announces a key instead of reconciling its numbers. A monotone-quantity assumption in
the re-seal. Comparing rendered prose instead of structured counts. Forgetting that a new column
is untested until a test writes it AND a test reads a row older than it.

**What you must never do.**

Never render an absent reading as 0 or 0.00. Never let a persisted figure and a derived one
disagree without the surface saying which it read. Never silently drop data a cap folded. Never
write anywhere under `~/workspace/production`. Never run the full test suite: run targeted tests
and let CI run `pytest tests -q` and `pytest evals -q`. Never make `inspection.live_alarms` prefer
a seal. Never add a committed transcript fixture. Never widen your section's scope into a
sibling's — ask Neo instead.
