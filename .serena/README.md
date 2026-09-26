# Why `.serena/project.yml` says what it says

`project.yml` is **tool-regenerated**. The installed Serena rewrites it from its own
template whenever the release's schema is newer than the file, and the rewrite strips
every comment the template did not ship with. That is why these notes live here: Serena
does not touch this README.

Both entries below are knowledge entry `kn-42d07c3f` (`jarvis learn show kn-42d07c3f`).

## `ignored_paths: [".claude/worktrees/**"]`

Worker worktrees are untracked but **not** gitignored, so `ignore_all_files_in_gitignore:
true` does not cover them. Without this entry the symbol index holds one copy of every
symbol per live worktree and `find_symbol` returns duplicate hits for a single definition.

## `initial_prompt` must stay short

It fires on **every** activation in **every** checkout, production included, and is paid
for once per session. Keep it to what a session cannot get anywhere else — the pointer at
the memories — and leave the rest to `CLAUDE.md`.

## When Serena regenerates the file

Land it as its **own declared change**, not folded into unrelated work: it is a ~120-line
diff of vendor comments in which the two lines above are invisible. Check after
regenerating that both survived verbatim.

## Worker worktrees hide this file on purpose

A worker's worktree marks every `worktree.tool_managed_paths` entry — `.serena/project.yml`
by default — `skip-worktree` in its index at `SessionStart`, so Serena's rewrite inside the
worktree never reaches `git status`, `git add -A` or the worker's pull request. **This is
deliberate, not a broken checkout.** Spec:
`docs/superpowers/specs/2026-09-25-serena-config-churn-and-tool-managed-files.md`.

A worker that genuinely must change a tool-managed file should say so rather than work
around it; un-marking is by hand, `git update-index --no-skip-worktree -- <path>`.
