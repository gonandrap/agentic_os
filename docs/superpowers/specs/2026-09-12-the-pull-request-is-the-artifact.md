# The pull request is the artifact

Issue #200. Supersedes the collection half of
`2026-08-08-validation-panel-design.md` — "The evidence packet" — which said the packet
is the worktree's git diff. It is not, any more.

## §1 What was wrong

A work order can deliver real, durable change without touching a file, and the panel
could not see any of it. `evidence.collect_work_order` built the packet from
`git diff` in the worker's worktree and from nothing else, so a work order whose whole
deliverable was a knowledge-base retraction produced `files == ()` and was escalated,
unjudged, with a sentence that was simply false: "this submission changes no files, so
there is nothing to review."

Live: wo-28405ea1 (production, jarvis-0.9.0) retracted kn-e30648dc — a fleet-wide
instruction every future worker reads — and wrote its replacement. Round 1 escalated
with no seat having opined at all.

The second half of the same defect is that the change was not even RECORDABLE. The
`knowledge` table carried no `wo_id`, and `add_knowledge`/`retract_knowledge` wrote
nothing to the work order's timeline, so no query could answer "what did this work order
change in the knowledge base". Reads were attributed (`knowledge_reads.wo_id`); writes
were not.

## §2 The artifact is the pull request, not the worktree

The user's ruling on #200: when a pull request exists it IS the artifact, judged the way
a human judges one. The diff, the body, the files, the check runs — all of it is already
assembled there, by the submitter, for exactly this purpose. An ad-hoc packet is the
special case, not the norm.

**The judge never writes to GitHub.** Blind review is the property this protects: a seat
that could comment on the pull request could talk to the implementor, and a review whose
two halves can negotiate is not a review. Neo (question 251) chose the structural
guarantee over the policy one:

- The SEATS stay tool-free — `tools=""`, `cwd=$JARVIS_HOME`, exactly as
  `validation.py`'s header has always said. They gained nothing.
- The COLLECTOR fetches the pull request, through `github.py`, which runs read verbs
  only. `github.READ_ONLY_VERBS` names them and `tests/test_github_artifact.py` asserts
  that every `gh` invocation in the module is one of them.

Read-only is therefore a property of the code path, not of an allowlist string a model
is asked to respect. There is no write verb anywhere in the judging path to allow.

## §3 What the collector does now

`evidence.collect_work_order` resolves its diff in this order, and records which one
it used in `packet.source`:

| `pr_url` | `gh` | `source` | diff, files, stat come from |
|---|---|---|---|
| set | answers | `pull_request` | `gh pr diff` + `gh pr view --json` |
| set | fails | `worktree` | the worktree, and `packet.pr_error` says why |
| unset | — | `worktree` | the worktree, as before |

**A failed fetch never silently becomes a worktree packet.** `pr_error` carries the
reason and the seat prompt prints it under a heading that says the artifact the
submitter pointed at could not be read. Presenting the worktree as the pull request
would be the same silent lie `collect_work_order` already refuses to tell when the
worktree is missing.

`packet.pr` carries what a human reviewer reads and a `git diff` cannot show: title,
body, state, draft, base and head refs, additions/deletions, and the check runs with
their conclusions. The declared testing evidence is now checkable against CI rather than
taken on the submitter's word.

Truncation, `files`, `dropped_files` and the file-boundary rule are unchanged and apply
to whichever diff was collected.

## §4 Side effects: what a work order did that no diff can show

`packet.side_effects` is a tuple of records of durable, non-file change, collected by
`ops` (which may read a store) and passed in, exactly as `spec` and `children` are —
`evidence.py` is still a leaf that touches no database.

This release attributes ONE kind: knowledge-base writes.

- `knowledge.wo_id` — new column, migrated through `ADDED_COLUMNS`, `''` on every
  pre-existing row, which reads as "not attributed" and is what those rows were.
- `ops.learn_add` / `ops.learn_retract` wrap the central write and add a
  `knowledge_added` / `knowledge_retracted` event to the work order's timeline. They sit
  in `ops` because the knowledge base is CENTRAL and the timeline is PER-PROJECT: the
  writer needs both stores, and `central_store` is a leaf that may not import `ops` to
  get one. `ops.find_work_order` is how the project is resolved from the id alone.
- The attribution is passed from the CLI, not read from `$JARVIS_WO_ID` inside the
  store — the same shape `record_knowledge_read` already uses for reads.

GitHub state, backlog items, Neo learnings, gate outcomes and configuration changes are
the same class of invisible effect. They are deliberately NOT in this release (issue
#200 step 3, "decide separately"); `side_effects` is shaped as a list of typed records
so adding one is a collector change and not a packet change.

## §5 The two guards

`Daemon._validate_work_order` escalated on `not packet.files`. It now escalates on
`not packet.files and not packet.side_effects`. The guard's intent is untouched — a
reviewer handed nothing will rubber-stamp it — and only its premise is corrected: "no
files changed" was never the same statement as "nothing was delivered".

The repeat-fingerprint guard needed the same correction, and it is the one that is easy
to miss. `fingerprint` hashed `diff_sha` and the normalised `declared` text. Two
consecutive diff-less rounds retracting two DIFFERENT knowledge entries would hash
identically, and round 2 would escalate as "identical to round 1" — issue #200
reappearing one guard further along.

So the formula widens, which is a correction to Neo's ruling on question 133
(kn-c8b9c7da) and was ruled on as one (question 253):

    fingerprint = sha256(diff_sha + "\n" + side_effects_sha + "\n" + normalise(declared))[:16]

**The exclusion list is unchanged, and that is the point.** `head`, `base`, `summary`
and `pr_url` stay out, by name, for the reason they were excluded: a submitter can move
each of them without producing any new evidence. Side effects are not that. They are
evidence of precisely the kind `diff_sha` covers for files, and `side_effects_sha` is
computed at collection time exactly as `diff_sha` is.

The packet still never carries the untruncated diff, in any field, at any time
(kn-c8b9c7da, option A, still rejected).

## §6 The seats

`build_packet_prompt` renders three new sections when they have content: the pull
request the change lives in, the checks GitHub ran on it, and the side effects. The
chair and the four seats are told, in their mandates, that a submission with no diff is
not automatically an empty one — a seat that has only ever seen diffs will otherwise
read an empty one as nothing delivered and reject on that alone, which is the false
escalation moved inside the panel.
