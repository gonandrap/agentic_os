# One copy of the project's CLAUDE.md (issue #850)

## The problem

A worker runs in `<project.path>/.claude/worktrees/<wo-id>`, which is INSIDE the project.
Claude Code loads `CLAUDE.md` from the cwd and from every directory above it, so every
worker's system prompt carries the project's CLAUDE.md TWICE: the worktree's copy (its own
branch) and the main checkout's copy (which can differ from the branch).

Measured on `/wo/jarvis_os/wo-37f2fc2c/debug`: the `memory_files` row is 11,071 tokens on
turn 1 and 22,454 on turn 2. Turn 2 = worktree CLAUDE.md 45,529 B + main-checkout
CLAUDE.md 43,642 B + the user's own files = 89,816 B. Cost: ~11k duplicated tokens in the
cached prefix of every request for the life of the order, plus two instruction sets that
may disagree — the worker is told the branch's rules and the main checkout's rules at
once, with no marker saying which is which.

Turn 1 measures the smaller figure because of a second defect, not because turn 1 loaded
less: `context._worktree_cwd` (src/jarvis/context.py:96) falls back to the project root, so
the seq-1 dispatch turn was measured as if it ran outside the worktree. Two independent
reasons, both live:

* it requires `path.is_dir()`, and at seq-1 the worktree does not exist yet —
  `worker_session.start` launches with `cwd=project.path` and lets the `--worktree` flag
  create it (src/jarvis/worker_session.py:339-343);
* `dispatch.dispatch_work_order` records the seq-1 row (src/jarvis/dispatch.py:1324) with
  the `wo` dict it read at src/jarvis/dispatch.py:1281 — BEFORE `worker_session.start`
  wrote `worktree=wo_id` — so `wo.get("worktree")` is None there regardless.

**Root cause of the duplication is the worktree's location.** Moving worktrees outside the
project root would remove it, and that is rejected below; this spec suppresses the second
copy at the only place the CLI lets it be suppressed.

### Verified, this order, before writing this

`/tmp/cmdtest` with `parent/CLAUDE.md` containing "Codeword PARENTZ" and
`child/CLAUDE.md` containing "Codeword CHILDZ". `claude -p --model haiku` run from
`child/` reports BOTH PARENTZ and CHILDZ. The same call with `--settings` naming
`{"claudeMdExcludes":["/tmp/cmdtest/CLAUDE.md"]}` reports CHILDZ only. Absolute paths in
that key work.

## The fix

### 1. `hooks.claude_md_excludes(root, cwd) -> list[Path]`

New function beside `hooks.memory_files` (src/jarvis/hooks.py:1522). Empty list when `cwd`
is not strictly under `root` — a turn at the project root has no ancestor copy to drop, and
an empty list is the answer, not an error. Otherwise: every existing `CLAUDE.md` from
`cwd.parent` upward through `root` inclusive.

Both sides resolved exactly as `memory_files` resolves them (src/jarvis/hooks.py:1543-1546
and its comment): worker worktree paths are symlinked on some checkouts and plain on
others, and an unresolved comparison either never matches the stop or never matches the
ancestor test.

`cwd.parent` and not `cwd`: the worktree's own CLAUDE.md is the branch's, the one the
worker must obey.

### 2. `hooks.memory_files` applies the exclusion

One definition feeds both readers, so they cannot disagree:

* the debug view — `context._memory_row` (src/jarvis/context.py:235) calls
  `hooks.memory_files`;
* the prompt-prefix fingerprint — `hooks._memory_digest` (src/jarvis/hooks.py:1557) walks
  the same list.

Consequence, stated rather than discovered later: a work order that spans the release
reports ONE memory drift, and that is TRUE — the prefix really did change, because the
duplicate left it. The fingerprint's comparison scope is one work order across its turns
(kn-0cb81cec), so the drift is confined to orders alive at the boundary.

### 3. `dispatch._write_worker_settings` writes `claudeMdExcludes`

src/jarvis/dispatch.py:58. The key gets absolute path STRINGS for the worker's worktree
`<project.path>/.claude/worktrees/<wo id>`, PREDICTED — no `is_dir` check, because this
file is written before the spawn that creates the worktree (worker_session.py:225 calls it
from `briefing_for`; the `--worktree` flag creates the tree). Merged with anything a
catalog's `settings_overrides` already put in that key, deduped, order preserved.

Why this file and nowhere else: it is the sole chokepoint. `worker_session.py:225` is its
only caller, and every launch goes through `briefing_for` — dispatch turns, message turns,
retries, compaction, and a feature order's planner, manager and children
(tests/test_wiring.py:250-257 pins exactly that parametrisation; the docstring at
tests/test_wiring.py:253 states the claim).

### 4. `context._worktree_cwd` measures the cwd the turn really runs in

Predict the path — skip `is_dir` — for the dispatch turn only, keyed off the `turn` dict
`measure` already receives (src/jarvis/context.py:170): `kind == "dispatch"` and
`seq == 1`. Later turns keep the `is_dir` check, because `worker_session.send` really does
fall back to the project root when the worktree is gone
(src/jarvis/worker_session.py:356). `_worktree_cwd` therefore takes the turn dict, and
`_memory_row` passes it through.

And `dispatch.dispatch_work_order` must hand `context.record` a work-order dict that
carries `worktree` — the stale read at src/jarvis/dispatch.py:1281 is defect (b) above and
the prediction alone does not fix it.

### 5. The row says what it costs and what it dropped

`context._memory_row`'s note states that `memory_files` is in EVERY request's window
(served from cache when warm), unlike `knowledge_index` and `worker_prompt`, which are
listed only on the turn that introduced them (src/jarvis/context.py:196-221). Without that
sentence a reader compares a per-conversation cost against a one-off one.

The row's `detail` names the EXCLUDED paths, so the reader can see the duplicate was
DROPPED rather than never found. Absent and zero are different (module docstring,
src/jarvis/context.py:15).

## Rejected alternatives

* **Move worktrees outside the project root.** Removes the root cause, and breaks the
  permission rules built from `<proj>/.claude/worktrees/<id>`
  (src/jarvis/dispatch.py:89-96), `worker_session.worktree_path`, every
  `.jarvis/worker-settings` path and the user's muscle memory. Out of proportion to ~11k
  tokens.
* **Delete or stub the worktree's CLAUDE.md at creation.** Keeps the main checkout's copy —
  the one that can be a DIFFERENT revision from the branch. Wrong copy survives.
* **Exclude in a `SessionStart` hook.** A hook cannot remove a file the CLI has already
  resolved into the prompt; `claudeMdExcludes` is read from settings at startup.
* **Subtract the duplicate in `context` only.** Makes the measurement honest and the
  prompt unchanged — reports the bug instead of fixing it.

## Not in scope

**Panel seats.** The issue asks for them. Nothing to do: every seat runs with
`cwd=ensure_home()` ($JARVIS_HOME) and `tools=""` — src/jarvis/validation.py:989
(cache priming), :997 (`run_blind`) and :1185 (the chair). No seat's cwd is inside a
project, so no seat ever resolves a project CLAUDE.md and there is nothing to exclude.
Adding the key there would be inert configuration claiming a fix.

## Tests this owes

1. **Nested-worktree exclusion list** — `hooks.claude_md_excludes(root, root/.claude/
   worktrees/wo-x)` returns the root's CLAUDE.md and NOT the worktree's; returns `[]` for
   `cwd == root` and for a cwd outside root; survives a symlinked worktree path (the case
   src/jarvis/hooks.py:1543's comment exists for).
2. **The settings file carries the key** — `dispatch._write_worker_settings(spec, wo)`
   writes `claudeMdExcludes` holding the absolute `<proj>/CLAUDE.md` string WITH NO
   WORKTREE ON DISK, and a `settings_overrides` entry in that key survives the merge
   deduped. Same shape as tests/test_wiring.py:41.
3. **Fingerprint and debug view agree** — `hooks._memory_digest` and
   `context._memory_row`'s `detail["paths"]` list the same files for the same
   `(root, cwd)`, with the duplicate absent from both.
4. **Dispatch-turn cwd prediction** — `_worktree_cwd` returns the worktree path for
   `{"kind": "dispatch", "seq": 1}` with no directory present, and the project root for a
   message turn with no directory present; plus a `dispatch_work_order` test that the
   recorded seq-1 payload's `detail["cwd"]` is the worktree and not the project root.
