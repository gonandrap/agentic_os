# A catch-up with main costs no round

GitHub issue #806, wo-659be188. Neo question 791 approved the mechanism in §3.
Predecessors this builds on: `docs/superpowers/specs/2026-09-18-a-red-base-heals-itself.md`
(the verdict carry, §5) and
`docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md` (the re-judge, §3).

## 1. The problem

**A validation verdict is bound to a commit, and catching the branch up with `main` moves
that commit — so every catch-up costs a validation round, and the last one strands the
order in front of the user.**

The chain, with the code:

1. `validation_rounds.head_sha` records the commit a round judged.
   `ProjectStore.validated_head` (src/jarvis/project_store.py:4233) is the single reader,
   and `automerge.decide` condition 5 (src/jarvis/automerge.py:287) holds
   `HELD_SHA_MOVED` when the live head is not that commit. This is correct and is not the
   defect: nothing may merge a commit no round bound.
2. `Daemon.auto_merge` answers that hold with `Daemon._rejudge_moved_head`
   (src/jarvis/daemon.py:4954, method at :5839), which calls `ops.rejudge_moved_head`
   (src/jarvis/ops.py:4108). That opens a **real round** — `submit_for_validation`, a
   full panel, a round number — and at `nxt >= cfg.max_rounds` (ops.py:4150) it declines,
   writes `invariants.REJUDGE_DECLINED_EVENT`, and `invariants.rejudge_exhausted`
   (src/jarvis/invariants.py:513) then derives `invariants.SHA_MOVED_BLOCKER`: an
   attention item telling the user to force a round by hand.
3. **Heads move for reasons no seat ever needs to read.** Merging `origin/main` into the
   branch is one: a worker resolving `CONFLICTING`, `ci.update_branch`
   (src/jarvis/ci.py:202), or the user pressing "Update branch". The diff the panel
   judged is unchanged; only the base underneath it moved.
4. And that move is **compulsory**, not incidental: the `auto_merge` gate reviewer refuses
   a merge whose CI ran against a base behind `main`, and `ops.PR_BEHIND_NOTE`
   (src/jarvis/ops.py:4944) already tells workers to merge `origin/{base}` in for exactly
   that reason. So every parked pull request must catch `main` up at least once, and the
   OS charges it a round for doing what the OS asked.

Live evidence, both parked right now on this fleet: **wo-00bd1096 / PR #779** and
**wo-8736a5c5 / PR #794**. Both are green and mergeable, both held on `sha_moved`, both
out of rounds, both sitting on the user's attention list with nothing wrong with the code.

**Root cause, stated plainly:** the OS has exactly one way to bind a verdict to a new
commit — spend a round — and one narrow exception to it. The exception,
`ops.carry_validated_head` (src/jarvis/ops.py:5274), already encodes the right rule ("the
difference is a base merge and nothing else, so the verdict still holds") but is reachable
from **one** call site, `Daemon.heal_inherited_failure` (src/jarvis/daemon.py:6143-6162),
and only for **one** shape: a single merge, exactly two parents, first parent the judged
commit. Any other true instance of the same fact — two catch-ups in a row, a catch-up the
user pressed, a catch-up a worker pushed — falls through to the round machine. Part 2 of
the issue is the second half of the same cause: nothing in the OS performs the catch-up
its own gate reviewer demands, so the move is always made by someone else, at a moment the
OS did not choose and cannot bound.

## 2. Rejected alternatives

* **Re-judge, but do not count the round** (`counted_validation_rounds` exemption).
  Cheaper to write, and it still spends a full panel — five seats, real money, minutes of
  latency — to re-read a diff that is byte-identical. It also makes the round ledger lie:
  a round that happened and is not counted is the shape of every "why is this at round 4
  of 3" bug.
* **Widen `automerge.decide` to accept "head is a descendant of the judged commit".**
  Ancestry says nothing about content: a merge can resolve a conflict and author new code
  while remaining a descendant. That relaxes the invariant instead of proving it holds.
* **Add a new round row marked "carried".** Rejected in Neo question 791: a second way to
  express "this verdict covers this commit" means two readers of the predicate, and
  `ProjectStore.validated_head`'s own docstring (project_store.py:4260) is the standing
  ruling that there is one.
* **Only detect the single-merge shape, keep the loop for the rest.** That is today's
  behaviour, and it fails on the case that actually occurs: base merges arrive in chains
  (a worker merges main, CI is slow, main moves, the OS merges main again).
* **Ask the panel for a cheap one-seat opinion instead.** A seat asked "did this merge
  change anything" is a model answering a question `git patch-id` answers exactly.

## 3. The fix

Generalise the existing carry from **one merge** to **a chain of base merges**, prove the
chain two independent ways, and fire it from the automerge poll before the round machine
gets the chance.

### 3.1 Where each piece lives, and why there

| Piece | Home | Why there |
|---|---|---|
| Walk `judged..head`, read each commit's parents | `ci.base_merge_chain`, new, beside `ci.commit_parents` (src/jarvis/ci.py:234) | `commit_parents` is already the existing reader and already in `ci.VERBS` as `("api", "--method")`; a walk is N of it. |
| Local git: fetch, `patch-id`, ancestry | **new module `src/jarvis/branchproof.py`** | `ci.py` is AST-tested against its `gh` allowlist and is *the* GitHub write surface (ci.py:51-66); `github.py` is AST-tested read-only. Neither is a home for local `git`. The shape to copy is `landing._git`/`landing._fetch_ref` (src/jarvis/landing.py:359) — subprocess, timeout, failure logs and returns None. No store, no catalog, no `gh`. |
| The policy and the single write | `ops.carry_merge_chain`, new, beside `ops.carry_validated_head` (ops.py:5274) | Same signature discipline: the facts arrive as arguments (chain, patch-ids), the function decides and writes. Keeps it unit-testable with no network, exactly as `carry_validated_head` is. |
| The IO and the logging | `Daemon._carry_catch_up`, new, beside `Daemon._rejudge_moved_head` (daemon.py:5839) | `_rejudge_moved_head`'s division of labour: daemon does `gh`/`git` and logging, `ops` holds the rule. |
| The write itself | `ProjectStore.carry_round_head` (project_store.py:4375), unchanged | It is already dumb on purpose. |

`ProjectStore.validated_head` already prefers `carried_head_sha` (project_store.py:4287),
so **no reader changes**. The invariant "nothing merges a commit no round has bound" is
preserved rather than weakened: the carry binds the existing round to the new head, and it
does so only when both proofs below say the bound content is the same.

### 3.2 Proof (a) — from GitHub: the chain adds no unjudged commit

`ci.base_merge_chain(pr_url, judged, head, *, base_ref, cwd)`, walking **first parents
backwards from `head`**:

1. `parents = ci.commit_parents(pr_url, cursor, cwd=...)` — one `gh api --method GET
   repos/{owner}/{repo}/commits/{sha} --jq .parents[].sha` per commit, the call
   `commit_parents` already makes.
2. Require `len(parents) == 2`. One parent is an ordinary commit — authored content.
   Three or more is an octopus merge nothing here reasons about.
3. Record `parents[1]` as the commit merged in, and whether it is a BASE commit (item 6).
4. `cursor = parents[0]`. Stop when `cursor == judged` — success. If `cursor` is a commit
   with fewer or more than two parents before reaching `judged`, fail.
5. **Bound: `ci.CHAIN_LIMIT = 10` commits**, i.e. at most 10 API calls, only on a pull
   request held at `sha_moved`, and at most once per (work order, head) because a refusal
   is recorded and deduped (§4). Past the limit: fail, fall through to the re-judge. Ten
   catch-ups on one parked pull request is not a case worth an unbounded walk.
6. Every `parents[1]` must be an **ancestor of `origin/{base_ref}`** — a base merge — **or
   an ancestor of `judged`**, which **Neo question 806** widened this to accept: a worker
   pulling with a merge before pushing leaves a merge of two lineages of the BRANCH, and a
   second parent the judged commit already contains brings in no commit that was not judged.
   A second parent that is neither is somebody's authored content arriving in the same shape
   and is refused with `proof == "chain"`. Proof (b) stays REQUIRED alongside: the widening
   is never sufficient on its own. Asked **locally**
   (`branchproof.is_ancestor`, `git merge-base --is-ancestor`), not with a compare API
   call per commit: the fetch is already paid for by proof (b), `origin/{base_ref}` comes
   from the same origin `gh` is talking to, and a per-commit `compare` would double the
   API cost of the walk. A fetch that fails refuses the carry (§3.4).

Returns the chain oldest-first, or `()` when it cannot be proved. `github.GitHubError`
propagates to the daemon, which logs and leaves the pull request exactly where it was —
`carry_validated_head`'s failure direction.

### 3.3 Proof (b) — locally: the pull request's own content is unchanged

`branchproof.patch_id(repo, base_ref, sha)`:

```
git -C <repo> fetch --quiet origin <base_ref> <pull/N/head>     # once per attempt
git -C <repo> diff <merge-base(base_ref, sha)>..<sha> | git patch-id --stable
```

computed for `judged` and for `head`. **The carry needs the two patch-ids to be
identical.**

This is the proof (a) cannot give. GitHub reports an **evil merge** — a merge whose
conflict resolution edited the pull request's own files — with exactly the parentage of a
clean one. Proof (a) would carry it, binding the panel's verdict to code no seat read.
Comparing what the branch adds *on top of its merge base* catches it: a resolution that
changed the branch's contribution changes that diff, and therefore the patch-id.

**Conservative by design, and the inverse error is accepted.** A perfectly clean merge can
still change the patch-id when `main` touched lines adjacent to the branch's own, because
the merge base moved and the diff's context moved with it. That is a *false refusal*: the
pull request falls through to `ops.rejudge_moved_head` and behaves exactly as it does
today. The asymmetry is deliberate — a missed carry costs a round, a wrong carry merges
unread code.

`git patch-id --stable`, not `--unstable`: the id must not depend on hunk ordering.

### 3.4 The fall-through, and why a failed proof (b) deserves a real round

When proof (a) fails, proof (b) differs, the fetch fails, or `gh` cannot be read: write the
refusal event (§4) and **do nothing else** — control returns to the existing
`Daemon._rejudge_moved_head` call at daemon.py:4954-4958, unchanged, which spends a round
or declines exactly as now.

Spending a round there is right, not a consolation prize. A differing patch-id means the
bytes this pull request contributes are not the bytes the panel read. On the commonest
cause — a worker resolving `CONFLICTING` — **the resolution is authored content**: someone
chose which side won, line by line, in code that will land on `main`. No seat has read it.
That is precisely the thing a validation round exists to judge, and the OS must pay for it.
The cheap version of this feature is one that carries whenever the parentage looks like a
merge; it would have shipped a conflict resolution to `main` under a verdict that predates
it.

### 3.5 Where it fires

In `Daemon.auto_merge` (daemon.py:4952-4959), in the `not decision.armed` branch, on
`decision.code == automerge.HELD_SHA_MOVED`, **before** `_note_automerge_held` and
**before** `_rejudge_moved_head`:

1. skip when `record_only` (a repairing pull request merges nothing);
2. `self._carry_catch_up(project, store, wo, pr, decision)`;
3. on success: re-read `store.latest_validation_round(wo_id)` (one indexed read, on the
   carry tick only) and call `automerge.decide` again with the same `pr` — no second `gh`
   read, the head has not moved — then continue down the existing armed path. The merge
   can then be proposed on the same tick instead of two minutes later.
4. on refusal or None: `_note_automerge_held`, then the existing `_rejudge_moved_head`
   call, untouched.

Two ordering notes:

* **Before `_note_automerge_held`**, so a carry that succeeds does not leave an
  `automerge_held` event describing a stall that lasted microseconds. The hold dedupe
  (`_note_automerge_held`, daemon.py:5892, keyed on head + code + reason text) is
  unaffected: a refused carry still records the hold, once.
* **The carry does not require `automerge.only_the_head_moved`** (automerge.py:308), which
  `_rejudge_moved_head` does require. That predicate asks "would this merge if a round had
  judged the head", and CI on a freshly caught-up head is usually still running. The carry
  only rebinds a verdict; whether the pull request merges is still `decide`'s six
  conditions, re-asked. Requiring an armed-but-for-the-sha pull request would defer every
  carry until CI finished and reintroduce the window in which a round gets spent.

## 4. Neo's two conditions, as requirements

**(i) Nothing else is relaxed.** A carried head still has to pass `automerge.decide`'s
conditions 1-4 and 6 — open, `mergeable_now`, `checks_green`, `merge_state == CLEAN` — on
the carried commit, and the merge still files its `automerge.GATE_KIND` request for Neo
(`automerge.propose`, daemon.py:4985). The carry changes what that request says about which
commit was judged and never whether one happens. `ci.update_branch` in part 2 stays
ungated for the reasons in its own docstring (ci.py:213-223) and is in `ci.WRITE_VERBS`
(ci.py:66) — **confirmed: `WRITE_VERBS = (("pr", "update-branch"),)`, and it is not in any
gate recogniser pattern.** §5's catch-up therefore needs no new authorisation; the merge it
leads to is still gated.

**(ii) Every carry and every fall-through is on the timeline, naming which proof failed.**
A carry that skipped a round must be auditable from `jarvis wo show` alone.

**The carry event: reuse `ops.HEAD_CARRIED_EVENT` (`"validation_head_carried"`,
ops.py:5188), with a `cause` field.** One kind for "a verdict was carried", for
`validated_head`'s one-home reason; the existing base-heal write at ops.py:5346 gains
`cause="base_heal"`. Payload for the new path:

```
cause: "base_merge_chain"
round, round_id, judged_sha, carried_head_sha
chain: [sha, …]              # oldest-first, the commits walked
merged_base_shas: [sha, …]    # second parents that are ancestors of origin/<base>
merged_branch_shas: [sha, …]  # second parents the JUDGED commit already held (Neo 806)
base, base_sha                # base_ref and origin/<base_ref> at proof time
patch_id                      # the value both commits produced
reason                        # the sentence below
```

`reason`: `` `main` was merged into this branch N time(s) and the pull request's own diff
is byte-identical (patch-id <id[:12]>) — no authored content changed ``, with a second clause
`` and M merge(s) brought in only commits <judged[:10]> already contained `` whenever the
chain holds a pull merge. **`merged_base_shas` must not lie**, so `base_sha` is the newest
BASE commit walked and `""` when the chain held none.

`timeline._describe` branch, beside `validation_rejudge_declined` (src/jarvis/timeline.py:531):

* label `"Verdict carried to the new head"`
* detail `round {round} passed on {judged_sha[:10]}; it now covers {carried_head_sha[:10]},
  which is {judged_sha[:10]} plus {n} merge(s) of {base} and nothing else` — and, when a
  walked merge was a pull merge, `plus {n} merge(s) — {n_base} of {base}, the rest bringing
  in only commits it already contained`

**The refusal event: new `ops.CARRY_REFUSED_EVENT = "validation_carry_refused"`.** Payload:

```
judged_sha, head_sha, proof, detail, chain
```

`proof` is one of `"chain"` (parentage), `"patch_id"` (content differs),
`"fetch"` (no local git answer), `"read"` (`gh` unreadable) — one value per condition, the
`automerge` hold-code discipline (automerge.py:76 onwards), so a reader can tell "someone
resolved a conflict" from "the daemon could not reach GitHub".

`_describe`: label `"Verdict not carried — re-judging"`, detail per proof, e.g. for
`patch_id`: `the diff on {head_sha[:10]} is not the diff round {round} read, so the merge
resolved content nobody judged`.

**Dedupe: once per (head_sha, proof).** A parked pull request reaches this every two
minutes; an event per tick would bury the record. New helper
`ops.carry_refusal_told(store, wo_id, head_sha, proof) -> bool`, reading
`store.events_of_kind(wo_id, CARRY_REFUSED_EVENT)` — the shape of `ops.rejudged_heads`
(ops.py:4080) and `ops.base_heal_spent` (ops.py:5217), keyed on the commit rather than
saturating. Neither event is an attention item and neither goes in `timeline.DEBUG_KINDS`:
unknown kinds already read as `signal` (timeline.py:96).

## 5. Part 2: the OS catches the branch up itself

Before proposing a merge, if the pull request is behind its base, the OS updates the
branch, lets CI run on the new head, carries per §3, then proposes. This reverses the
"BEHIND is reported, never acted on" decision recorded on `github.PullRequest.behind`
(src/jarvis/github.py:345) and `ops.PR_BEHIND_NOTE` (ops.py:4944): acting on it is now
cheaper than telling a worker to, because §3 makes the act free.

### 5.1 Detecting behind-ness — `.behind` alone is not enough

`PullRequest.behind` is `merge_state == "BEHIND"`, and
`automerge.decide`'s own docstring (automerge.py:224) records that on this repository —
`strict_required_status_checks_policy` off — a merely-behind branch reports **`CLEAN`**,
not `BEHIND`. Keying the catch-up on `.behind` alone would make this feature a no-op on the
fleet's busiest project. So:

* add `baseRefOid` to `github.PR_FIELDS` and `PullRequest.base_oid` (the base branch's head
  commit as GitHub sees it) — a field on a read the poll already makes, costing nothing;
* behind-ness is `pr.behind` **or** `base_oid` is not an ancestor of `head_oid`
  (`branchproof.is_ancestor`, after the fetch §3.3 already needs). New predicate
  `ops.catch_up_needed(pr, *, repo)`.

`base_oid` is also the **accounting key** below, and it is the honest one: it is the commit
that would be merged in, read from the same call.

### 5.2 Where it fires

`Daemon.auto_merge`, in two places, both **before any gate request exists**:

* the held path, when `decision.code == automerge.HELD_MERGE_STATE_UNCLEAN` and
  `pr.merge_state == "BEHIND"`;
* the armed path, immediately before `store.latest_approval_for(...)` (daemon.py:4963), and
  **only when that lookup finds no approval**. Never after one is filed: the gate command
  string carries the judged sha (`automerge.merge_command`), so updating the branch behind
  a live grant would orphan a permission Neo already gave and silently ask for another.

New method `Daemon._catch_up_with_base`, returning the re-read `PullRequest` — the
`heal_inherited_failure` contract (daemon.py:6022, `(handled, pr)`), for its reason: the
caller must decide against the head the update produced.

### 5.3 The guards, and what each stops

1. `cfg.enabled and cfg.auto_merge` and `wo["status"] == "waiting_pr_merge"` —
   `auto_merge`'s own gate (daemon.py:4929-4932). A project that has not opted in pays
   nothing and no branch of its is touched.
2. `not worker_session.busy(store, wo["id"])` and `not store.queued_messages(wo["id"])` —
   never move the head under a running turn or an undelivered instruction.
3. `not store.validation_round_open(wo["id"])` — the branch must not move beneath the
   seats mid-round. Neo question 283; the guard pair copied from
   `heal_inherited_failure` (daemon.py:6097).
4. `not ops.base_heal_spent(store, wo_id, base_oid)` (ops.py:5217) — **one update per
   (pull request, base commit)**, counting a refused update as spent. Shared with the
   red-base heal deliberately: the bound is about how often the OS may rebuild one merge
   ref, not about why. `ops.record_base_update` / `record_base_update_failed` gain
   `cause: "behind" | "base_red"` so the two reasons stay distinguishable on the record
   while sharing the key.
5. **`ops.catch_up_attempts(store, wo_id) < ops.CATCH_UP_MAX` (3)** — counting
   `PR_BASE_UPDATED_EVENT` rows with `cause="behind"`. The per-base-sha key cannot bound a
   fast-moving `main`: every new commit on `main` is a new key and would earn a fresh
   update, so a busy day could have the OS chasing the base for ever. Past the cap the
   pull request stays held with its reason and the user or the gate decides.
6. **Re-read the head immediately before the update** (`github.pr_view`) and require it to
   equal the judged commit — `heal_inherited_failure`'s review-round-1 note
   (daemon.py:6102-6120). `gh pr update-branch` has no `--match-head-commit`, so this
   narrows the race and §3's proof (a) closes it.
7. `ci.update_branch` raising `GitHubError` → `record_base_update_failed`, spend the
   attempt, leave the pull request where it was. No retry this tick.

After the update the new head's CI is unfinished, so `decide` holds
`HELD_CHECKS_NOT_GREEN` for a tick or two and the poll simply waits — no new machinery.
When it goes green the head is still not the judged commit, `HELD_SHA_MOVED` fires, and
§3 carries the verdict across the merge the OS itself made. That is the whole loop, and it
cannot fight a worker: guards 2, 3 and 6 all defer to one, and every deferral is "not this
tick", never "dropped".

## 6. Part 3: the two stranded orders recover with no human action

Required behaviour: **wo-00bd1096 / PR #779 and wo-8736a5c5 / PR #794 recover on a
reconcile tick after this ships, with nothing typed.** The paths that make it true:

1. Both are `waiting_pr_merge` with a `pr_url`, so `Daemon.poll_pull_requests` selects
   them (`PR_POLL_STATUSES`, daemon.py:4619) and reaches `Daemon.auto_merge` every tick
   (~2 min) already today — no new scheduling.
2. `decide` returns `HELD_SHA_MOVED`, and §3.5 attempts the carry **before** any round
   accounting. The carry consults **neither** `cfg.max_rounds`, `counted_validation_rounds`
   nor `rejudged_heads(..., declined=True)`. Stated as a requirement because it is the
   whole of part 3: the declined-heads dedupe inside `ops.rejudge_moved_head` (ops.py:4151)
   must **not** suppress the carry, and an order with zero rounds left must still be able
   to carry. Enforced structurally — `ops.carry_merge_chain` never reads `cfg` — not by a
   comment.
3. The carry writes `carried_head_sha` on the existing passed round
   (`ProjectStore.carry_round_head`), so `store.validated_head(latest_round)` becomes the
   live head (project_store.py:4287).
4. `invariants.rejudge_exhausted` (invariants.py:513) therefore returns **False** at its
   last clause — `store.validated_head(...) != head` is no longer true (invariants.py:544)
   — so `SHA_MOVED_BLOCKER` stops being derived and nothing re-raises it. **No invariant
   lowers the STORED `needs_attention` flag for a non-terminal status**:
   `check_attention_reason_is_true` only relabels the reason and
   `check_no_phantom_attention` is terminal-only. So the attention item stops being
   derived at once and the flag itself comes down when the order COMPLETES on the merge
   (step 5) — still with no ack and no `jarvis validation force`.
5. Same tick, §3.5 step 3 re-decides: green, mergeable, CLEAN, verdict bound → the
   `AUTO_MERGE` gate request is filed for Neo and the pull request merges on approval.

**Honest limit**, narrowed by Neo question 806: a divergent pull merge of the branch's own
lineage no longer falls through (§3.2 item 6), which is the shape wo-00bd1096 / PR #779
actually has. What remains: if either branch's history contains a merge whose conflict
resolution edited the branch's own files, proof (b) refuses, the order falls through to
`rejudge_moved_head`, and with rounds exhausted it declines again — the user still has to
raise `max_rounds` or force a round for that one. Implementation must check both branches'
actual first-parent chains before claiming recovery for both; a conflict-resolving merge on
either is the expected fall-through, not a bug. Recovery for a clean catch-up chain is
required and testable.

## 7. Tests

Pinned ruling: targeted tests only, CI runs the suite.

**`tests/test_base_heal.py`** — owner of `carry_validated_head`, the fake-`gh` parentage
fixture (`fake_gh.set_parents`, src/jarvis/testing.py:1793) and the `ci.VERBS` AST test
(`test_this_module_writes_only_the_branch_update`, :646):

1. a chain of **two** base merges is carried, the pull request merges, and
   `counted_validation_rounds` is unchanged (3a + 3b happy path);
2. a chain whose first parent is a worker push is refused — `proof == "chain"`;
3. a merge whose second parent is an ancestor of neither `origin/main` nor the judged
   commit is refused — `proof == "chain"`, no carry, and the round machine gets its turn
   (Neo question 806, condition 3);
3a. a pull merge of two lineages of the branch itself, second parent already contained in
   the judged commit and patch-ids equal, IS carried with no round spent (wo-00bd1096 /
   PR #779);
4. a chain longer than `ci.CHAIN_LIMIT` is refused and makes at most `CHAIN_LIMIT` `gh api`
   calls (assert on `fake_gh.calls`);
5. identical patch-ids carry; a merge that edits a branch file gives a different patch-id
   and is refused with `proof == "patch_id"` (needs a real git repo — build it with the
   existing `git init` helper at testing.py:2243);
6. a fetch that fails refuses with `proof == "fetch"` and merges nothing;
7. the AST test extended: `branchproof.py` runs `git` and never `gh`; `ci.WRITE_VERBS` is
   still one entry.

**`tests/test_rejudge_moved_head.py`** — owner of the round-spending policy:

8. a catch-up costs **no** round: `_rejudge_moved_head` is never reached
   (no `validation_forced` event with `by == REJUDGE_BY_OS`);
9. the carry runs with **zero rounds left** and with `validation_rejudge_declined` already
   written for that head (part 3, the two live orders' exact state);
10. `SHA_MOVED_BLOCKER` is gone on the next tick after a carry, with no ack
    (pair with `tests/test_invariants.py` if the assertion is on `rejudge_exhausted`
    directly);
11. an evil merge still re-judges: proof (b) differs, a round is opened, and at
    `max_rounds` the decline is still recorded (the fall-through must not regress).

**`tests/test_automerge.py`** — `decide` is pure and its table unchanged:

12. a carried head arms **only** when CI is green and `merge_state == CLEAN` (condition (i):
    nothing else relaxed);
13. no catch-up and no carry happens when an approval already exists for the judged sha.

**new `tests/test_branch_catch_up.py`** — part 2, its own file because it is a new
behaviour with its own guards (`test_base_heal.py` is already 670 lines):

14. a `CLEAN`-but-behind pull request is detected via `base_oid` ancestry, not `.behind` —
    the case that would otherwise silently do nothing on this repository;
15. one update per `base_oid`, a refused update spends the attempt;
16. each guard defers and writes nothing: busy worker, queued message, open round,
    `CATCH_UP_MAX` reached, head moved between the re-read and the update;
17. end-to-end: behind → update → CI green on the new head → carry → gate filed → merged,
    with zero rounds spent and one `validation_head_carried` event.

**`tests/test_timeline.py`** — 18. both new `_describe` sentences render, including a
refusal naming the proof.

**`tests/test_pr_checks.py`** — 19. the poll's read budget: the carry's extra reads happen
only on a `sha_moved` tick, and a green, up-to-date pull request pays nothing new. That
file already counts statements, so the guarantee stays executable.

## 8. Scope, coordination, open questions

**Coordinated with PR #794 (wo-8736a5c5, issue #793 — `HELD_BASE_RED` plus a base-heal path
in daemon/automerge).** This branch is based on `main` (d901a0f) and keeps edits disjoint:
all new logic is in new functions and one new module. The two overlapping edit points are
`automerge`'s held-code table (#794 adds a code; this spec adds none) and the
`not decision.armed` branch of `Daemon.auto_merge` (#794 adds a `HELD_BASE_RED` arm; this
adds a `HELD_SHA_MOVED` arm). Whichever lands second rebases those two hunks; neither
changes the other's behaviour.

**Deliberately not covered.** Squash and rebase histories (a rebase destroys the provenance
the carry rests on — `ci.update_branch` merges on purpose, ci.py:209); carrying across
anything but base merges; making `ci.update_branch` gated; any change to `max_rounds`
semantics or to `rejudge_moved_head` itself; base branches other than the pull request's own
`baseRefName`.

**Open questions for implementation.**

* Does `mergeStateStatus` ever read `BEHIND` on this repository? `ops.PR_BEHIND_NOTE`
  (ops.py:4944) says the ruleset forbids merging behind; `automerge.decide` (automerge.py:224)
  says the strict policy is off. §5.1 does not depend on the answer — the `base_oid`
  ancestry test is authoritative and `.behind` is a cheap positive short-circuit — but the
  contradiction between the two docstrings should be settled and one of them corrected.
* Whether `refs/pull/N/head` is fetchable in every project's checkout. If some project's
  `origin` refuses it, proof (b) cannot be computed and the order falls through to a
  re-judge — correct, but it makes the feature silently inert there, so
  `branchproof.fetch` failing must be logged with the ref it asked for.
* The issue body was read as relayed in the work order brief, not with `gh issue view 806`:
  this seat has no shell. If #806 asks for anything beyond parts 1-3 above, that part is
  unspecified.
