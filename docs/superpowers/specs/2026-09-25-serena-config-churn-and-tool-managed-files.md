# Tool-managed files must not churn into a worker's commit

Work order wo-842978a6 · GitHub issue #743 · decided by Neo, question 666 (do not re-open).
Two parts, one pull request: **B** is the mechanism, **A** is the one-off that stops the
current file drifting.

## The problem

`.serena/project.yml` is a TRACKED file that an installed tool REWRITES. Serena
regenerates it from its own template on activation whenever the installed version's schema
is newer than the file — dropping every comment the file did not come with and adding the
release's new keys.

Evidence, in this worktree, right now:

1. The working tree carries an uncommitted regeneration of
   `/home/gonzalo/workspace/agentic_os/.serena/project.yml`. The file is now 197 lines of
   the installed release's template. It contains keys the committed version predates
   (`activation_command_timeout: 180.0`, `language_backend`, `ls_workspace_folders`,
   `ls_additional_workspace_folders`, `symbol_info_budget`, `fixed_tools`,
   `read_only_memory_patterns`, `ignored_memory_patterns`, `included_apis`,
   `excluded_apis`, `agent_interface`), and it has NO Jarvis comment left anywhere: the
   only two non-template lines are the values `ignored_paths: [".claude/worktrees/**"]`
   (line 76) and the `initial_prompt` block (lines 115-124), both now bare.
   *(This seat has no Bash, so `git diff` was not run here — the shape above is read off
   the working-tree file. The implementer must paste the real `git diff -- .serena/project.yml`
   into the PR body.)*
2. Every worker worktree is a checkout of that tracked file, and every worker activates
   Serena in the first seconds of its first turn. So the rewrite happens **inside the
   worktree**, against a file `git status` reports as modified, and `git add -A` /
   `git commit -a` stage it. The churn lands in the worker's commit and in its pull
   request, in an order that never touched Serena's configuration.
3. It is unreviewable churn of exactly the wrong kind: a ~120-line diff of vendor comments
   in which the two lines that carry a decision are invisible, in a PR about something
   else.
4. The two stripped comments were load-bearing (knowledge entry `kn-42d07c3f`):
   - `.claude/worktrees/**` in `ignored_paths`: worktrees are untracked but **not**
     gitignored, and `ignore_all_files_in_gitignore: true` therefore does not exclude them.
     Without the entry the symbol index holds one copy of every symbol per live worktree
     and `find_symbol` returns duplicate hits.
   - `initial_prompt` must stay short: it fires on every activation in every checkout,
     production included, and is paid for once per session.
   Losing the reasons is how the values get "cleaned up" by the next reader.

Root cause: **the repository tracks a file whose content is owned by a tool, and nothing
tells git that the worker's copy is not the worker's to change.** Part B fixes that class.
Part A fixes the single instance now in flight — the drift between the installed tool's
output and the committed file, and the comments that drift destroyed.

## The fix

### Part B — mark tool-managed files `skip-worktree` in a worker worktree's index

`git update-index --skip-worktree -- <path>` tells git "the worktree copy of this path is
not mine to report": `git status`, `git add -A` and `git commit -a` all stop seeing it. The
tool keeps rewriting the file, the worker keeps reading it, and the churn can no longer be
staged.

#### Where it lives, and why there

**Hook point: the `SessionStart` branch of `handle_hook`, `src/jarvis/hooks.py:1471`** —
specifically inside `if event == "SessionStart":` (hooks.py:1522), after the
`dispatching`->`running` correction and after the `note_prefix(...)` call, immediately
before the `return {... "additionalContext": concision.house_style()}`.

- After the status correction and the fingerprint: `note_prefix` documents that it wants
  "exactly this moment: once per turn, before the turn's first API call", and the git calls
  added here cost tens of milliseconds. Nothing already in the branch may be delayed by
  them.
- Before the return: the return value stays byte-identical to what it is today. Marking is
  a side effect, never the hook's answer.
- Below the `PreToolUse` / `PostToolUse` / `PreCompact` / `SubagentStart` early returns
  (hooks.py:1476-1489), so `root`, `store` and `wo` are already resolved and a non-worker
  session has already returned `None`.
- `SessionStart` fires inside the worktree before Serena's MCP activation, so the marking
  is in place before the first rewrite.

**Why not at dispatch.** The OS has no code running inside the worktree before the worker
does, because it does not create the worktree — Claude Code's own `--worktree` flag does,
at spawn:

- `worker_session.start` (`src/jarvis/worker_session.py:327-335`) launches turn 1 with
  `cwd=project.path` and `worktree=wo_id`.
- `claude_cli.turn_args` (`src/jarvis/claude_cli.py:733-738`) turns that into
  `--worktree <wo-id>`.
- "`--worktree`: the flag creates the worktree" — `worker_session.py:1154`. And
  `worker_session.worktree_path` (`worker_session.py:100-105`) returns `None` until the
  directory `is_dir()`, i.e. until after the CLI has run.

So the earliest OS code that can run `git -C <worktree>` is a hook, and `SessionStart` is
the first hook of the first turn. A dispatch-time implementation would have to create the
worktree itself, which is a different and much larger change.

#### What identifies a worker worktree

Both conditions, in this order:

1. `"/.claude/worktrees/" in str(cwd)` — the same cheap text test the file already uses at
   `hooks.py:441` and `hooks.py:876`.
2. `cwd.resolve() == (root / ".claude" / "worktrees" / wo["worktree"]).resolve()`, where
   `wo["worktree"]` is the column `worker_session.start` sets (`worktree=wo_id`) and
   `worker_session.worktree_path` builds its path from
   (`project.path / ".claude" / "worktrees" / wt`). Resolve **both** sides: a worker's cwd
   is a symlinked path on some checkouts and a plain one on others
   (`hooks.py:1086-1089`), and `find_project_root` (`hooks.py:1388-1399`) resolves for the
   same reason.

If either fails — a `wo["worktree"]` that is empty, a cwd that is the shared checkout, a
cwd under some other work order's worktree — do nothing. A session the user opened
themselves never reaches here at all: `handle_hook` has already returned `None` for a cwd
with no `.jarvis/` and for a session with no work order (hooks.py:1493-1507).

**On the shared checkout: nothing happens, by design.** The dev checkout's own
`.serena/project.yml` churn is the user's to see and commit; hiding it from `git status`
there would hide a real schema upgrade from the only person who can land it. That case is
what Part A resolves.

#### The list is a setting, not a constant

Key: **`worktree.tool_managed_paths`** — a list of repo-relative path strings, fleet-wide
default `[".serena/project.yml"]`, overridable per project.

The key space is not a table anywhere: `config_version.resolve`
(`src/jarvis/config_version.py:130-143`) flattens the parsed catalog dataclasses
reflectively, so a new field appears in `jarvis config show` / `get` / `set` with no edit
to the config machinery. Follow `WiringConfig` exactly — it is the nearest precedent (a
list-valued, OS-level-with-project-override block):

1. `catalog.DEFAULT_TOOL_MANAGED_PATHS = (".serena/project.yml",)` beside the other
   `DEFAULT_*` constants in `src/jarvis/catalog.py`.
2. `@dataclass class WorktreeConfig` with a single field
   `tool_managed_paths: tuple[str, ...] = DEFAULT_TOOL_MANAGED_PATHS`. Docstring states
   that the list REPLACES rather than merges on a project override — `WiringConfig`'s rule
   (`catalog.py:812-838`, `kn-6ca2bcd9`), inheritance is field-level.
3. `_parse_worktree(raw, base=None, where="os.worktree") -> WorktreeConfig`, modelled on
   `_parse_wiring` (`catalog.py:1495-1523`) including its `_names` guard: a bare string is
   refused (`must be a list of paths`), entries are `str()`-coerced. Refuse an absolute
   path and any entry containing `..` — this value is handed to `git -C <worktree>`, and a
   path escaping the worktree is a configuration error worth failing the catalog over.
4. Field `worktree: WorktreeConfig = field(default_factory=WorktreeConfig)` on **both**
   `OsConfig` (beside `wiring`, `catalog.py:1123`) and `ProjectSpec` (beside `wiring`,
   `catalog.py:1014`).
5. Wire it in `parse_catalog`: `worktree=_parse_worktree(os_raw.get("worktree", {}))`
   beside `catalog.py:1759`, and
   `worktree_cfg = _parse_worktree(p.get("worktree", {}), base=os_cfg.worktree, where=f"projects[{i}] ({name}).worktree")`
   beside `catalog.py:1843`, passed into the `ProjectSpec(...)` construction at
   `catalog.py:1846`.

That is the whole registration. `ops.set_config` (`src/jarvis/ops.py:7580`) and
`ops._key_path` (`ops.py:7410`) then accept
`jarvis config set <project> worktree.tool_managed_paths '[".serena/project.yml", "…"]'`
with no further change, and `ops.parse_config_value` (`ops.py:7395`) already parses the
JSON list. Adding the next tool-managed file is a config write, not a release.

#### How the hook reads it

Via the worker's environment, **not** by importing `catalog`. The hook must not parse the
catalog: `hooks.py:995-998` records that importing `jarvis.catalog` costs ~60ms against a
155ms hook process, a 39% tax, and the house answer for a value fixed at spawn is the
worker settings file's `env` block.

- `hooks.TOOL_MANAGED_PATHS_ENV = "JARVIS_TOOL_MANAGED_PATHS"`, a JSON-encoded list.
  Defined in the READER, like `concision.STANDING_PROMPT_ENV` (`concision.py:34`).
- Written by `dispatch._write_worker_settings` in the `env.update({...})` block
  (`src/jarvis/dispatch.py:122-179`), from `project.worktree.tool_managed_paths`, beside
  `JARVIS_GATES` and `JARVIS_SUMMARY_MAX_WORDS` and for the reason stated there. The
  project spec is already resolved against the OS config, so the env value is the answer
  for this project and the hook consults nothing.
- `dispatch` importing `hooks` for the constant is a downward edge: `hooks` imports only
  `concision` and `project_store` at module level (`hooks.py:21-22`). Confirm no cycle
  before committing to it; if one appears, move the constant to `concision` rather than
  spelling the string twice.
- Absent or unparseable env: the mechanism is off for that worker, silently. An old work
  order dispatched before this lands has no such env and must keep working.

#### Behaviour

For each configured path, in the worktree, bounded to the first 32 entries so the hook's
cost cannot grow without limit:

1. `git -C <worktree> ls-files -- <path>` — empty output means git does not track it:
   skip. (`update-index` on an untracked path exits non-zero and is otherwise harmless,
   but the tracked test is what makes the skip a recorded fact rather than a swallowed
   error.)
2. The path must also exist on disk. `skip-worktree` on a path absent from the worktree
   makes git present the index version as the truth, which is a confusing state to create
   for a file the tool has not yet written: skip.
3. `git -C <worktree> update-index --skip-worktree -- <path>`.

Idempotent: marking an already-marked path is a no-op in git, and every turn's
`SessionStart` re-runs the same three steps. **Never fatal**: a new module-local helper in
`hooks.py` (lazy `import subprocess` inside it, since `hooks.py` does not import it today)
returns `None` on `OSError` or non-zero exit; the whole block is additionally wrapped
`try/except Exception: pass` exactly as `note_prefix` is (`hooks.py:1533-1536`). Reuse
`landing._git` is REFUSED: that helper's contract is "one read-only git command"
(`landing.py:427-440`) and this writes the index.

#### What is recorded

One `wo_events` row, `kind="tool_managed_paths"`, written **only when the call actually
changed something or something failed** — i.e. when at least one path was newly marked, or
a `git` invocation failed. Payload: `{"marked": [...], "skipped": {"<path>": "untracked" |
"absent"}, "failed": {"<path>": "<exit status>"}}`. Add the kind to
`timeline.DEBUG_KINDS` (`src/jarvis/timeline.py:32`): it is circuitry, like
`hook_ignored`, and the user's story does not contain it.

Against the house rule that a guard returning early must still record why: every *distinct*
reason is recorded once, which is what the rule protects. `untracked` and `absent` land in
the first event's payload; a failure always lands. The one case that records nothing is
"every configured path is already marked", which is not a new reason — it is the state the
first event already reported, re-observed. `SessionStart` fires once per TURN, so recording
it each time would put a row on the timeline per turn and make the record a hook log; that
is precisely what `timeline.py:19-21` says `wo_events` must not become.

#### Rejected

- **Gitignore `.serena/project.yml`.** It must stay in the repo: the code map is committed
  on purpose so it ships in release tags and works in production (CLAUDE.md), and
  `ignored_paths` is what stops duplicate symbol hits for everyone who clones.
- **`--assume-unchanged`.** A performance hint git is free to ignore and that commands
  reset; `--skip-worktree` is the flag that means "I will not change this file" and
  survives more operations.
- **A `PreToolUse` deny on writing `.serena/project.yml`.** Serena's MCP server rewrites
  the file itself; no tool call the hook can see is involved.
- **A pre-commit hook in the worktree.** The diff is already staged by then, `-n` skips it,
  and it would have to be installed into a worktree the OS does not create — the same
  problem as dispatch-time marking, with a worse failure mode.

### Part A — land the regeneration, move the comments to `.serena/README.md`

One-off, small, same PR.

1. Commit the working tree's regenerated `.serena/project.yml` as its own declared change,
   with `git diff` quoted in the PR body. The two values must survive verbatim:
   `ignored_paths: [".claude/worktrees/**"]` and the `initial_prompt` block as it stands
   (lines 75-76 and 113-124 of the current file).
2. Create `.serena/README.md` — Serena does not rewrite it — carrying:
   - Why `.claude/worktrees/**` is in `ignored_paths`: worktrees are untracked but **not**
     gitignored, so `ignore_all_files_in_gitignore: true` does not cover them; without the
     entry the symbol index holds one copy of every symbol per live worktree and
     `find_symbol` returns duplicate hits.
   - Why `initial_prompt` must stay short: it fires on every activation in every checkout,
     production included, and is paid for once per session.
   - That `project.yml` is TOOL-REGENERATED: the installed Serena rewrites it and strips
     every comment, which is why these notes live here.
   - That a regeneration should land as its own declared change, not folded into unrelated
     work.
   - That worker worktrees mark it `skip-worktree` (Part B, `worktree.tool_managed_paths`),
     so a worker will not see it as modified — and that this is deliberate, not a broken
     checkout.
   Both entries cite `kn-42d07c3f`.

## Tests (TDD — each must fail first)

New file `tests/test_tool_managed_paths.py`. Drive `hooks.handle_hook` with a
`SessionStart` payload the way `tests/test_prefix_drift_hook.py:46` does, over a real git
project from `testing.make_git_project`:

1. **Marks each configured path** — cwd is the work order's worktree, the path is tracked
   and present: after the hook, `git ls-files -v -- <path>` reports the `S` flag for every
   configured path.
2. **Does nothing in a non-worktree cwd** — same work order, cwd the project root: no path
   is marked, and no `tool_managed_paths` event exists.
3. **No-op and does not raise for an untracked or absent path** — a configured path git
   does not track, and a tracked path deleted from the worktree: `handle_hook` returns its
   normal `SessionStart` result including the `additionalContext` injection, and the event
   payload names the path under `skipped`.
4. **A project-level override is honoured** — a catalog with
   `projects[x].worktree.tool_managed_paths` set to a different list: the env written by
   `dispatch._write_worker_settings` carries that list, and the hook marks those paths and
   not the fleet default's.
5. **The fleet default contains `.serena/project.yml`** — assert on
   `catalog.parse_catalog({})`-level defaults (`OsConfig().worktree.tool_managed_paths`)
   and that `config_version.resolve` exposes the path
   `os.worktree.tool_managed_paths`, so `jarvis config set` can reach it.

Plus one catalog test beside the existing `_parse_wiring` coverage: a non-list value, an
absolute path and a `..` entry are each refused with a `CatalogError`.

## Not covered

- The comments in any other tool-owned tracked file. Only `.serena/project.yml` is on the
  default list; the next one is a `jarvis config set`.
- Cleaning `.serena/project.yml` churn out of branches that already carry it.
- The shared checkout, which Part B deliberately leaves alone.
- Un-marking: nothing removes `skip-worktree`, because the worktree is disposable. A
  worker that genuinely needs to change a tool-managed file must say so and the user
  un-marks it by hand (`git update-index --no-skip-worktree`).
