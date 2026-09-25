# An auto-merge request that proves itself, and a denial that is not silent

Work order wo-da90f283, GitHub issue #731. Repairs four defects in the gate request
`automerge.propose` files and in what the OS does after Neo denies it. Predecessor spec:
`docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md` (§5 sha binding, §5.4
what happens after a push, §7 failure directions). Nothing here changes the six positive
facts of `automerge.decide` — the mechanism that decides WHETHER to merge is correct; what
is broken is the request that ASKS, and the record afterwards.

## The problem

Live run on `jarvis_os`: the panel passed a work order, the head was the judged commit, the
pull request was green and `CLEAN`, `automerge.propose` filed the `auto_merge` gate request,
and **Neo denied it on unclear provenance**. The pull request then sat in
`waiting_pr_merge` with `needs_attention = 0` and an auto-merge line that said the merge was
*held on mergeability*, which was not what happened. Four separate defects, each with its own
evidence and its own root cause.

### 1. No provenance boundary: the OS's own request read as text inside a quoted issue

`src/jarvis/automerge.py:409` passes the work order's description into the Neo question as
free context:

```python
context=f"{wo.get('title') or ''}\n{(wo.get('description') or '')[:800]}",
```

`src/jarvis/gates.py:936` (`queue_for_review`) passes the byte-identical expression, and
`gates.build_request_question` (`src/jarvis/gates.py:781-783`) interpolates the same
description raw into the request body:

```python
description = (wo.get("description") or "").strip()
if description:
    parts += ["Work order description:", description[:1200]]
```

A work order dispatched off a tracker issue (`jarvis issues start`) has a description that
opens with *"The issue text is reproduced below"*. So the reviewer was handed a labelled
quotation with no closing delimiter, and the OS-authored
`"AUTOMATIC MERGE REQUEST — gate auto_merge"` header — which
`automerge._request_question` writes as the FIRST line of the question body, while the
description arrives through `context=` — could not be distinguished from more reproduced
issue text. Neo did exactly the right thing with what it could see: it refused to authorise
an irreversible act whose provenance it could not establish.

The root cause is not the tracker-issue wording and not the 800-character slice. It is that
**three fields of borrowed text reach reviewer prompts with no boundary at all** — the
description (both builders, twice each), `justification`, and `evidence` — and nothing in
either builder enumerates them, so the next field added inherits the same defect silently.
`gates.render_user_messages` (`src/jarvis/gates.py:733-740`) already does the right thing for
a fourth field, in prose written for that one field only; it is the proof that the rule is
known and applied one input at a time. kn-f8acf85c's rule is the opposite: the boundary
belongs at the prompt BUILDER and enumerates every untrusted field.

### 2. The request never says CI passed

`Daemon.auto_merge` (`src/jarvis/daemon.py`, the `propose` call) throws away every field of
each check except its name:

```python
automerge.propose(store, neo_store, project.name, wo, decision,
                  checks=tuple(c["name"] for c in pr.checks))
```

`automerge._request_question` then renders
`f"CI on that commit: {', '.join(checks) or 'no checks reported'}."` and `propose` puts the
same bare names in the approval's `evidence`. So the request the reviewer authorises from
lists `test, lint, evals` — three strings that are equally true of three passing checks and
three failing ones. `github.read_checks` already normalises `name`, `status` and `conclusion`
for every check run and every legacy commit status; the conclusion is dropped one line
before it would be useful. A reviewer being asked to authorise a merge cannot see that CI
passed, which — with defect 1 — is most of the case for denying.

### 3. A false sentence in the request

`automerge._request_question` ends with a hardcoded claim:

> Nothing here weakens branch protection: the five required checks still apply and GitHub
> refuses the merge on its own if they do not pass.

On this repository there is **no branch protection at all** (the predecessor spec §9 says so
explicitly: "NOT needed, and deliberately not wanted — see §4 reason 5") and there are
**three** checks, not five. Two false statements of fact in the one paragraph that tells the
reviewer what it cannot undo. Root cause: a count and a repository policy were written as
literals in a prompt instead of being read from the repository or omitted.

### 4. The denial is silent and final

The worst half, and independent of the other three.

* `Daemon.auto_merge` returns on `approval is not None and approval["status"] != "approved"`.
  Correct as far as it goes.
* `automerge.propose` never re-asks, because `merge_command` carries the judged sha and
  `latest_approval_for` matches the exact command string. Also correct, and deliberate.
* `automerge.record_verdict` writes one `automerge_decided` event and one `info` inbox row.
* **Nothing else ever moves the order.** It stays in `waiting_pr_merge`, `needs_attention`
  stays 0, and `invariants.true_blockers` has no branch that derives anything from a denied
  `auto_merge` verdict. `record_verdict`'s docstring states this as intended: *"Nothing is
  flagged for attention, because 'a human merges this one' is not a problem"*, and
  `invariants.SHA_MOVED_BLOCKER`'s comment repeats the aside: *"a held auto-merge is still
  deliberately not an attention item"*. Both paragraphs are about a HOLD. A hold is the OS
  declining to act on facts that may change next tick; a DENIAL is a reviewer refusing, for
  ever, on a commit that will never be re-proposed. The inbox row is a notification, not a
  flag: `jarvis status` reads attention, the user reads `jarvis status`.
* And the one surface that did say something said the wrong thing.
  `ops.automerge_state` takes the newest event by timestamp across `AUTOMERGE_EVENTS`, with
  `AUTOMERGE_TERMINAL` (`automerge_merged`) as the only exception. One tick of GitHub
  answering `mergeable: UNKNOWN` writes `automerge_held` / `HELD_NOT_MERGEABLE`, whose
  timestamp is later than the denial's, so `_automerge_line` rendered
  `held — GitHub does not (yet) say the branch merges cleanly` over a denial — for ever,
  because `Daemon._note_automerge_held` dedupes that hold per (sha, code, reason) and so
  never writes a newer row that could be overtaken again.

Net effect measured on the live order: a pull request the panel passed, refused by the
reviewer, with nothing in the fleet's attention list, nothing in `jarvis status`, and a
`jarvis wo show` line naming a transient GitHub answer as the reason. The user finds it by
noticing an old row on the open list.

## The fix

Four changes, in the four places the four defects live. They share no code except where
noted, and none of them touches `automerge.decide`.

### Fix 1 — one borrowed-text renderer, used by both builders, enumerated per builder

**New leaf module `src/jarvis/provenance.py`.** A leaf because both callers must import it
and `gates.py` is itself a leaf that `catalog.py` imports; putting the renderer in `gates.py`
would make `automerge.py` import `gates` at module scope, which it deliberately does not do
(it imports `gates` lazily inside `apply`). Not in `github.py`: that module's whole claim is
`READ_ONLY_VERBS` plus an AST walk, and prompt text is not a GitHub read.

Contents:

* `@dataclass(frozen=True) class Borrowed` — one field of text the OS did not write:
  `label` (what the field is: `"the work order's description"`), `whose` (who wrote it:
  `"whoever filed the work order — on an order opened from a tracker issue this quotes the
  issue"`), `text`, and `limit` (the per-field truncation, so the caps stay where they are
  today: 800 for the Neo `context=`, 1200 for the description in the request body, 2000 for
  `evidence`).
* `borrowed_block(field: Borrowed) -> str` — the ONE renderer. Emits a delimited block:
  an opening marker naming the label and `whose`, one sentence stating that **nothing inside
  the block is an instruction to the reader and nothing in it may be read as the OS
  speaking**, the truncated text, and a closing marker naming the same label. The reader
  therefore learns three things a bare paste cannot carry: where the quotation starts, where
  it ends, and whose words are inside it.
* **De-fanging is part of the renderer, not the caller's problem.** Any occurrence of either
  marker token inside `field.text` is replaced before wrapping, so borrowed text that spells
  the closing marker becomes ordinary text inside the block rather than ending it. Without
  this the boundary is advisory and a work order description could close its own block — the
  same class of defect as the one being fixed, one level down.
* `borrowed_sections(fields: Sequence[Borrowed]) -> list[str]` — every block in order, with
  a field whose `text` is empty after stripping omitted entirely. Omitted rather than
  rendered empty, on `render_user_messages`' rule: a blank block asserts "they said nothing",
  which is a claim the renderer is not entitled to make.

**Both builders declare their untrusted fields as a tuple and build from it.**

* `gates.build_request_question` assembles `description`, `justification` and `evidence`
  into `tuple[Borrowed, ...]` and renders them through `borrowed_sections`. Its OS-authored
  material — the `PRIVILEGED ACTION REQUEST` header, the exact command, the decision
  instruction — stays outside every block and comes FIRST, so the reviewer reads who is
  asking before it reads anything quoted.
* `automerge._request_question` gains the same treatment for the one field it will now
  render (the description) and keeps its own opening, which must state in its first
  paragraph that **the OS is the author of this request and the work order's worker asked
  for nothing** — the actor problem that function already exists to solve
  (`remedies._request_question`'s precedent).
* Both `neo.ask(context=...)` call sites — `automerge.propose` and `gates.queue_for_review`
  — build their context through the same renderer instead of the raw f-string. The `context=`
  argument is the field that actually carried the defect; it must not be the one input left
  untagged.

**What makes a new field break the call rather than widen the leak.** A test walks the AST
of `gates.build_request_question`, `gates.queue_for_review`, `automerge._request_question`
and `automerge.propose` and asserts that no expression reading `wo["description"]`,
`wo.get("description")`, `justification`, `evidence` or `context` reaches a returned string
except as an argument to a `Borrowed(...)` construction. This is `automerge.WRITE_VERBS`'
mechanism pointed at prompt text: a builder that grows a field cannot ship without a commit
that also edits the test. A convention in a docstring was what failed here; an assertion over
the builder is what replaces it.

**Rejected:** sanitising the description at the WRITE site (`ops.create_work_order`,
`issues.start`) so no quoted issue ever reaches a prompt. Loses because the description is
the worker's brief and must stay verbatim, and because the risk is not this one wording — it
is every borrowed field, in every builder, for ever. **Also rejected:** dropping the
description from the auto-merge request entirely. It is cheap and it would have fixed this
instance, but the reviewer legitimately uses the description to judge whether the merge is
what the order was for, and the same untagged path would still be live in
`build_request_question`, which files far more requests.

### Fix 2 — carry each check whole, and render its conclusion

* `Daemon.auto_merge` passes `checks=pr.checks` — the `tuple[dict[str, str], ...]` that
  `github.read_checks` produced — instead of a tuple of names.
* `automerge.propose` and `automerge._request_question` take
  `checks: tuple[dict[str, str], ...] = ()`, i.e. `read_checks`' keys (`name`, `status`,
  `conclusion`, and the two it does not read). Typed as that shape and documented as
  `read_checks`' output, so the one normaliser stays the only place a check is parsed.
* **One new renderer, `automerge.checks_evidence(checks) -> str`**, used BOTH in the question
  body and for the approval row's `evidence=`. One function because the two strings are one
  claim: a gate request whose body and whose stored evidence describe CI differently is a
  record that cannot be audited afterwards. It renders, per check,
  `name: conclusion (status)`, and `"no checks reported"` for an empty tuple — which is not
  the same fact as green and must not read as it.
* **`is CI green` is NOT re-derived here.** `github.failing_checks` /
  `PullRequest.checks_green` remain the single predicate, and `automerge.decide` condition 6c
  remains the only place it is consulted. `checks_evidence` renders and asserts nothing; a
  second opinion on greenness in this module is exactly issue #224's shape — the OS judging a
  submission by a standard it does not police.

**Rejected:** rendering only the failing checks. The reviewer's question is "did CI pass",
and an empty list is indistinguishable from a list that was never read.

### Fix 3 — the protection sentence: branch A, derive it (Neo question 601, RULED)

The hardcoded sentence goes, and with it the literal `five`. **No count of checks and no
claim about repository policy may be a literal in this module** — that is the rule this fix
establishes.

**Neo ruled for BRANCH A — derive branch protection — with three binding conditions.**
Branch B (claim nothing) is RETIRED and is not implemented; it is kept below as one
paragraph of record only.

1. **The AST test pins the reader to GET-only arguments.** The `gh api` argument list this
   OS builds must spell a literal `--method GET` and must contain none of `-X`,
   `--request`, `-f`, `-F`, `--input`.
   (`tests/test_github_artifact.py::test_every_api_call_this_module_builds_is_a_get_and_can_be_nothing_else`,
   with a negative control beside it.)
2. **Only a 404 whose body says the branch is not protected renders as "no protection".**
   A 403, any other error and a timeout render "the OS could not read branch protection",
   and NEITHER of those ever blocks the proposal: the request is still filed, without a
   protection claim the OS cannot make. `github.NOT_PROTECTED_RE` is the one reading that
   yields `None`; every other outcome raises `GitHubError`, which `Daemon._protection_fact`
   turns into `automerge.PROTECTION_UNREADABLE`.
3. **All three renderings are fixture-tested**: protected (the required checks GitHub
   reports, named and counted off the payload), not protected (stating plainly that this
   gate and `--match-head-commit` are the only things standing between the commit and the
   default branch), unreadable.

Shared by both: `automerge._request_question` takes a keyword argument
`protection: str = ""` — a rendered *protection fact*, or empty for "the OS holds no fact
about branch protection". The paragraph that today makes the false claim renders
`protection` when it is non-empty and omits the sentence entirely when it is empty. Every
other sentence in that paragraph (the squash lands on the default branch; no branch is
deleted; the work order completes) is a fact the OS already holds and is unchanged.
Whichever branch Neo picks is therefore an edit to **one caller and one reader**, not to the
request's structure.

**Branch A — derive the truth. CHOSEN.**
`github.py` gains one read-only reader:

* `branch_protection(owner_repo, base, cwd=None) -> BranchProtection | None`, returning a
  frozen dataclass with `required_checks: tuple[str, ...]` and nothing else it does not
  need; `None` ONLY when GitHub answers a 404 whose body says the branch is not protected
  (**the state this repository is in** — condition 2) and raising `GitHubError` on any
  other failure, like every other reader in the module. `base` is checked against
  `BRANCH_RE` before it becomes a path segment, `checked_pr_url`'s reason.
* It runs `gh api --method GET repos/{owner}/{repo}/branches/{base}/protection`, so
  `READ_ONLY_VERBS` gains `("api", "--method")` and the argument list always spells
  `["api", "--method", "GET", path]`. The AST test in `tests/test_github_artifact.py` is
  extended with a second assertion for this verb: any argument list opening with `api` must
  carry the literals `--method` and `GET` in the next two positions. Without that assertion
  the pair `("api", ...)` would admit every mutation `gh api` can perform, and the module's
  one claim — everything in here is a question — would become unverifiable. The path is a
  dynamic f-string and the assertion is about the method, which is what makes the check
  possible at all.
* `base` comes from `PullRequest.base_ref`, which `PR_FIELDS` already fetches. `owner_repo`
  comes from `github.origin_repo(cwd)`, the existing reader.
* `Daemon.auto_merge` calls it on the tick that proposes — that tick is rare (once per judged
  commit, guarded by `latest_approval_for`), so the extra round trip is not on the two-minute
  path. A `GitHubError` means the fact is unknown: `protection=""` and the sentence is
  omitted. **An unreadable protection API must never become a claim in either direction.**
* Rendered sentence with protection present: the required checks by name and their count
  taken from the payload. With protection absent (`None`): a sentence saying plainly that
  the base branch carries no protection rules, so the only review this merge has had is the
  panel's and this gate's — which is the honest version of the sentence being deleted, and
  is *more* reason for the reviewer to read the evidence, not less.

**Branch B — claim nothing about protection. RETIRED, kept as record.** It would have had
`Daemon.auto_merge` pass no `protection` and add no reader. Neo chose A: the reviewer
learns whether anything *other* than this gate is checking the merge, which on a repository
that DOES protect its base is material. `protection=""` — the OS holding no such fact —
still renders no sentence, which is what keeps the cost of A one caller and one reader.

### Fix 4a — a denial outranks a later hold about the same commit

In `ops.automerge_state`, after the newest-by-timestamp pick and before the terminal
exception is applied:

* **A non-approved `automerge_decided` is sticky for the commit it was bound to.** If the
  newest non-terminal event is an `automerge_held` whose `head_sha` equals the sha of the
  newest `automerge_decided` whose `decision` is not `approved`, the DECIDED event is the
  state and `_automerge_line` renders the denial. A verdict is a permanent fact about one
  commit; a hold is a transient one about the same commit, and the transient one must not
  bury it.
* **It does NOT win over a hold about a DIFFERENT commit.** A hold whose `head_sha` differs
  means the head moved: that is a new submission, the denial does not describe it, and
  `HELD_SHA_MOVED` (or the re-judged round that follows it) is the news. This is the same
  rule `rejudge_exhausted` applies to its own dedupe, and it is what makes the stickiness
  self-clearing instead of permanent.
* `AUTOMERGE_TERMINAL` stays above everything, unchanged: nothing follows a merge.
* An APPROVED `automerge_decided` gains no stickiness whatever. That is the case
  `automerge_state`'s docstring already argues about — an approval outranking every later
  event is how a pull request came to say "approved by neo" while nothing was going to
  merge — and this fix must not reintroduce it. The asymmetry is the point: an approval is
  followed by a merge attempt that writes its own events; a denial is followed by nothing.

**The sha a denial was bound to.** `record_verdict` today writes `approval_id`, `decision`,
`by`, `reason` and `command` — the sha is inside `command`
(`--match-head-commit <sha>`) and nowhere else. So:

* `record_verdict` additionally writes `head_sha` on the `automerge_decided` payload.
* `automerge.sha_of_command(command) -> str` is added as the one parser paired with
  `merge_command`, next to it, and is used to read the sha off rows written before this
  change. Two spellings of "which commit did this grant cover" is how the pair comes to
  disagree; one renderer and one parser, adjacent, is the same discipline `merge_command`
  already argues for.
* Readers take `payload["head_sha"]` and fall back to `sha_of_command(payload["command"])`.
  A row with neither is not a denial anyone can act on and is ignored — silently, because it
  predates the record keeping the flag depends on.

### Fix 4b — a denied auto-merge is an attention item, derived and ack-able

`invariants.py` gains one blocker constant and one derivation.

* **`AUTOMERGE_DENIED_BLOCKER`** — free of any elapsed time and of any sha, on
  `PARKED_BLOCKER`'s rule: `ProjectStore.ack_attention` stores the string verbatim and
  INV-ATTENTION-REASON compares it, so a reason that ticked or that named a commit could
  never be acknowledged. It must name **Neo's refusal as the reason** — not "the merge did
  not happen" — and give the three routes forward: merge it by hand, push a fix and
  `jarvis validation force`, or `jarvis wo done`. Three routes because they answer three
  different situations: the reviewer was over-cautious, the reviewer was right about the
  code, and the order should be closed against a pull request nobody will land.
* **`automerge_denied(store, wo) -> bool`** — the derivation, shaped exactly like
  `rejudge_exhausted` above it:
  1. the newest `automerge_decided` event exists and its `decision` is `denied` or
     `dismissed`. Both, because `apply` refuses on anything but `approved` and a dismissal
     therefore stalls the order exactly as a denial does — while meaning the reviewer reached
     for the wrong verb, which is *more* reason to put it in front of the user. An
     ESCALATED request writes no `automerge_decided` row and already reaches the user
     through `store.escalated_approvals` in `true_blockers`; this must not double-flag it.
  2. the sha that verdict was bound to (fix 4a) is still the commit the panel's verdict
     names: `store.validated_head(store.latest_validation_round(wo_id=wo["id"]))`. The same
     single source of "which commit did the panel accept" that `decide` is forbidden to
     re-derive.
  3. nothing has since bound a verdict to a different commit — guaranteed by (1) reading the
     NEWEST decided row — and no `automerge_held` newer than that row carries
     `HELD_SHA_MOVED` with a different `head_sha`. Condition 3 is what makes a push clear the
     flag within a tick: the poll writes the moved-head hold, this returns False, and the
     re-judge path (`Daemon._rejudge_moved_head`) owns the order again.
* In `true_blockers`, gated on `wo["status"] == "waiting_pr_merge"` — so no other work order
  pays for the reads — immediately after the `rejudge_exhausted` branch. The two cannot both
  be true (that one requires the newest hold to be `sha_moved` on the judged head, this one
  requires no such hold), so the order is documentary rather than a ranking anyone relies on.
* **DERIVED, NEVER WRITTEN.** `record_verdict` raises no flag and sets no
  `attention_reason`: kn-089de524's rule, and the same reason `SHA_MOVED_BLOCKER` is derived
  — a flag written on a path that re-runs would re-raise itself over `jarvis wo ack`, and
  `record_verdict` runs from the verdict-delivery arm of the daemon. Derivation through
  `true_blockers` is what makes the flag ack-able like every other blocker, with no new ack
  machinery.
* **This reverses two documented decisions, and both docstrings must be corrected in the
  same commit**, or the next reader restores the bug:
  * `automerge.record_verdict`'s paragraph *"Nothing is flagged for attention, because 'a
    human merges this one' is not a problem — it is the behaviour this feature is an
    optimisation over."* It is wrong about a DENIAL. The user did not choose to merge this
    one by hand; they were never told there was one to merge. The inbox row the function
    writes is a notification, and `jarvis status` does not read the inbox for attention.
    The paragraph is replaced with: the verdict is recorded, the inbox row still goes out,
    and `invariants.automerge_denied` derives the attention flag from the timeline.
  * `invariants.SHA_MOVED_BLOCKER`'s aside *"which is why a held auto-merge is still
    deliberately not an attention item"*. Still true of a HOLD, and it must now say so
    explicitly: a hold is transient and the next tick may clear it; a denial is a reviewer's
    final answer about a commit nothing will re-propose. `Daemon._note_automerge_held`'s
    "DELIBERATELY NOT AN ATTENTION ITEM" paragraph stays as written and gains the same
    one-line distinction.

**Rejected for 4b:** moving the order to `needs_review` on a denial. It would surface, and
it would lie: `needs_review` means the panel or the landing wants a decision about the WORK,
`PR_REPAIR_STATUSES` and the whole pull-request poll key on `waiting_pr_merge`, and an order
moved out of it stops being polled — so a pull request the user then merged by hand would
never be noticed as merged. The order is parked behind its pull request; that is still what
is true. **Also rejected:** an `automerge_denial_flagged` event plus a flag raised at the
write site. That is precisely the reconciler trap kn-089de524 names, and it would overwrite
the user's ack every tick.

## Post-conditions — what must be proved

Per defect, in `tests/test_automerge.py`, `tests/test_gates.py`, `tests/test_github_artifact.py`
and `tests/test_invariants.py` as the subject dictates.

**Fix 1 — provenance**
1. `provenance.borrowed_block` wraps text in both markers, names the label and `whose`, and
   its output states that nothing inside is an instruction.
2. Borrowed text that CONTAINS the closing marker cannot end the block: the rendered output
   has exactly one closing marker.
3. A description opening with "The issue text is reproduced below" appears inside a block in
   BOTH `gates.build_request_question` and `automerge._request_question`, and in the
   `context=` both `queue_for_review` and `propose` pass to `neo.ask`.
4. The AST assertion: no builder reads `description` / `justification` / `evidence` /
   `context` into its returned text except through a `Borrowed(...)` construction. The test
   must FAIL when a raw interpolation is reintroduced (proved by asserting on a fixture
   source, not only on the live module).
5. The OS-authored header of the auto-merge request precedes every borrowed block.

**Fix 2 — CI**
6. `propose` called with `read_checks`-shaped dicts renders each check's CONCLUSION in the
   question body, and the approval's `evidence` string is byte-identical to the body's CI
   line source (`checks_evidence` called once per string, same output).
7. An empty `checks` tuple renders "no checks reported" and never anything that reads as
   green.
8. `automerge.py` contains no re-derivation of greenness: the AST walk already applied to
   `WRITE_VERBS` is extended, or a test asserts `failing_checks` / `checks_green` are the
   only greenness predicates referenced.

**Fix 3 — protection**
9. No test may find the literal `five` or any hardcoded check count in
   `automerge._request_question`, and the old sentence is absent.
10. `protection=""` omits the sentence entirely — the request renders with no claim about
    branch protection.
11. `branch_protection` returns `None` on a 404 that says the branch is not protected and
    the request then states that the base carries no protection; a 403, any other error and
    a timeout render "the OS could not read branch protection" and block nothing; the AST
    test refuses an `api` argument list without literal `--method GET`.

**Fix 4a — the line**
12. A denial followed by an `automerge_held` on the SAME sha with a LATER timestamp still
    reads as denied: `ops.automerge_state`'s `kind` is `automerge_decided` and the line names
    the denial, not the hold. This is the exact live regression.
13. A denial followed by an `automerge_held` with `HELD_SHA_MOVED` on a DIFFERENT sha reads
    as the hold.
14. `automerge_merged` still wins over a later event of any kind.
15. An APPROVED verdict followed by a hold on the same sha reads as the HOLD — the asymmetry,
    pinned so a future edit cannot generalise 4a into the bug `automerge_state`'s docstring
    already warns about.
16. `sha_of_command(merge_command(url, sha)) == sha`, and a legacy `automerge_decided` row
    carrying only `command` still resolves its sha.

**Fix 4b — the attention**
17. A denied verdict on the panel's current judged head, in `waiting_pr_merge`, puts
    `AUTOMERGE_DENIED_BLOCKER` in `true_blockers[0]` and makes the order an attention item
    through INV-ATTENTION-MISSING.
18. The same state, then a push (new head): the next derivation returns False and the flag
    clears itself — both via condition 2 (validated head moved) and via condition 3 (a newer
    `sha_moved` hold on a different sha), each tested separately, because either alone must
    be sufficient.
19. `jarvis wo ack` puts the flag down and the next reconcile tick does NOT re-raise it —
    the kn-089de524 regression test, in the shape `tests/test_rejudge_moved_head.py` already
    uses.
20. A `dismissed` verdict flags identically; an ESCALATED request flags ONCE (through
    `escalated_approvals`) and never twice.
21. `record_verdict` writes no `attention_reason` and no flag of its own, and still writes
    its inbox row.
22. INV-ATTENTION-REASON does not relabel an order flagged with this blocker — i.e. the
    constant is re-derivable, which is the obligation every blocker string in
    `invariants.py` carries.

## What this spec deliberately does NOT do

* It does not re-propose a denied commit, ever. `merge_command` carrying the judged sha, and
  `propose` refusing on an existing approval for that command, stay exactly as they are: a
  reviewer's refusal of one diff must not be re-asked every two minutes. The user's routes
  forward are the three in the blocker string.
* It does not change `automerge.decide`, `GRANT_USES`, the merge command, or any of the six
  positive facts. The mechanism that decides whether to merge was not the defect.
* It does not give the OS a way to merge without a gate, and adds no write verb anywhere.
  Branch A adds one GET-only read; branch B adds nothing.
* `gates.build_contest_question` IS covered after all — the lead's recorded assumption
  settled the one open question here. It renders borrowed text by the same route as
  `build_request_question`, so it sits inside the provenance boundary and inside the AST
  assertion, on the same rule that put the boundary at the builder in the first place.
