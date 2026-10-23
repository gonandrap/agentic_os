# A pushed branch with no pull request is a state

Work order wo-b289fae8. GitHub issue #1014.

## The problem

The OS models landing as a binary: a pull request exists, or the work is abandoned.
`unlanded_work` (src/jarvis/ops.py:4948) has no third value. So the one workflow this
user actually runs on this repo — the `gh` account is read-only, workers push the branch
and the user opens the pull request by hand — has no representation, and the OS's only
available response is a refusal the worker cannot satisfy. Everything below is downstream
of that missing state.

**1. The worker has no legal exit.** `ops.finish` reads `stranding = unlanded_work(...)`
at ops.py:6471 and raises `unlanded_refusal` at ops.py:6473 on
`stranding.produced and not abandon`. For a pushed branch: `--pr` has no URL to give,
`--abandon` is false because the work is not abandoned, and committing more changes
nothing. Neo told the worker to wait. The turn stopped mid-flight and the reconciler filed
the order into `needs_review` with no `result_summary`. The refusal's own printed remedy —
"open a pull request, and finish again with `--pr <url>`" (ops.py:6389) — is the one thing
the worker is not permitted to do.

**2. Nothing then names the pushed branch.** `invariants.true_blockers`
(src/jarvis/invariants.py:986) appends the assumptions line at invariants.py:1013.
`UNLANDED_BLOCKER` is case 3 of the `needs_review` triage at invariants.py:1240 and is
guarded by `not pending`. With an assumption outstanding, case 3 is skipped, case 4
(`IDLE_NO_FINISH_BLOCKER`) is also `not pending`, and the branch is named by nothing at
all. `check_attention_reason_is_true` (invariants.py:2219) then stamps `blockers[0]` over
the column at invariants.py:2253 — which is how the reporter watched the Claude Code
hook's worker-stopped reason get replaced by "2 assumptions pending your review".

**3. The blocker is generic even when it does fire.** `UNLANDED_BLOCKER`
(invariants.py:171) reads "work not landed — merge its pull request, or record that it is
being abandoned". There is no pull request to merge. It never names the branch, the base,
or a compare URL. On the reporting order the compare URL existed in exactly one place: the
body of msg-7, a worker message.

**4. Accepting the assumptions parks, but with the same generic line, after the fact.**
`review_work_order` (ops.py:8101) → `_land_after_acceptance` (ops.py:7942) →
`land_when_cleared` → `land_finished` (ops.py:4851), whose
`if stranding.produced: return park_unlanded(...)` at ops.py:4885 fires. So the order is
not silently completed — that part works. But it parks with `UNLANDED_BLOCKER`
(ops.py:5069), and `jarvis wo review`'s help still advertises accept as landing the work
order.

Root cause, named: **there is no state for "pushed, undertaken to land, not yet landed".**
Fixing only §2's ranking or only §3's wording patches the symptom and leaves the worker
without an exit.

## The fix

A branch that is pushed — the remote-tracking ref exists and carries every commit HEAD has
— with `pr_url` empty is an UNDERTAKING to land, just an incomplete one. The OS DERIVES
that state and parks on it, recording a `pr_pending` episode. Decisions A–G below were
settled by Neo on questions 3 and 4 of this work order; they are stated here, not
re-opened.

### 1. The predicate: what "pushed" means, and why it is read locally

New on `landing.Authored` (src/jarvis/landing.py:176), filled by `landing.authored`
(landing.py:291) with two more `_git` calls in the same worktree:

- `git rev-parse --verify --quiet refs/remotes/origin/<branch>` — the remote-tracking ref
  exists;
- `git rev-list --count refs/remotes/origin/<branch>..HEAD` is `0` — the remote carries
  every commit HEAD has.

AND `dirty` must be empty. Uncommitted files cannot have been pushed, and the worker is
the only one who can commit them, so that case keeps today's refusal verbatim. `pushed` is
false whenever any of the three reads fails — `_git` returns None on failure
(landing.py:484) and the safe direction is the existing refusal.

**Local, never the network.** This runs inside the worker's finish turn, and
`landing.authored`'s contract is that a work order must never be unable to finish because
the OS could not reach something (landing.py:296-299, and the module docstring's "no
`gh`"). The remote-tracking ref is written by the `git push` that this same worktree ran,
so for the case this spec is about it is exact. The stale direction is safe: a push made
from somewhere else reads as not-pushed and gets the old refusal.

### 2. `ops.finish` stops refusing — and does not park

The only change at ops.py:6472 is that the refusal is conditional:

- **`--pr <url>`** — unchanged. `unlanded_work` short-circuits to an empty `Authored`,
  `authorship` (ops.py:4986) does the record read, lands `waiting_pr_merge`.
- **`--abandon "<why>"`** — unchanged. The refusal is skipped, `abandoned` is written at
  ops.py:6498, `work_abandoned` excuses the landing.
- **produced, pushed, no `--pr`, no `--abandon`** — NEW. No refusal. The `finished` event
  at ops.py:6489 carries the extended `authored` record (§3), and the order falls through
  the ordinary path: validation round opens, `land_when_cleared` joins the two gates.
- **produced, NOT pushed** — the refusal stands, byte-identical. There is no URL that
  would show the user the code, so there is nothing better to say.
- **nothing produced** — unchanged, `completed`.

**The park belongs to `land_finished` (ops.py:4885), not to `finish`.** That branch is
already the single chokepoint every route to `completed` passes through, and it is where
the choice is made: `park_unlanded` when the branch is not pushed, a new
`park_pr_pending` when it is. One branch, one site. Putting a second parker in `finish`
would need a third copy on the accept path, which is precisely how the problem's §4 arose.
It also means **E's first half costs nothing**: `review_work_order` →
`_land_after_acceptance` → `land_when_cleared` → `land_finished` reaches the same branch,
so the moment the assumptions clear the order parks with the SPECIFIC blocker, not
`UNLANDED_BLOCKER`.

`park_pr_pending` mirrors `park_unlanded` (ops.py:5032) exactly, including the
once-per-episode discipline: an open episode means `set_status(..., trigger=
"pr_pending_repark")` and nothing else — no event, no flag — so `Daemon.settle_work_order`
re-deriving the same ending every tick renotifies nobody.

### 3. The facts hang on `landing.Authored`

`Authored` gains `pushed: bool = False` and `compare_url: str = ""`, and `record()`
(landing.py:203) emits both. That is the payload shape four settlement events already
write (`SETTLEMENT_EVENTS`, landing.py:232) and `authored_in` (landing.py:235) already
reads, so the facts reach the record through one writer and one reader. The addition is
backward-compatible by construction: `authored_in` keys its None sentinel on `"commits"
not in payload` and defaults everything else, so an event written before this ships reads
as `pushed=False, compare_url=""` — not-pushed, which is the safe direction.

**The URL is assembled by a new `github.compare_url(cwd, base, branch)`**, beside
`github.blob_url` (src/jarvis/github.py:185) and built to the same rule: local git only,
no network, `None` at every unreadable step and the caller omits the link rather than
guessing. It returns
`https://github.com/{owner}/{repo}/compare/{base}...{branch}?expand=1`, with the `origin/`
prefix stripped off `Authored.base`, and `{owner}/{repo}` from `github.origin_repo`
(github.py:162) — a LOCAL git read, declared in `github.LOCAL_GIT_READS` (github.py:74)
and already audited by the AST test.

**It returns None for a non-github.com origin**, which `blob_url` does not do.
`origin_repo` discards the host, so a checkout whose `origin` is a local path or a GitLab
remote still yields an `(owner, repo)` pair and `blob_url` happily builds a github.com URL
from it. Printing that in an attention line would tell the user to open a page that does
not exist, so `compare_url` reads the host too. Narrowing `blob_url` the same way is a
real but separate defect and is out of scope here.

**Import discipline.** landing.py's docstring forbids `gh`, the store and the catalog. It
does not forbid a sibling's local-git helper, and `github.blob_url` already does
`from .landing import base_ref` inside a function body for the same reason
(github.py:196). So landing calls `github.compare_url` through a function-body import —
both directions lazy, no cycle — rather than growing a second copy of "which repository is
this". No new `gh` verb is added anywhere in this spec, so
**tests/test_github_artifact.py does not move**, and that is asserted rather than assumed.

An empty compare URL is not a failure: `pushed` stays true, the park still happens, and
the blocker degrades (§5).

### 4. The `pr_pending` episode

`ProjectStore.pr_pending_open(wo_id)` on `release_red_park_open`'s shape
(src/jarvis/project_store.py:3942) — it returns the PAYLOAD, not a bool, because
`true_blockers` rebuilds the sentence from it and no constant can carry a URL.

Opened by `park_pr_pending`, writing `pr_pending` with
`{**work.record(), "was": wo["status"]}` — so branch, base, commits, dirty, pushed and
compare_url are all on the event.

Closed by a strictly newer event of any of: `finished` (a re-delivery; the next park is a
new episode and does record), `abandoned`, `pr_url_recorded` (§8), `pr_merged`.
`work_unlanded` is deliberately NOT a closer — that is the other episode, and the two are
mutually exclusive anyway (§5).

Ordering holds: `ops.finish` writes `finished` at ops.py:6489 before `land_when_cleared`
runs, so the `pr_pending` the same call produces is strictly newer and the episode is open,
as intended.

### 5. The blocker, and where it sits

`invariants.pr_pending_blocker(wo_id, park)` builds the sentence from the episode payload,
the way `release_base_red_blocker` (invariants.py:1145) and `neo_question_blocker`
(invariants.py:1105) already build theirs:

> branch `<branch>` is pushed and has no pull request — open it at `<compare_url>`
> (base `<base>`), then `jarvis wo pr <wo-id> <url>`

**With no compare URL on the payload** — origin unreadable, a non-github.com remote, or an
event written by an older release — it degrades to naming the branch, the base and the
command, and drops the URL clause entirely:

> branch `<branch>` is pushed and has no pull request — open one against `<base>`, then
> `jarvis wo pr <wo-id> <url>`

Both renderings are functions of the PAYLOAD ONLY. That matters: `INV-ATTENTION-REASON`
re-derives `attention_reason` every reconcile tick and rewrites anything it cannot derive,
so **the sentence must survive the worktree being reclaimed.** It does, because the branch,
the base and the URL were written onto the event at park time and nothing re-reads git.
A sentence that fell back to a live worktree read would be byte-identical today and
different after the worktree is deleted — the same relabelling defect this spec is fixing,
one level up.

**Placement:** inside the `governed and wo["status"] == "needs_review"` block at
invariants.py:1197, as its own `if` immediately BEFORE the three-way triage — so BELOW the
assumptions line appended at invariants.py:1013, and ABOVE `UNLANDED_BLOCKER`. Unguarded
by `pending`.

One guard changes elsewhere: the triage's case 4, `elif not pending:
blockers.append(IDLE_NO_FINISH_BLOCKER)` at invariants.py:1245, gains
`and pr_pending is None`. A pr_pending park always follows a `finished` event, so "the
worker stopped without finishing" is false there, and printing both would be two lines for
one state.

**D — why BELOW the assumptions line, departing from issue #1014's "Expected" text.** The
issue asks for the attention reason itself to name the compare URL. We are not doing that.
The precedent is `PR_CLOSED_BLOCKER` fifteen lines further down (invariants.py:1218) with
its note at invariants.py:1209-1217: it is also a fact about the outside world, it also
changes what the user does next, it is also allowed to co-occur with a pending assumption,
and it was deliberately left below the assumptions line on the grounds that letting both
through relabels nothing. The same reasoning applies unchanged. `blockers[0]` is one slot
and the decision the user OWES wins it; the compare URL is one line down and one `jarvis
wo show` away. The measured gap the issue reports is closed by §7 instead.

**Co-occurrence proofs**, for the blockers either side:

- *`UNLANDED_BLOCKER` (case 3, below)* — mutually exclusive by construction.
  `land_finished` writes one park or the other, never both, and each episode reads only
  its own event kind. The only path to both being open would be: park unlanded, then park
  pr_pending — but reaching the second requires a `finished` event, which closes the first
  episode (`work_unlanded_open`, project_store.py:3937).
- *`PR_CLOSED_BLOCKER` (case 1, above)* — needs `pr_state == "CLOSED"`, written only by
  `Daemon.poll_pull_requests`, whose candidate filter is `if wo.get("pr_url")`
  (daemon.py:6574). A pr_pending order has no `pr_url` by definition of the episode, so it
  is never polled and the column is never written. A legacy row carrying a stale CLOSED
  `pr_state` with an empty `pr_url` would render both, PR_CLOSED above — harmless, and the
  `jarvis wo pr` line still prints.
- *the `_mentions_assumptions` tail at invariants.py:1261* — no longer fires on these
  orders, because not every blocker mentions assumptions. Correct: the order is not parked
  on a dead session, it is waiting on a pull request, and `parked_reason` would say the
  wrong thing.

### 6. NOT a `remedies.ATTENTION_TEMPLATES` key — a deliberate asymmetry

`ATTENTION_TEMPLATES` (src/jarvis/remedies.py:229) is a CLOSED table and its docstring
forbids free-text parameters: a rule may pass only a record id or `#issue` (`_ID_PATTERN`,
remedies.py:245), explicitly so that an investigation's prose can never reach a command
the user is told to type. `pr_pending` is NOT added to it.

The compare URL is DERIVED STATE, not rule data. Interpolating it in `true_blockers` is
fine and has precedent (`SETTLED_FEATURE_BLOCKER`, `neo_question_blocker`); passing it
through a rule row would be exactly the path remedies.py exists to break. The consequence
is that **no self-evolution rule can raise this blocker** — correct, because a rule has no
derived worktree or episode payload to read the URL from, and a rule-raised flag with an
empty URL would be rewritten by INV-ATTENTION-REASON on the next tick anyway. This is an
asymmetry on purpose. Do not "fix" it by adding the key.

### 7. Closing the gap the issue actually measured, two other ways

1. The accept path parks with `pr_pending_blocker`, not `UNLANDED_BLOCKER` — free, from §2.
   The moment the assumptions clear, the real ask is `blockers[0]`.
2. `jarvis wo show` renders the `pr_pending` event — branch, base, compare URL —
   regardless of blocker rank, through `timeline._describe` (src/jarvis/timeline.py:1109),
   which `cli.py:3616` already feeds. `jarvis wo review`'s accept output carries the same
   three facts when the landing parked, instead of claiming the work order landed.

### 8. `jarvis wo pr <wo-id> <url>`, and the routing predicate it must be taught

The receiver the blocker names. `ops.record_pull_request(wo_id, url, project_name=None)`,
wired at cli.py beside `wo done` (cli.py:3719). It records `pr_url`, closes the
`pr_pending` episode and puts the order back in the merge queue.

Explicitly NOT "re-run `jarvis wo finish --pr`": `finish` is a worker verb, it opens a
validation round and writes a `finished` event, and a settled order refuses it.

Refusals:

1. no such work order — `find_work_order` already raises;
2. the order already has a `pr_url` — refuse naming it, `gates._record_pull_request`'s
   fourth condition (src/jarvis/gates.py:1504): a declaration is never overwritten;
3. no open `pr_pending` episode — refuse, and say what the order IS waiting on
   (`true_blockers[0]`);
4. malformed or foreign URL — `github.checked_pr_url(url, store.project_path)`
   (github.py:140) already does exactly this, including the leading-`-`-is-a-flag case;
   catch `UntrustedPullRequest` and re-raise as `OpsError`.

Writes: `update_work_order(pr_url=url)` and
`add_event("pr_url_recorded", {"pr_url": url, "source": "user"})`.

#### 8.1 This is the sharpest thing in the change: without it the command is inert

`ops.declared_pull_request` (ops.py:4899) decides whether a `pr_url` may route the merge
queue, and it reads ONLY the `pr_url` on `finished` events (ops.py:4921-4925).
`routes_on_pull_request` (ops.py:4928) is that read plus the gate-only test, and it is
what `land_finished`, the reconciler's park and `Daemon.poll_pull_requests`
(daemon.py:6587) all ask.

There are two sources today: `ops.finish --pr`, a declaration; and
`gates._record_pull_request` (gates.py:1492), deliberately NOT one, so a planner whose
merge gate was decided before `ops.submit_plan` settles `completed` instead of parking in
`waiting_pr_merge` over a pull request that has already merged (ops.py:4906-4910).

`jarvis wo pr` is a THIRD source **and it must route** — re-entering the merge poll is the
entire point. So `declared_pull_request` is taught it: walk `pr_url_recorded` events as
well as `finished` ones, and honour a recorded URL whose `source` is `"user"` or `"auto"`
(§8.2). `source: "gate"` keeps returning "" and stays inert, unchanged.
ops.py:4912 says in terms that this predicate is the one line to revisit when another
route is to reach the merge queue; this is that route, and nothing else branches on where
the column came from.

**If this is skipped the command LOOKS like it works.** The column is set, `jarvis wo show`
and the dashboard print the URL, INV-PR-RECORDED is satisfied, the attention flag goes
down — and `routes_on_pull_request` returns False, so `land_finished` completes the order
instead of parking it in `waiting_pr_merge`, and `Daemon.poll_pull_requests` never looks at
it again. Nothing fails loudly. That is the failure mode to test for first.

**Why the user's URL is a declaration where the gate's is not.** The gate proves that a
merge COMMAND was authorised; it says nothing about whether this order's delivery sits
behind that pull request, which is why a planner's already-merged gate URL must not park
it. A user typing `jarvis wo pr` is asserting the opposite fact directly: *this* order's
work is in *this* pull request, and it has not landed. It is the declaration the worker
could not make because the `gh` account is read-only — the same sentence `jarvis wo finish
--pr` would have carried, spoken by the only party able to speak it.

**Status:** move to `waiting_pr_merge` only when nothing else holds `needs_review`. Probe
the row it would become through `true_blockers` before writing, `resettle_after_repair`'s
pattern at ops.py:7117-7120. With an assumption still pending the URL is recorded and the
episode closed, the status stays `needs_review`, and the accept path lands it through
`land_finished` — which now sees a routing `pr_url` and returns `waiting_pr_merge`.

#### 8.2 The auto-attached URL routes too, and that is decided, not inherited

A URL the OS discovered is not obviously the user's case — on the face of it it looks like
the gate's, something the OS inferred rather than a person asserted. Decided explicitly:
**it routes**, recorded as `pr_url_recorded {source: "auto"}`, and `declared_pull_request`
honours it.

The gate's URL is inert because the gate is evidence about a COMMAND and the connection to
this order's delivery is an inference that was wrong in practice (issue #742's planner).
The auto-attach has no such gap, by three constructions that the gate has none of:

1. it only runs when an open `pr_pending` episode says THIS order is undertaking to land
   and has not yet;
2. it searches on THIS order's own branch, read off that episode's payload
   (`gh pr list --head <branch>`);
3. `--state open` means the pull request has not merged, so the planner failure mode — a
   settled order parked behind a merged PR — cannot occur.

An auto-attach that did not route would also be strictly worse than doing nothing: it
would set the column, satisfy INV-PR-RECORDED and silence the blocker, while removing the
order from the merge queue and from the user's attention list at the same time.

The honest weakness is that this is a guess at a branch-to-PR mapping. It is bounded:
`checked_pr_url` keeps it on this project's own origin, the URL is on `jarvis wo show` and
the timeline with `source: "auto"` so the provenance is visible, and a wrong attach is
reversible — the order sits in `waiting_pr_merge`, and `jarvis wo done` closes it.

### 9. Auto-attach: on the daemon poll, not on the blocker's derivation

`github.open_pull_request_for_branch` (github.py:473) already exists, already runs
`("pr", "list")` — in `READ_ONLY_VERBS` (github.py:66) — and already passes its answer
through `checked_pr_url`. Nothing new is allowlisted.

**It goes on the daemon, beside `poll_pull_requests`.** Two reasons, and the first is the
load-bearing one: `true_blockers` runs for every work order on every reconcile tick and is
read by the CLI, the dashboard and the invariants, and its contract is "derived from state
alone" (invariants.py:988). A sentence derived from a network read is one
INV-ATTENTION-REASON renders differently when the network is down — the same class of
defect as the problem's §2. Second, the daemon already owns every `gh` read, its rate
budget and the skip-whole-step-when-nothing-is-parked shape.

Shape: select `needs_review` rows with an open `pr_pending` episode and no `pr_url` — zero
on almost every tick, one indexed query — read the branch OFF THE EPISODE PAYLOAD so it
survives the worktree, and on a non-empty answer call the same `ops.record_pull_request`
the CLI calls, with `source="auto"`. Cadence: `PR_POLL_EVERY_TICKS`.

Degrades silently: `GhUnavailable` and `GitHubError` are logged and the step returns,
exactly as `poll_pull_requests` treats them. The manual `jarvis wo pr` route is unaffected,
and a read-only `gh` can still run `pr list`.

### 10. `pr_url` never holds a compare URL

Four readers treat that column as a real pull request: `Daemon.poll_pull_requests`
(daemon.py:6572), `automerge` via `Daemon.auto_merge`, INV-WORK-LANDED
(`invariants.check_work_lands`), and `landing.judge` (landing.py:425). Neo rejected
writing a compare URL there explicitly. The column holds the declared pull request or
nothing; the compare URL lives on the `pr_pending` payload and in the rendered blocker and
nowhere else.

Belt to that braces, already in place: `github.PR_URL_RE` (github.py:136) anchors on
`/pull/<n>`, so a compare URL reaching `checked_pr_url` raises `UntrustedPullRequest`
rather than being fetched. The failure would be loud, not silent.

## Rejected alternatives

1. **Put the compare URL in `pr_url`.** The obvious fix, and the one a reviewer will
   propose. Poisons the four readers in §10. Neo rejected it.
2. **A worker verb — `jarvis wo finish --pushed` or `--no-pr`.** The state is a fact about
   the branch the OS can read exactly and locally; asking a worker to assert it adds a way
   to assert it falsely. Decisive argument: a verb repairs nothing already stuck. Deriving
   it fixes the orders in this state today, including the one that filed issue #1014.
3. **Rank the new blocker above the assumptions line** (issue #1014's "Expected" text).
   §5's D.
4. **Make the attention reason itself carry the compare URL regardless of rank** (also the
   issue's text). `blockers[0]` is one slot; §7 closes the measured gap without taking it
   from the decision the user owes.
5. **Add `pr_pending` to `ATTENTION_TEMPLATES`.** §6.
6. **Park in `ops.finish`.** Two parkers, two chances to drift, and the accept path would
   need a third copy — the shape of the problem's §4.
7. **Ask GitHub at finish time whether a pull request exists.** A settle-time predicate
   must not depend on the network (landing.py's module docstring). §9 does the asking
   where a failure costs nothing.
8. **Write a synthetic `finished {pr_url}` from `jarvis wo pr`** so `declared_pull_request`
   needs no change. Rejected: it would put a delivery on the timeline that no worker made,
   and `refusal_answered` and the round machine both date against `finished` events.
   Teaching the predicate is one line in the function whose docstring asks for it.

## Test surface

- **tests/test_landing.py** — `Authored.pushed` and `compare_url`; `record()`/`authored_in()`
  round trip; a legacy payload with neither key reading as `pushed=False`; the predicate
  across remote-ref-absent / remote-behind / remote-equal / dirty-worktree.

  **The `Repo` fixture (tests/test_landing.py:63-100) needs no change for any of that, and
  must not be used for the compare URL.** Its `origin` is a local bare repo at
  `tmp_path/origin.git`, which is exactly right for the pushed predicate — the
  remote-tracking ref is real and `git push` updates it — and exactly wrong for the URL:
  `github.origin_repo` does not return None there, it returns a pair parsed out of the
  temp path, so a naive derivation would build a plausible-looking github.com URL from
  nonsense. That is what §3's host check exists for, and the fixture is the regression
  test for it: assert `compare_url` is `""` on this repo and that the park still happens
  with `pushed=True`.
- **A separate test for the derivation itself** — a repo whose `origin` is set to a
  github.com remote, asserting the exact URL including `?expand=1` and the stripped
  `origin/` prefix; plus None for an ssh GitLab remote and for an unreadable origin.
- **tests/test_pr_recorded.py** — `finish` no longer refuses on pushed-no-pr; still refuses
  verbatim with uncommitted files and with an unpushed commit; `pr_pending` written once;
  a re-park writes no second event and raises no second flag.
- **tests/test_wo_pr_merge.py** — **first, the inert-command test from §8.1**:
  after `jarvis wo pr`, `routes_on_pull_request` is True and `Daemon.poll_pull_requests`
  picks the order up — a test that only asserts the column is set would pass against the
  broken version. Then: `declared_pull_request` honours `source` `"user"` and `"auto"` and
  still refuses `"gate"`; the full round trip into `waiting_pr_merge`; all four refusals;
  the status staying `needs_review` while an assumption is pending and landing on accept;
  auto-attach on the poll, and silent degradation when `gh` raises.
- **tests/test_attention_premature.py** — the blocker co-occurs with the assumptions line
  and ranks below it; `IDLE_NO_FINISH_BLOCKER` is suppressed; INV-ATTENTION-REASON
  re-derives the sentence byte-identically after the worktree is deleted; and the degraded
  rendering is stable across ticks when the payload has no compare URL.
- **tests/test_remedies.py** — unchanged, plus an assertion that `pr_pending` is NOT an
  `ATTENTION_TEMPLATES` key, so §6's asymmetry is pinned rather than remembered.
- **tests/test_github_artifact.py** — unchanged, asserted: no verb is added, and
  `compare_url` adds only `LOCAL_GIT_READS` traffic the AST test already permits.

## Deliberately not covered

- The worker briefing. OPERATION.md and the dispatch prompt are not updated to describe the
  new outcome; the worker does not need to know, because `finish` simply stops refusing.
- `github.blob_url`'s missing host check (§3). Same latent bug, different surface, and
  fixing it would change links already rendered on the dashboard.
- Dashboard rendering beyond what `wo show --json` already carries.
- Orders stuck in this state whose worktree has already been reclaimed. Derivation needs a
  worktree to read, so those do not self-repair; `jarvis wo pr` or `jarvis wo done` closes
  them by hand. Orders whose worktree survives — including wo-b289fae8's own reporter — are
  repaired by the next settle.
- Projects whose `gh` account can open pull requests. Unaffected: `--pr` is still the first
  and normal outcome.
