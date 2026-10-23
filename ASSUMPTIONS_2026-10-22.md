# Assumptions — session dispatch layer (2026-10-22)

1. **The mapping lives in the catalog, as `worker.launcher`.** The catalog is already per machine and outside version control, so it keeps the mapping local without a second config file.
2. **Two launcher forms.** `open` is an argv template for session managers such as `claude-jarvis start @jarvis --model {model} --compact-pct {compact_pct} --prompt {prompt} {wo_name}`. `command` is a thin argv-transparent wrapper. The user's stated mapping is the `open` form.
3. **The manager is called once per order.** It opens the session on turn 1. Every later turn is a bare `claude --bg --resume` inside the worktree the manager made, because `claude-shining resume` is interactive and re-running `start` would build a second env.
4. **`{wo_name}` is the work-order id with non-alphanumerics removed** (`wo-61a28e47` becomes `wo61a28e47`). `claude-shining` refuses any other name. A title slug was not used, because ids are unique and stable.
5. **`{compact_pct}` assumes a 1,000,000-token model context.** It is Jarvis's autocompact window as a percentage of that, so 400k becomes 40. A launcher can override it with `context_window`.
6. **Jarvis's worker settings reach a template-opened session through `claude-shining --inject`.** The template carries `--inject {inject_dir}`. The per-order dir holds the worker settings as `settings-fragment.json`, the appended system prompt as `context.md`, and the OS skills and agents. This needs the agentic-env change on `gonzaloraposo-bc/injectSettingsMerge`. Without it, only `permissions.allow` and the skills land. The merge rule: objects merge, lists union (both sides' hooks fire), and the env's own value wins a scalar conflict.
6a. **Not carried in template mode:** `--agent`, `--effort`, `--permission-mode` (the env's autonomy decides) and `--max-budget-usd` (poll enforces the cap). A skill both sides name the same (`caveman`) ends up as the injected copy.
7. **Only launches go through the launcher.** `claude agents`, `stop`, `rm`, `respawn`, Neo and the validators stay on the real CLI.
8. **The mapping is per project, not per work-order kind.**
9. **A missing launcher is a doctor finding, not a catalog error.** One deleted script should not stop every project.
10. **Settling now waits for the session's process to exit.** That costs up to a second per settled background turn. It closes the resume-forks-a-copy race the live run hit.
11. **The live test ran against a throwaway detached worktree of agentic_os,** not the main checkout, whose `.jarvis` queue belongs to the prod daemon. The test used an isolated `JARVIS_HOME`. `claude-jarvis` was restricted to agentic_os mid-session, so the first probe repo was abandoned and removed. All test envs, sessions, branches, plugin records and the probe trust entry were cleaned up.
12. **The production catalog was not edited.** The earlier env-only draft launcher was deleted, because env cannot reach a background session.
13. **Experiments ran the patched `claude-shining` through a `PATH` override.** `~/.local/bin/claude-shining` was never replaced. All experiment envs, sessions, worktrees and local plugin records were removed.
