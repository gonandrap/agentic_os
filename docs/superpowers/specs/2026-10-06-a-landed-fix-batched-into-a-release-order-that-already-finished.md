# A landed fix batched into a release order that already finished

GitHub issue #945, work order wo-38cead7e. Real occurrence: wo-29f44978 (the release order
that swallowed the fix) and issue 903 / PR 921 (the fix it swallowed).
Design decided by Neo question 1335 — both halves below are settled, not reopened here.

Concurrent, same function: wo-39ffce4b (issue 934) adds a guard clause ABOVE
`ensure_release`'s loop and a helper BELOW the function. §1.4 says how this diff stays out
of its way.

## The problem

**A fix that lands joins a release order whose worker has already finished, and is never
released.** The batch grows after the only thing that could read it has stopped.

`Daemon.ensure_release` (src/jarvis/daemon.py:7860, called from `Daemon.sync_issues`
src/jarvis/daemon.py:7755) walks `store.list_work_orders(statuses=OPEN_STATUSES,
include_hidden=True)` and, for the FIRST candidate carrying a `RELEASE_BATCH_KEY`
(`"release_for_issues"`, src/jarvis/release.py:77) list, appends the landed fix's issue url
to that batch and `return`s — src/jarvis/daemon.py:7883-7900. The `return` is
unconditional: there is no test of whether that candidate can still ship anything.

`OPEN_STATUSES` (src/jarvis/project_store.py:64-65) is `pending`, `dispatching`,
`running`, `idle`, `waiting_input`, `validating`, `needs_review`, `waiting_pr_merge`,
`budget_exhausted`. Four of those are states a release order reaches only AFTER its worker
delivered. "Open" answers "is this order settled"; `ensure_release` needs the answer to a
different question — "can this order still put a commit in a tag" — and has been reading
the first as if it were the second.

Measured, 2026-10-06:

1. **wo-29f44978** — a release order whose own payload (issue 837) was already live. Its
   worker delivered `"No release cut: nothing to ship"` and the order parked in
   `needs_review` holding one pending assumption saying no release is needed.
2. **Three minutes later** the daemon closed issue 903 — an expedited fix, PR 921, merge
   commit `667bd494` — and `ensure_release` appended it to wo-29f44978's batch, because
   `needs_review` is in `OPEN_STATUSES`. The worker never saw it; its turn was over.
3. `667bd494` is in no `jarvis-*` tag. The user accepting that assumption completes
   wo-29f44978 through the review path, and issue 903 is released by nothing.

Nothing re-files it. `ensure_release` fires only on the issue-state TRANSITION — the
`applied == issues.CLOSED` branch at src/jarvis/daemon.py:7751 — so the landing signal is
spent exactly once. `hold_red_release`'s docstring (src/jarvis/daemon.py:7935-7939) already
names this trap in prose for its own case: *"`ensure_release` fires only on the issue-state
TRANSITION, so a release skipped because `main` was red would never be filed again and the
fix would drop out of every future batch."* The same sentence is true of a batch appended
to a finished order, and the code does not defend against it.

Root cause, both layers, because the fix needs both:

* **Selection.** `ensure_release` picks a batch target by SETTLEMENT status, which does not
  imply a live worker. That is the bug that created the drop.
* **No reader of the other end.** A batch entry has no post-condition. `_release_payload`
  (src/jarvis/daemon.py:8149) and `_settle_shipped_release` (src/jarvis/daemon.py:8106) ask
  whether a tag carries a batch in order to CLOSE an order; nothing ever asks whether a
  settled order's batch was carried. So any route to a drop — this one, a cancelled
  release, a failed one — is permanent and silent. Fixing selection alone leaves issue 903
  stranded for ever, because the user-accept review path completes wo-29f44978 without ever
  entering `settle_shipped_releases`.

## The fix

### 1. Prevention — a fix may only join an order that can still ship it

#### 1.1 The discriminator is a `finished` event, not a status list

A release order can still ship a fix for exactly as long as its worker has not delivered.
The recorded fact for that is a `finished` event on the candidate, written by
`ops.finish_work_order` at src/jarvis/ops.py:6315 — i.e. by `jarvis wo finish`, which every
delivery goes through.

```python
if store.events_of_kind(candidate["id"], "finished"):
    continue          # its worker is done; it can ship nothing more
```

One test covers `needs_review`, `waiting_pr_merge`, `validating`, `budget_exhausted` AND
wo-29f44978's "nothing to ship" abandonment, because all five are reached through a
delivery. Enumerating statuses instead would be wrong in both directions: it would have to
list four names that drift, and `budget_exhausted` is reachable BEFORE a delivery (a worker
that ran out of money mid-task is resumable by a raise and can still ship), where the event
test correctly keeps such a candidate eligible.

Rejected: **`wo.get("result_summary")`** — written by the same call but also by the
review/settle paths, so it is a weaker proxy for the same fact with no upside.
Rejected: **asking whether a turn is in flight** (`running_turns`) — an `idle` release order
between turns is still shippable, and that test would send its fix to a fresh order.

#### 1.2 Skip and keep looking; no candidate means a fresh order

A finished candidate is SKIPPED and the loop continues to the next batch-carrying
candidate. Batching is unchanged for every order that is still live — two blockers ten
minutes apart still make one release. When no candidate can still ship, control falls
through to the existing `store.create_work_order` tail (src/jarvis/daemon.py:7902-7913)
untouched: the fix earns a fresh release order with the same brief, the same
`release_batched` event and the same back-link.

#### 1.3 Url-level idempotency survives, and it is read BEFORE any write

A url already present in the batch of SOME open order — finished or not — is never added
twice and never earns a second release order. So the loop is two passes over one read:

1. **Pass one, read-only:** if `url` is in any open candidate's batch, `ensure_release`
   **returns that candidate's id** and writes nothing — no metadata update, no
   `release_batched` event, no new order. That is today's behaviour for the already-present
   case (src/jarvis/daemon.py:7889 + :7900) and it must be preserved verbatim, including
   when the holder is finished: the sweep in §2 is what rescues a url stuck in a finished
   order's batch, and a second release order filed here would race it.
2. **Pass two:** the first candidate with no `finished` event gets the append, the event and
   the log line exactly as now.

Both passes read the one `list_work_orders` result, so there is no extra query. The scope of
"some open order" is deliberate and load-bearing for §2: a url in a SETTLED order's batch
does NOT block filing, which is precisely what lets the sweep re-file.

#### 1.4 Cost, and keeping the diff mergeable with wo-39ffce4b

`events_of_kind` is called only after the metadata parse has confirmed a batch, so the per-
tick cost is one indexed read (`idx_events_wo`, src/jarvis/project_store.py:1112) per
batch-carrying open order — in practice zero or one. Every other open work order costs
exactly what it costs today.

The diff replaces the `for candidate in …` block, src/jarvis/daemon.py:7883-7900, and
nothing else. Not the `url`/`reason`/`line` preamble above it, not the create-fresh tail
below it, not the signature. The one shared line is the docstring: append the new rule as a
final paragraph; if wo-39ffce4b also appends there, keep both paragraphs.

### 2. The safety net — a settled release order's batch is checked against the tags

The issue's last sentence: *settling a release order must never drop a fix from its batch
that no live tag carries.* §1 stops new drops; it recovers nothing, and issue 903 is
stranded today. A reconcile-side sweep re-files any batch entry of a SETTLED release order
that no `jarvis-*` tag contains, so the OS self-heals instead of a person noticing.

#### 2.1 Where it lives and where it is called from

New method `Daemon.refile_dropped_fixes(project, store)`, placed in src/jarvis/daemon.py
AFTER `_merge_commit_of` (i.e. after src/jarvis/daemon.py:8207) with its constants beside
`OVERTAKEN_EVENT` / `MERGE_COMMIT_EVENT` (src/jarvis/daemon.py:8053-8058). After, not
immediately below `ensure_release`, for §1.4's reason: wo-39ffce4b is adding a helper in
exactly that gap.

Called from the tick's release band, immediately after `self.settle_shipped_releases(project,
store)` (src/jarvis/daemon.py:915) and therefore on the `poll_prs` beat
(`PR_POLL_EVERY_TICKS = 24`, ~2 min, src/jarvis/daemon.py:108). Three reasons, in order of
weight:

* **Same beat as the step that creates the condition.** `settle_shipped_releases` is one of
  the two things that settle a release order; running in the same band means a drop is seen
  within one poll interval of the settlement. AFTER it, never before — an order that settles
  on this tick must be read in its settled state, by the next pass.
* **It can make a `gh` call.** `_merge_commit_of` back-fills a merge commit through
  `github.pr_view` for a fix that merged before the poller recorded it. `PR_POLL_EVERY_TICKS`
  is the cadence that owns leaving the machine; the 30-second reconcile beat
  (`RECONCILE_EVERY_TICKS = 6`) does not.
* **It repeats work already paid for.** `release.overtaken_by` fetches tags in the project
  checkout; pairing this sweep with `settle_shipped_releases` keeps all tag reads on one
  beat instead of spreading them over two cadences.

Scoped like both its neighbours — `if project.name != self._os_owner(): return` as the first
statement (`hold_red_release` src/jarvis/daemon.py:7963, `settle_shipped_releases`
src/jarvis/daemon.py:8086). The sweep reads `jarvis-*` tags and
`paths.production_code_dir()`: facts about this repository, not about a project.

Rejected: **a new cadence constant of its own.** Nothing here expires; the sweep is pure
catch-up, and a third release cadence would have to be explained against the two that exist.
Rejected: **doing it inside `settle_shipped_releases`.** That function's contract is "end an
order whose batch shipped" and its selection is `OPEN_STATUSES`; a second selection over
settled orders inside it would make its docstring false and its cost guards unreadable.

#### 2.2 The query, and the cost bound

It must never parse the metadata of every settled work order in the project. New
`ProjectStore` method, beside `unsealed_terminal_orders` (src/jarvis/project_store.py:2580),
pushing both filters into SQLite:

```python
def settled_release_orders(self, since: float,
                           limit: int = 20) -> list[dict[str, Any]]:
    """Settled orders carrying a release batch, newest settlement first."""
    # status is indexed (idx_wo_status); the batch test runs in SQLite, so no row's
    # metadata is parsed in Python unless it is a release order inside the window.
    SELECT * FROM work_orders
     WHERE status IN ('completed', 'failed')
       AND updated_at >= ?
       AND CASE WHEN json_valid(metadata)
                THEN json_extract(metadata, '$.release_for_issues') IS NOT NULL
                ELSE 0 END
     ORDER BY updated_at DESC LIMIT ?
```

* `json_valid` in a `CASE`, not a bare conjunct: `stale_autopsy_orders`
  (src/jarvis/project_store.py:2630-2638) is the precedent, and `json_extract` on garbage
  errors rather than returning NULL.
* `'$.release_for_issues'` is the JSON path of `release.BATCH_KEY`. The literal appears
  here because a SQL path cannot be interpolated from the constant without defeating the
  statement cache; the constant's docstring must name this query as a second reader.
* **`cancelled` is excluded.** A cancelled release order is the user stopping a release;
  re-filing one would overrule them. `completed` is wo-29f44978's path and `failed` is a
  release order that died holding fixes — both are drops the user never chose.
* **Window:** `Daemon.RELEASE_REFILE_WINDOW_SECONDS = 14 * 86400`, read as
  `db.now() - RELEASE_REFILE_WINDOW_SECONDS`. A release order settles within minutes to
  hours of its batch landing; two weeks covers a `needs_review` order the user leaves over
  a holiday and bounds the scan to a handful of rows on this repository's history. It is a
  constant, not a catalog setting, for `PR_POLL_EVERY_TICKS`' recorded reason.
* **Steady-state cost:** one indexed query plus one `events_of_kind` read per not-yet-cleared
  row. Once every url of a batch is accounted for, the sweep writes
  `RELEASE_BATCH_CLEARED_EVENT = "release_batch_cleared"` on that order and skips it on
  every later pass with **zero** git and zero `gh` work. A quiet fleet therefore pays one
  query per poll and nothing else.

#### 2.3 Per url, not per batch — and `_release_payload` is the wrong granularity

For each url in a settled order's batch, in order:

1. **Already handled?** Skip if `store.events_of_kind(wo_id, RELEASE_REFILED_EVENT)` holds
   an event whose payload `issue_url` is this url. That event is the marker that makes the
   sweep file **at most once per url**.
2. **Which commit?** Map url to its fixing order through the `release_batched` events —
   the same mapping `_release_payload` builds (src/jarvis/daemon.py:8158-8162) — then
   `self._merge_commit_of(project, store, fix)`. `_merge_commit_of` already caches its
   back-fill on `MERGE_COMMIT_EVENT`, so this is one `gh` call per fix, once ever.
   **`_release_payload` itself is NOT reused: it returns `None` when ONE entry is
   unresolvable**, which is right for its caller (a partial payload must never complete an
   order) and wrong here, where each url is decided on its own and one unreadable entry must
   not hide a genuine drop in the entries beside it.
3. **Did it ship?** `release.overtaken_by(project.path, [sha])` — one sha, which is what
   per-url granularity means. Then:
   * `found.error` — **skip, write nothing, retry next poll.** Fail closed: an unreadable
     tag list must never be read as "not shipped", or a `git fetch` failure files a release
     order per poll.
   * `found.tag` non-empty — the commit IS in a `jarvis-*` tag: nothing to re-file. Whether
     production is on that tag is the deploy question, and it belongs to
     `_settle_shipped_release` / `release.verify_on_boot`, not here. The entry counts as
     accounted for.
   * no containing tag — **the drop.** Re-file, §2.4.
4. **Clear the order** with `RELEASE_BATCH_CLEARED_EVENT` once every url is accounted for
   (tag-carried or re-filed). An url left unresolved at step 2 or skipped at step 3 blocks
   the clear, which is correct: the order stays in the window and is looked at again.

#### 2.4 What the re-filed order looks like

The sweep calls `self.ensure_release(project, store, fix)` with the FIXING order — the same
entry point the landing signal uses. So the brief (`RELEASE_BRIEF`), the `release_batched`
event, the per-fix line with its expedited/Neo-confirmed reason, and the back-link are
unchanged by construction, and §1's selection applies: the entry joins a still-live release
order if one exists, otherwise it earns a fresh one.

Two events carry the trace, written AFTER `ensure_release` returns the target id:

* on the DROPPER: `RELEASE_REFILED_EVENT = "release_refiled"`, payload
  `{"issue_url": url, "release_wo_id": target, "fix_wo_id": fix["id"]}` — the dedupe marker
  of §2.3.1 and the record that this order dropped something;
* on the TARGET: `"release_refiled_from"`, payload `{"issue_url": url, "dropped_by": wo_id}`
  — so the new release order says where its entry came from and the user can walk back to
  the order that lost it.

Crashing between the `ensure_release` call and the marker is safe, and this is the second
thing §1.3's ordering buys: on the next poll the url is already in an open order's batch, so
`ensure_release` writes nothing and returns that order's id, and the marker is then written.

Not done, deliberately: **the pending assumption on wo-29f44978 is untouched** (Neo 1335 —
it is a true statement about that order's own payload), and **no finished worker is woken**.
The new-order route is Neo's choice: a finished session re-sent its whole conversation to be
told about a fix it never read is both the dearest option and the one that reopens a settled
order.

### 3. Test plan

All in **tests/test_release_overtaken.py** — it already owns this machinery and its helpers
(`release_order`, `batch_in`, `land`, `tag`, `deploy`, `step`, real local git repositories
and real `git tag --contains`). New cases:

1. **A finished candidate is skipped and a fresh order is filed.** `release_order(…,
   status="needs_review")` plus a `finished` event on it; drive `ensure_release` with a
   landed fix. The finished order's batch is unchanged, a NEW release order exists carrying
   the url, with the brief and a `release_batched` event. wo-29f44978 reproduced.
2. **A still-running candidate still batches.** Same fixture without the `finished` event:
   the url joins it, one `release_batched` event, no second order. The property §1 must not
   break.
3. **No double-add.** The url already in a candidate's batch — once with that candidate
   finished, once running — adds nothing, files nothing, and returns that candidate's id.
4. **The sweep re-files an unshipped entry exactly once.** A `completed` release order whose
   batch holds a url whose merge commit is in NO tag. First pass: a new release order
   carries the url, `release_refiled` on the dropper, `release_refiled_from` on the target.
   Second pass: nothing new written, no second order.
5. **The sweep files nothing when a tag carries the entry.** Same order with the merge
   commit tagged `jarvis-0.10.24`: no new order, no `release_refiled`, and the batch is
   cleared so a third pass does zero git work (assert via the tag-read count or a monkey-
   patched `release.overtaken_by` call counter).
6. **A project that is not the OS owner pays nothing.** Mirrors
   `test_a_project_that_does_not_own_the_os_pays_nothing`: no query, no git, no writes.
7. **The tick wires it after the settlement step.** Mirrors
   `test_the_tick_runs_the_step_after_the_issue_sweep` — order matters per §2.1.
8. **An unreadable tag list files nothing.** `overtaken_by` returning `error` leaves the
   record untouched and no marker, so the next poll retries.

`uv run pytest tests/ evals/` before the pull request.
