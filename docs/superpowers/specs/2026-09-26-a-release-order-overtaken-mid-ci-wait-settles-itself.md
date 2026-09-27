# A release order overtaken mid-CI-wait settles itself

GitHub issue #784, work order wo-521c9225. Evidence: kn-4f5aaa2b, kn-a14fdd47, kn-179cd767.
Design already decided by Neo question 751 (option B) — §2 records it, it is not open here.

## The problem

A release work order the daemon filed itself stays open after the release it was going to
cut has already shipped, and a worker turn is spent closing it by hand.

`Daemon.ensure_release` (src/jarvis/daemon.py:6434, called from `sync_issues`
src/jarvis/daemon.py:6336) files one work order per confirmed critical/blocker fix that
lands, marked by `metadata[Daemon.RELEASE_BATCH_KEY]` (`"release_for_issues"`,
src/jarvis/daemon.py:6409) — the list of issue URLs it is shipping. Its worker runs
`scripts/shipit.sh --stage` behind a gate (`Daemon.RELEASE_BRIEF`,
src/jarvis/daemon.py:6416) and must first wait ~9 min for CI on the merge commit. That
wait is the window: another release path cuts a `jarvis-*` tag that already carries the
same fix, and nothing in the OS notices.

Twice, measured:

1. **wo-e5816dd7** — jarvis-0.10.21 went out during the wait. Gate 215 was then denied by
   Neo because the evidence in the request had gone stale. The order sat open.
2. **wo-4301a7a6** — jarvis-0.10.24 was cut during the CI wait, carrying merge commit
   `0527b5d`, which is this order's own payload. The order sat open.

Both times the ending was a worker turn: fetch tags, compare ancestry, conclude the fix
already shipped, close the order. That is the OS asking a model to do arithmetic on git
refs, and paying a whole conversation re-send for it.

Root cause, stated plainly: **a release order has no definition of done that is not "my
own worker cut a tag".** The only settlement path is the marker handshake —
`release.maybe_restart` / `release.verify_on_boot` (src/jarvis/release.py:206, :250),
whose marker names the shipping `wo_id` — so a release carrying the batch that this order
did not itself cut settles nothing. Two consequences the record already shows:

* nothing reads the batch back. `RELEASE_BATCH_KEY` is written by `ensure_release` and
  never queried outside that function (its own comment says so), so no code can ask "has
  this batch shipped";
* the batch's commits are not written down anywhere. `ops.complete_merged`
  (src/jarvis/ops.py:4788) records `head_oid` on the `pr_merged` event, and `head_oid` is
  `headRefOid` (src/jarvis/github.py:291) — the tip of the PR BRANCH.
  `automerge._merge_args` (src/jarvis/automerge.py:394) merges with `--squash`, so that
  sha is **never** an ancestor of `main`. The commit that is on `main` is
  `mergeCommit.oid`, which is not in `github.PR_FIELDS` (src/jarvis/github.py:201) and is
  recorded nowhere in the OS today.

And the two tests a reader reaches for first are both wrong. kn-4f5aaa2b is explicit:
`main` was 5 commits ahead of the tag and that decided nothing, because release tags are
DESCENDANTS of `main`, never ancestors (kn-179cd767). "The tag list has not changed" is
equally worthless — the tag exists, it is whether it CONTAINS the payload that is the
question.

## The fix

### 1. What "done" means (Neo question 751, option B — settled, not reopened here)

A release order is complete when **a `jarvis-*` tag contains every payload commit of its
batch AND the production checkout runs that tag.** A release order is done when the fleet
RUNS the fix, not when a tag exists: the order was filed because the fleet owed the user a
release, and a tag nobody deployed does not pay that debt.

While a tag contains the payload but production is still behind, the order stays OPEN and
the OS records **one** timeline event naming that tag — once per tag, never once per tick
(§6).

### 2. The payload commit gets recorded where it is already read

Add `mergeCommit` to `github.PR_FIELDS` and `merge_commit_oid: str = ""` to
`github.PullRequest`, filled in `github.pr_view` (src/jarvis/github.py:377) from
`payload.get("mergeCommit", {}).get("oid")` — a nested object, so the extractor is one
`or {}` guard, and `""` when GitHub did not answer (an open PR has no merge commit, which
is not an error).

`Daemon.poll_pull_requests` (src/jarvis/daemon.py:4536) already calls `github.pr_view` on
every parked order and already sees `pr.merged`, so it passes
`merge_commit=pr.merge_commit_oid` to `ops.complete_merged`, which puts it on the
`pr_merged` event payload beside the existing `head_oid` (src/jarvis/ops.py:4831). **Zero
extra network** — one more field on a round trip already being made — and the field lands
on the one path that knows the fact while it is true, which is the reason `head_oid` is
recorded there (that docstring, verbatim).

`head_oid` stays. It is read by the auto-merge head comparison and by `landing.assess`;
this is a second, different fact and the docstring must say which is which in one line, or
the next reader will use the wrong one — that confusion is the whole of this defect.

**Fallback for a fix that merged before this ships** (wo-4301a7a6's own batch has no
recorded sha, so the path is dead on arrival without one): when a batched fix's newest
`pr_merged` event carries no `merge_commit`, the step makes ONE `github.pr_view` call on
that order's `pr_url`, in the project checkout, and — if it answers MERGED with a sha —
writes a `pr_merge_commit_recorded` event on the FIXING order carrying it. So the back-fill
costs one `gh` call per fix, once ever, not once per tick. `gh` unreachable, not merged, or
no sha: the fix is UNRESOLVED and §7 applies. Every batched fix has a pull request —
`ensure_release` only fires behind `ops.routes_on_pull_request` — so there is no
no-PR branch to design.

### 3. The tests that decide, and which checkout each runs in

New in src/jarvis/release.py, beside `_tag_created_at` (src/jarvis/release.py:386) so
every git read the release path performs stays in one module:

```
RELEASE_TAG_GLOB = "jarvis-*"          # hardcoded, no config knob
def tags_containing(root: Path, shas: Sequence[str]) -> tuple[list[str], str]
def overtaken_by(project_root: Path, shas: Sequence[str]) -> Overtaken
```

`RELEASE_TAG_GLOB` is a constant and not a setting: recorded assumption, and the whole
release path — `scripts/shipit.sh`, `_production_ref`'s `jarvis-X.Y.Z` rule, `DAEMON_UNIT`
— is already hardcoded to this repository. A knob would imply the rest generalises.

**A. Does a tag contain the payload — in the PROJECT's own checkout.**

```
git -C <project.path> fetch --tags --quiet --force origin        # first, timeout 60
git -C <project.path> tag --list 'jarvis-*' --contains <sha>     # once per sha, timeout 20
```

* the fetch is first because a tag another release path cut is not in this checkout until
  it is fetched — the defect's window is exactly minutes long, so a stale tag list is the
  normal state. It is best-effort: a fetch that fails is logged and the local tags are
  used, because the only thing that can produce is a tag not being SEEN, which leaves the
  order open (§7's direction);
* `--contains` is ancestry — `git tag --contains X` lists the tags whose commit is a
  descendant of X, which is the only correct reading given kn-179cd767. NOT "main is ahead
  of the tag" (kn-4f5aaa2b: main was 5 ahead and it decided nothing), NOT "the tag list is
  unchanged";
* **one invocation per sha, intersected in Python.** `git tag --contains A --contains B` is
  a UNION, so a single call would report a tag carrying half the batch as carrying all of
  it. A batch is one or two commits; the cost is one subprocess each;
* a non-zero exit or an empty stdout with stderr (`malformed object name`, an unknown sha,
  an unreadable repository) is an ERROR return, never "no tags". §7;
* the intersection is ordered by parsed version (`(major, minor, patch)`, lexical fallback)
  and the LOWEST is named: the first release that carried the whole batch is the honest
  answer, and it is deterministic across ticks.

**B. Does production run that tag — in the PRODUCTION checkout, read-only.**

`release._production_ref(paths.production_code_dir())` (src/jarvis/release.py:509) already
answers this from git, with `HEAD` as its "not at a tag" fallback. The test is membership:
the deployed tag must be one of the tags from A. Plus `_production_version()`
(src/jarvis/release.py:483) must equal that tag's version — BOTH readings, the file and
git, on kn-58429229's rule and for its reason: during the 0.5.0 half-apply the tag was
checked out and the running code was not it, and a check that reads one of the two is the
check that was already fooled once.

Ancestry is deliberately NOT re-run in the production checkout. Two reasons: `git
merge-base --is-ancestor` needs the payload OBJECT to be present there, which a deploy
clone does not guarantee, so the answer would be a permanent false negative on some hosts;
and making it reliable would need a `git fetch` in production, which writes refs into a
checkout that is a release tag and must never be written to. Membership against the set
computed in A gives the same fact with no object lookups and no writes. Note what it
correctly refuses: if a later tag was cut from an older base and does not contain the fix,
production running it leaves the order open, which is right.

`_production_ref` returning `HEAD`, or the version unreadable, is a refusal (§7).

### 4. Where it is hooked, and what the common case costs

`Daemon.settle_shipped_releases(project, store)`, called from `Daemon.tick`
(src/jarvis/daemon.py:715) inside the existing `if poll_prs:` block, immediately after
`self.sync_issue_references(project, store)` — so after `sync_issues`, which is what calls
`ensure_release`. Same cadence as the poller that records the merge commit (§2), which is
the only thing that changes the answer besides a tag or a deploy; both are minutes-scale,
so `PR_POLL_EVERY_TICKS` is the right beat and the reconcile beat would be too slow to
save the worker turn this exists to save.

Cost, in order of the guards:

1. `project.name != self._os_owner()` (src/jarvis/daemon.py:3803) returns immediately —
   **zero queries** for every project but one. §8 says why the step is OS-owner-only;
2. one `list_work_orders(statuses=OPEN_STATUSES, include_hidden=True)` and a metadata parse
   to find open orders carrying `RELEASE_BATCH_KEY` — the same read `ensure_release`
   makes, and with none found the step returns: **one indexed query per poll tick, no
   subprocess, no write**. That is the common case, because a release order exists for
   minutes at a time;
3. only with an open release order: one `release.read_marker()` (one small file read), one
   `git fetch`, one `git tag --contains` per payload sha, `_production_ref` and
   `_production_version`. No `gh` call unless the back-fill of §2 is needed.

`include_hidden=True`, matching `ensure_release`: hiding drops a record from listings, not
from the arithmetic that decides whether it is over.

### 5. Settling: `_settle` gains a reason, it does not gain a sibling

`release._settle` (src/jarvis/release.py:342) already holds every trap this path needs —
`completed` (clear a stale flag), `waiting_pr_merge` (leave parked; the merge poller owns
it), pending assumptions (leave in `needs_review`; completing over them is the back door
`wo ack`/`wo done` refuse), and `ops.close_out` + `ops.mark_backlog_done` for everything
else, which is kn-99d3f1d4 fact 4. A sibling function would be a second copy of those four
decisions, and the first divergence is a release order completed over an assumption nobody
answered.

So: `_settle(store, wo_id, tag, why=None)`. `why` defaults to today's
`f"release {tag} verified live"`, keeping the verify-on-boot path byte-identical; the new
caller passes `f"{tag} already carries this release's fixes and production runs it"`. The
event kind stays `release_completed` — one kind for "this release order is over and it went
fine", so the timeline renderer, the label test and any reader of that kind keep one
meaning.

### 6. The waiting event, once per tag

Tag contains the payload, production behind: write ONE event and return.

```
kind "release_overtaken"
payload {"tag": "jarvis-0.10.24", "shas": [...], "deployed": "<_production_ref answer>",
         "detail": "jarvis-0.10.24 already carries these fixes; production is on <ref>"}
```

Dedupe on the TAG, not on the kind: `store.events_of_kind(wo_id, "release_overtaken")`
(src/jarvis/project_store.py:2740) and write only when no existing event names this tag.
Dedupe by kind alone would go silent if the first tag never deploys and a later one does —
the user should hear about that second tag. It is one indexed read, paid only in this
branch.

Both new kinds (`release_overtaken`, `pr_merge_commit_recorded`) need a branch in
`timeline._describe`: kn-3f133363 — falling through to the raw-kind renderer is not the
same claim as a reader being able to see it, and `tests/test_issue_lifecycle.py:1173`
already pins that rule for the release kinds.

No attention flag, no notification. The order is open and waiting for a deploy that is
already on its way; flagging it would make a normal few minutes look like a fault.

### 7. It never fails open. Every refusal, and what it does

Each of these leaves the work order **exactly as it is** — no event, no status change, no
flag — and is retried on the next poll tick. The step returns `None`.

1. **No recorded merge commit and no back-fill** — `gh` unreachable, the PR not merged,
   `mergeCommit` empty, or no `pr_url`. An unresolved fix means the payload is not fully
   known, and a partial payload must never be tested: a tag containing the known half
   would complete an order whose other fix never shipped.
2. **Git unreadable in the project checkout** — non-zero exit from `git tag`, `OSError`,
   `subprocess.SubprocessError`, timeout. "Cannot tell" is a refusal, `_tag_created_at`'s
   own direction (src/jarvis/release.py:386).
3. **No tag contains the whole batch** — the ordinary case, and the one the order was filed
   for. Nothing to record.
4. **No production checkout, or `_production_ref` answers `HEAD`, or
   `_production_version()` is `None`, or the two disagree** — the fleet cannot be shown to
   run the fix. A fleet with no production checkout therefore keeps today's behaviour
   exactly, which is the same safe direction `verify_release_claim` documents
   (src/jarvis/release.py:410).
5. **A marker names this order** (`release.read_marker()`'s `wo_id` equals it, in ANY
   state, including `failed_verification`) — this order staged its own release and the
   marker handshake owns it. Skipping is what keeps the two paths from racing: only
   `verify_on_boot` may settle a release the OS itself performed, because only it checks
   the unit restart timestamps.
6. **Any unexpected exception** — caught per work order, `log.exception`, the loop
   continues, matching `sync_issues` and the PR poll. One release order must not stall the
   project's tick.

Only the conjunction of a fully resolved payload, a containing tag, and a production
checkout at that tag with a matching version completes anything.

### 8. Bounded to the OS-owning project, deliberately

The test reads `paths.production_code_dir()` and globs `jarvis-*`: both are facts about the
OS's own release path, and `schedule.os_owner` is the existing answer to "which project is
that". A second project with a release order gets exactly today's behaviour. Widening this
would mean a per-project notion of "the tag glob and the deployed ref", which is the config
knob §3 refuses.

### 9. Rejected alternatives

* **Have the worker do it (today's behaviour, documented).** It is a whole conversation
  re-send to run two git commands, it happened twice, and its output is a judgement that
  can be wrong — wo-e5816dd7 got as far as a DENIED gate before anyone noticed.
* **Complete on the tag alone, ignoring production.** The obvious fix and the one Neo
  question 751 ruled against: the order exists because the fleet owes the user a running
  fix, and a tag that never deploys closes the order with the debt unpaid.
* **Compare `head_oid` against the tag.** Silently never matches: `--squash`
  (src/jarvis/automerge.py:394) means `headRefOid` is not an ancestor of `main`, so the
  order would wait for ever and the bug would read as "the check does not work".
* **"main is ahead of the tag" / "the tag list changed".** kn-4f5aaa2b measured the first
  deciding nothing (main 5 ahead), and the second answers a different question than the one
  asked.
* **`git merge-base --is-ancestor` in the production checkout.** §3B: needs objects a
  deploy clone may not have, and making it right needs a fetch into a checkout that must
  never be written.
* **A `merge_commit` column on `work_orders`.** Drifts the first time the daemon dies
  between the two writes, and the event is already the recorded-when-true home for exactly
  this fact (`ops.complete_merged`'s docstring).
* **A new sibling of `_settle`.** Duplicates four traps that exist because each was a bug
  once; §5.
* **An invariant in `invariants.py`.** Invariants repair only what is unambiguous and take
  no subprocess; this one shells out to git twice and reads another checkout. Cadence and
  blast radius are both wrong.

### 10. Tests

Unit level, no systemd and no network. `src/jarvis/testing.py`'s `make_git_project` builds
real local repositories cheaply, `fake_gh` (tests/test_issue_lifecycle.py) is the `gh`
seam, and `paths.PRODUCTION_ROOT_ENV` points the production reads at a temp checkout. New
file `tests/test_release_overtaken.py`, plus additions to `tests/test_release_staging.py`.

1. **Ancestry, on real git.** Two repos: a squash commit on `main`, a `jarvis-0.10.24` tag
   cut after it, and a second sha that is NOT in the tag. `tags_containing` returns the
   tag for the first, `[]` for the pair — the intersection, which is the test that fails if
   anyone writes one `git tag --contains A --contains B`.
2. **The squash trap, pinned.** With the PR's `headRefOid` as the sha, `tags_containing`
   returns `[]`. This is the assertion that stops the wrong field being used again.
3. **Full completion.** A release order whose batch resolves, a containing tag, a
   production checkout at that tag with a matching `pyproject.toml` version: status
   `completed`, one `release_completed` event whose `why` names the tag, backlog closed.
4. **Tag but no deploy.** Production at an older tag: status unchanged, exactly one
   `release_overtaken` event naming the tag — and running the step three times still adds
   exactly one. Then a SECOND, later containing tag while production is still behind adds a
   second event.
5. **Every refusal in §7, parameterised, asserting the work order row and its event list
   are unchanged**: missing sha with `gh` unreachable, `git tag` exiting non-zero, no
   containing tag, no production checkout, production on `HEAD`, version/tag mismatch, and
   a marker naming this order.
6. **Pending assumptions and `waiting_pr_merge`** — `_settle`'s traps still hold through
   the new caller: the first stays `needs_review`, the second stays parked.
7. **The back-fill is paid once.** Two steps over the same unresolved fix make ONE
   `github.pr_view` call, and the second reads the `pr_merge_commit_recorded` event.
8. **Cost.** With no open release order, the step makes one statement and spawns no
   subprocess; for a non-OS-owner project, zero of both. Counted the way
   `tests/test_pr_checks.py` counts the poll's budget, so the §4 claim is executed rather
   than commented.
9. **Timeline labels** — both new kinds render through `timeline._describe` without
   falling back to the raw kind, added to the parametrised list at
   `tests/test_issue_lifecycle.py:1173`.

### 11. Out of scope

* **Cancelling the release worker.** An order completed here may still have a session with
  a pending gate request. `ops.close_out` already stops the session (that is what
  `_settle` relies on today); nothing extra is done about a gate request left `pending`,
  and `abandon_unargued_gates` is its existing owner.
* **The gate that went stale** (wo-e5816dd7, gate 215 denied on stale evidence). A gate
  request whose evidence has aged is its own defect; this spec removes the case where the
  order should not have been asking any more.
* **Other projects' release orders** — §8.
* **Batch membership.** Nothing changes about what `ensure_release` batches or when it
  files; this spec only adds an ending.
* **`waiting_pr_merge` release orders.** Left parked, unchanged: the merge poller is their
  ending and pulling them off it here would complete them before anyone merged.
