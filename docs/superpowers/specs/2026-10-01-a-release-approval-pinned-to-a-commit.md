# A release approval pinned to a commit

Work order wo-33e1d0b4. Design ruled by the user and by Neo, question 1145 (Option A).

## The problem

A release gate grant authorises a **byte-exact command string** for 60 minutes
(`gates.GRANT_TTL_SECONDS = 3600`, `src/jarvis/gates.py:111`; the scope rule is
`gates.py:33` — "Matching is on the exact command string… a grant is a receipt for a
specific act"; spent in `gates.open_gate`, `src/jarvis/gates.py:1173`; and told to the
worker verbatim by `gates.approved_message`, `src/jarvis/gates.py:1231`). It is scoped to
no commit at all. kn-672cf420 and kn-e25827ea are the recorded form of that rule.

`scripts/shipit.sh` meanwhile refuses to run unless the local HEAD **is** `origin/main`
(`scripts/shipit.sh:153-156`), because releases are cut from already-merged main. The two
rules together mean the approved string stops being runnable the moment `main` moves.

Live case, this very order: gate 347 was approved after Neo verified all three CI checks
green on main head `442729f`. `main` advanced to `535b93c` (PR #884) before the verdict
landed. The approved command then exited 1 at its first precondition:

```
✗ HEAD is not origin/main — land the code on main via a merged PR and pull first (local 442729f vs origin/main 535b93c)
```

The naive repair — fast-forward the worktree and run the approved command anyway — is the
defect **issue #837** exists to fix: "an auto-merge approval is executed hours later
without re-checking that the PR's CI still covers main, so stale-base merges keep breaking
main". Shipping `535b93c` under a grant issued for `442729f` ships content no reviewer ever
saw CI pass on, and nothing in the record would show the substitution.

Root cause: **the authorisation and the artifact are not bound.** The reviewer decides about
a commit; the grant records a string; the script derives its content from whatever
`origin/main` happens to be at run time. Three different referents, and the window between
them is minutes wide on a repo whose own fleet merges into it.

kn-49e6d8ee records the OPPOSITE rule for `pr_merge` gates — "the approval is scoped to the
string, not to the sha, so a branch update does not invalidate it" — and it is right there.
A `pr_merge` grant names a pull request, and GitHub re-runs required checks on the merged
result, so a branch update is still reviewed by something. A release grant names no
artifact: `shipit.sh` is a cut-and-deploy of whatever is in front of it, and the production
fleet is downstream with nothing between. The asymmetry is the point — for `pr_merge` a
string is enough because another system holds the sha; for `release` nothing else does.

## The fix

Pin the sha into the command string. The existing byte-exact grant then becomes
hash-scoped at zero cost: **nothing in `gates.py` changes, and `approvals` gets no new
column.** That is the load-bearing simplification of this whole spec — the sha travels in
`approvals.command`, which is already the thing the reviewer reads, the thing
`usable_grant` matches, and the thing `approved_message` quotes back.

The 60-minute TTL is **unchanged**. "This authorisation is stale in time" and "this
authorisation is for a stale base" are separate properties; one of them is now expressible
and the other already was.

### 1. `scripts/shipit.sh --base <sha>`

Argument parsing, `scripts/shipit.sh:54-62`. The loop's last arm is a catch-all
(`*) BUMP_OR_VERSION="$1"`), so without its own case `--base` would be swallowed as the
version and die at `:214` with `invalid version/bump: '--base'`. One new case beside
`--wo`, which already has the shape:

```bash
--base)    shift; BASE_SHA="${1:-}" ;;
```

`BASE_SHA=""` initialised with the other four at `:50-53`.

Two pure-argument checks in the validation block at `:63-67`, before any git call, next to
the existing `--stage requires --wo`:

* **required in `--stage` mode.**
  `die "--stage requires --base <sha>: a release approval is scoped to the command string, so the commit being shipped has to be in it"`
* **full sha only** (`[ "${#BASE_SHA}" = 40 ]` and `[0-9a-f]` only):
  `die "--base takes a full 40-character sha, not '$BASE_SHA' — an abbreviation names one commit today and can name another later"`

### 2. The precondition it replaces

`scripts/shipit.sh:146-156` is the origin-sync block. The fetch (`:150`) and the
`origin/main` existence check (`:151-152`) stay as they are — `--base` is checked
*against* `origin/main`, so both are still needed. The HEAD equality test at `:153-156`
forks on the FLAG, not on the mode:

```bash
if [ -n "$BASE_SHA" ]; then
  git cat-file -e "$BASE_SHA^{commit}" 2>/dev/null \
    || die "--base $BASE_SHA is not a commit in this checkout — fetch origin and pass a sha from origin/main"
  git merge-base --is-ancestor "$BASE_SHA" origin/main \
    || die "--base $BASE_SHA is not an ancestor of origin/main — releases are cut from already-merged main
     (origin/main is at $(git rev-parse --short origin/main))"
elif [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  die "HEAD is not origin/main — …"        # :154-155 verbatim
fi
```

Existence is checked before ancestry because `merge-base --is-ancestor` on an unknown
object exits non-zero with a git error on stderr, which would report the wrong fault.

**The local HEAD is no longer constrained at all** when `--base` is given. Nothing about
the working checkout is an input to a release: the branch is cut from a sha, the bump
happens in a throwaway worktree, and production deploys the tag from origin — the script
says so itself at `:105-107`. That comment needs its "cut from the origin/main COMMIT"
reworded to "cut from the commit named by `--base`".

**The clean-tree guard at `:116-136` is unchanged**, including the `OS_MANAGED_PATHS`
allowance at `:116` (`OPERATION.md`, `ASSUMPTIONS.md`, `.claude/settings.json`,
`.serena/project.yml` — dirtied by the OS and by Serena in the very checkout a release runs
from, printed by name at `:129-132`, with anything outside the list a hard `die` at
`:133-136`). It asks "do you have uncommitted work you care about", which is a question
about the operator and not about the base.

Why `--base` is **accepted outside `--stage` too**, rather than refused there: the script
has exactly one precondition block and exactly one place where the shipped commit is
resolved, and its only mode forks are `run()` (`:79`) and the `--stage` exit at `:410`.
Refusing the flag outside stage mode would mean a second `if [ "$STAGE" = 1 ]` inside the
preconditions guarding behaviour that is otherwise identical — a fork with no difference
behind it. The flag's PRESENCE selects the precondition; `--stage` only decides whether its
absence is an error. A human running the script by hand with no flag keeps today's
behaviour byte for byte, which is why the `elif` above is the old test verbatim rather than
a relaxation to ancestry for everyone: ancestry alone would let a stale checkout silently
release an old main, and nobody asked for that.

### 3. The release branch is cut from that sha

`scripts/shipit.sh:222` is the single ref the rest of the script hangs off:

```bash
MAIN_SHA="$(git rev-parse HEAD)"
```

It becomes `SHIP_SHA="$(git rev-parse "${BASE_SHA:-HEAD}")"`, renamed because "main's sha"
stops being true. Six sites, all mechanical: `:222` (assignment), `:223` (the `Releasing…`
line, which should now print the sha's own short form and say `base:` rather than
`from $BRANCH`), `:229` (`git branch '$REL_BRANCH' '$SHIP_SHA'` — the branch cut), `:271`
(comment), `:286` and `:289` (`release_notes`' range).

The throwaway worktree at `:234-238` needs no change of its own: `git worktree add --quiet
'$WT' '$REL_BRANCH'` takes the branch, and the branch is now at `$SHIP_SHA`. **That is the
whole mechanism** — the bump commit, the tag, the pushed branch and production's checkout
all descend from the sha the reviewer approved, and commits merged after the request ride
the next release.

Version resolution is untouched: `latest_tagged` (`:159-162`) over `jarvis-*` tags,
`BASE`/`VERSION` at `:209-215`. A pinned base does not change which number comes next.

Two consequences, both correct and both worth stating so no one reads them as bugs:

* **The release notes get shorter.** `release_notes` (`:285-303`) logs
  `$PREV_TAG..$SHIP_SHA`, so what merged after the base is excluded. It is not lost: the
  next release's range starts at this tag, and by the reachability argument already written
  at `:268-273` everything the base held is an ancestor of it, so the skipped commits
  appear in the next release's notes exactly once.
* **The `warning: shipping from '$BRANCH'` line at `:83-84` stops meaning anything** when
  `--base` is set, and a daemon-filed release runs from a `wo-*` worktree branch, so it
  would fire on every single one. Skip it when `BASE_SHA` is non-empty; a warning that
  always fires teaches readers to ignore a line that elsewhere matters.

### 4. Who resolves the sha, and when: the WORKER, at gate-request time

The newest commit on `origin/main` that (a) contains every payload merge in this order's
batch and (b) has green CI.

**Not the OS at dispatch.** `daemon.py:7449-7451` says the batch "lives in `metadata`
rather than in a column because it is a list that grows while the order waits"
(`Daemon.RELEASE_BATCH_KEY` = `release.BATCH_KEY`, grown by `Daemon.ensure_release`,
`daemon.py:7506-7522`). Any sha chosen at dispatch is wrong the moment a second fix joins
the batch. Gate-request time is the latest moment before the authorisation exists, which is
the only moment at which the sha and the approval can agree.

What happens if the batch grows AFTER the request: the release ships without the late fix,
and `Daemon.settle_shipped_releases` / `_settle_shipped_release`
(`daemon.py:7682`, `:7729`) correctly leaves the order open, because
`release.tags_containing` (`release.py:453`) intersects one `git tag --contains` per
payload sha and the new tag carries only some of them. The late fix is then picked up by
the next release and that order settles by the existing overtaken path. No new code, and
the failure mode is "ships one release later", never "ships unreviewed".

### 5. `Daemon.RELEASE_BRIEF` teaches the flag

`daemon.py:7464-7480`. It currently prescribes, verbatim,
`scripts/shipit.sh --stage --wo {wo_id}` and closes with "Check `main` is green before you
ask." Unchanged, every future release order asks for the unpinned command and gets today's
defect back; the brief IS the interface.

The command line becomes

```
    scripts/shipit.sh --stage --wo {wo_id} --base <sha>
```

and the closing paragraph is replaced by an instruction to resolve the sha rather than a
request to eyeball the branch: the newest commit on `origin/main` carrying every fix listed
above whose CI is green, named in full, resolved BEFORE the gate request so the reviewer
and the command agree on one commit; a red or unreadable base means say so and stop. Keep
the existing "do not invent a second release path / do not drop `--stage`" paragraph
(`:7474-7477`) and add that dropping or changing `--base` after approval invalidates the
grant — which is now true by construction rather than by instruction.

Same text in the two other places that prescribe the command, or a reader will follow the
stale one: `.claude/skills/shipit/SKILL.md:108` and `docs/DEPLOYMENT.md:93`.

### 6. Nothing else in the tree assumes the branch sits at main's tip

Checked, each with the reason it survives:

| Site | Affected? |
|---|---|
| `release.tags_containing` (`release.py:453`) | No. Pure ancestry, `git tag --contains <payload sha>`, one call per sha intersected. Its own docstring: release tags are descendants of main, never ancestors (kn-179cd767), and "main is ahead of the tag" decides nothing (kn-4f5aaa2b). A tag cut from an older base is exactly the case it already handles. |
| `release.overtaken_by` (`release.py:524`) | No. Reads `tags_containing`, then tests MEMBERSHIP of production's ref in those tags. The docstring already names this case: "a later tag cut from an older base does not contain the fix, so production running it leaves the order open" — the honest answer, and §4's. |
| `release.verify_release_claim` (`release.py:566`) | No. Production version + ref, an approved `release` grant with `uses > 0`, and the tag post-dating the authorisation. Pinning the sha strengthens check 2's evidence (the command now names the commit) and changes none of the three tests. |
| `pending_release.json` (`shipit.sh:410-431`, `release.read_marker`/`write_marker`, `release.py:144`,`:160`) | No. Carries `wo_id`, `project`, `version`, `tag`, `staged_at`, `state`. Nothing in it references a base. OPTIONAL and recommended: add `"base": "<sha>"` to the printf at `:419-420` as audit record — every consumer reads by key (`invariants.check_release_marker`, `invariants.py:3634`, is key-tolerant and `write_marker` round-trips the dict whole), so an extra key costs nothing. |
| `Daemon.settle_shipped_releases` / `_settle_shipped_release` (`daemon.py:7682`,`:7729`) | No. "A `jarvis-*` tag contains every payload commit of the batch AND production runs that tag" is ancestry plus membership, both base-independent. §4 covers the batch-growth interaction. |
| `Daemon.hold_red_release` (`daemon.py:7555`) | No. A DISPATCH filter reading `ci.base_runs` for the branch. Still the right pre-check; the sha is resolved later, by the worker. |
| `invariants.check_production_clean` (`invariants.py:3822`) | No. Compares the production checkout against its tag. |
| `invariants.check_gate_canaries` (`invariants.py:3580`) | No, but see §7. |
| `ops.py` | No release-cut assumption anywhere. Its only `origin/main` mention is `:8761`, advice about rebasing a spec branch. |

### 7. `gate_rules.py`: still a `release`, plus one canary

`SEED_MATCHES` has `("release", r"shipit")` (`gate_rules.py:1296`) — an unanchored
substring, so `scripts/shipit.sh --stage --wo wo-33e1d0b4 --base 442729f…` classifies as
`release` with the extra flag present and no recogniser changes. `--base` carries no verb
that could reclassify it: `push_protected` needs `git push … main`, `pr_merge` needs
`gh pr merge`. The release `GateKind`'s `conflict_markers` (`gate_rules.py:188-189`) are
untouched.

Add ONE canary to `SEED_CANARIES` (`gate_rules.py:1316-1361`), in the release group after
`:1321`:

```python
("release", "scripts/shipit.sh --stage --wo wo-1234abcd "
            "--base 442729f0c1e4b7a9d3f5068b2c4e7a1d9b0f3c58"),
```

Why it earns a line when the group already has nine: the canaries are the set every
proposed exemption is run against before admission, and the pinned command is now **the
only release command the OS itself will ever ask for**. It is also the longest and most
argument-shaped of them, which is precisely the shape a learned exemption about "a long
quoted argument" would most plausibly clear by accident. Ids are content-derived
(`gate_rules.seed_id`), so the new row appears on first use with no migration.

Out of scope: the release kind's `evidence_ask` (`gate_rules.py:187`, "the merged PRs, the
commits being tagged, and CI's verdict on them") already asks for the commits, and the
reviewer now reads the sha in the command itself.

### Rejected

* **Fast-forward the checkout and run the approved command.** Issue #837 exactly: it ships
  content under an approval granted for different content. This is the alternative the
  order exists to refuse.
* **Re-check the base at run time inside `shipit.sh` and abort when main has moved.** Fails
  closed, so it is not dangerous — but it converts every release into a race against the
  fleet's own merges and gives the reviewer no way to authorise a specific commit. The
  outcome is a release that can only ship when nothing else is landing.
* **A `base_sha` column on `approvals`, matched by `usable_grant`.** A schema migration,
  a second thing that can disagree with the command string, and a new question in
  `gates.py` ("which of the two is authoritative"). The string already carries it.
* **Shorten the grant TTL so the window cannot open.** Treats a correctness property as a
  latency problem. Neo's review takes minutes by construction; a TTL short enough to make
  the race unlikely is short enough to expire honest approvals, and the race would remain.
* **Re-run CI on the release branch before deploying.** Minutes of wall clock inside a gate
  window, duplicating the checks that already passed on the merged base, and still silent
  about WHICH commit was approved.

## Tests

1. `test_stage_requires_a_base_sha` (`tests/test_shipit.py`) — `--stage --wo wo-x` with no
   `--base` exits non-zero naming `--base`, beside the existing
   `--stage requires --wo` case.
2. `test_base_must_be_a_full_sha` — a 7-character abbreviation exits non-zero naming
   "40-character"; no git call is needed to fail.
3. `test_base_must_be_an_ancestor_of_origin_main` — commit on a side branch, pushed
   nowhere: exits non-zero naming `origin/main` and printing origin/main's short sha.
4. `test_an_unknown_base_says_so_rather_than_failing_ancestry` — a syntactically valid sha
   absent from the checkout: exits non-zero naming "not a commit in this checkout".
5. `test_the_release_branch_is_cut_from_the_base_not_mains_tip` — the regression this spec
   exists for. Repo with `origin/main` two commits ahead of the base; `--base <older sha>`:
   the plan contains `git branch 'release/jarvis-X.Y.Z' '<base sha>'` and the local HEAD is
   never compared.
6. `test_a_pinned_release_ignores_the_local_head` — HEAD on an unrelated branch, `--base`
   an ancestor of `origin/main`: exit 0. The live gate-347 case.
7. `test_the_notes_stop_at_the_base` — a commit merged after the base does not appear in
   the release notes, and the commits up to the base do.
8. `test_no_base_keeps_the_old_precondition` — no `--base`, HEAD ahead of `origin/main`:
   still the `HEAD is not origin/main` failure. This is the existing
   `test_refuses_when_main_is_ahead_of_origin` (`tests/test_shipit.py:184`) kept as-is, and
   it is what pins the §2 fork.
9. `test_a_dirty_os_managed_path_still_does_not_block_a_pinned_release` — the
   `OS_MANAGED_PATHS` allowance under `--base`, so §2's "unchanged" is pinned rather than
   asserted.

Tests 1-7 and 9 ride the existing harness unchanged: `_make_repo` (dev clone with a bare
`origin`), `_dry_run` (the real script, `--dry-run`, stub `uv` and `gh` on PATH),
`_commit`, `_tag_like_shipit` — all `tests/test_shipit.py:30-117`. `--dry-run` composes
with `--stage` already (`test_a_staged_release_re_renders_too`, `:386`).

10. `tests/test_issue_lifecycle.py:1019-1026` **must be updated, not added to**: it asserts
    `f"scripts/shipit.sh --stage --wo {releases[0]['id']}" in brief`, which is a prefix of
    the new line and would keep passing while saying nothing. Assert the `--base`
    placeholder and the resolution instruction are both in `Daemon.RELEASE_BRIEF`.
11. `test_a_pinned_release_command_still_gates` (`tests/test_gate_rules.py`) — the §7
    canary through `RuleSet.classify`, and `check_canaries` green with it in the set.

Not covered, deliberately: no test asserts the worker resolves the sha correctly. That is
a judgement made in a prompt, and §4's failure mode is already fenced by machine checks —
a sha that is not an ancestor is refused by §2, and a batch the tag does not carry leaves
the order open by `tags_containing`.
