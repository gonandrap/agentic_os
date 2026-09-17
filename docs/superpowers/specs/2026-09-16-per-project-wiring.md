# Per-project wiring of MCP servers, skills and plugins

GitHub issue #164 item 4, final clause. Work order `wo-dfab12bb`; default state ruled by
the user, 2026-09-16.

> Also, I like the idea of having more control over what MCP configs/skills/plugings are
> ship on each work order. Expand /config UI to also let the user configure that per
> project. by default, it will show the configs that are configured at user level. Then
> each project can select which ones will be wired to their work_orders or
> feature_orders. **No claude config is changed on file, only what is wire and injected
> into each work order.**
> — the user

> The per-project selection is OPT-OUT, not opt-in. The OS-level default is POPULATED
> FROM THE USER'S OWN CLAUDE CONFIG: every MCP server, skill and plugin configured at
> user level is wired ON for every project out of the box […] A project then DESELECTS
> the ones it does not want; it never has to select the ones it does.
> — the user, ruling the default state

## 1. The shape, in one paragraph

The user's Claude configuration is **read** to populate a list and **never written**. A
project stores its DEVIATION from that list — four keys under `wiring`, all of whose
defaults mean "wired". At dispatch, `dispatch._write_worker_settings` turns that
deviation into keys in the per-work-order settings file the spawn already passes to
`--settings`. Nothing else changes, and at the shipped default the file is byte-identical
to the one written before this feature existed (`tests/test_wiring.py::
test_the_shipped_default_wires_everything_and_writes_nothing`).

## 2. The levers, and why the page may not offer more than it has

Every lever is a key in that one settings file. There is a lever **per source, not per
item**, and that asymmetry is the most important thing in this document, because it is
what the page is allowed to offer:

| What | Lever in the worker settings file | Granularity |
|---|---|---|
| claude.ai connectors | `disableClaudeAiConnectors: true` | the block, all nine |
| a plugin (its MCP servers, skills, agents) | `enabledPlugins: {"<id>@<mkt>": false}` | per plugin |
| a user or project skill | `skillOverrides: {"<name>": "off"}` | per skill |
| Claude Code's bundled skills | `disableBundledSkills: true` | the block |
| a hand-added MCP server | **none** | — |

The last row is the constraint doing its work. `/mcp disable <server>` persists to
`disabledMcpServers` **in the project entry of `~/.claude.json`** — the user's own
configuration, which this feature may not touch. So a hand-added server is listed with no
control and a sentence saying why, rather than a button that silently does nothing.
`--strict-mcp-config` was considered and rejected for this: it is all-or-nothing (verified:
with no `--mcp-config` beside it, zero servers connect), so using it to unwire one server
would unwire every server.

`skillOverrides` takes four values; the page writes only `off`. The other three describe a
human at a prompt ("hide it from the model but keep `/name`"), and a dispatched worker is
not one. A PLUGIN's skills are not reachable this way at all — Claude Code returns "on"
for a plugin-sourced skill regardless — which is why plugin skills are listed under their
plugin and not beside the user's own.

Every one of these was verified live against Claude Code 2.1.272 by spawning a session
with a settings file and reading `--debug-file` for the MCP connections, or asking the
session to list its own skills. The knowledge entry records the probes; this document
records the conclusions.

## 3. Where a deselection takes effect, and where it does not

`dispatch._write_worker_settings` — the single seam. Its docstring already explains why
that file exists (a worktree has no `.claude/settings.json`, so hooks, permissions and env
must travel with the spawn); this adds one merge to it.

* **Work orders and feature orders alike.** A feature order is a planner, a manager and
  children, and all four kinds resolve their flags through `worker_session.briefing_for`,
  which calls that one function. Parameterised over the kinds in the tests.
* **`next-dispatch`, never hot.** `ops.APPLY_RULES` says so on the page: a running
  worker's session already holds the servers it was launched with.
* **Not the project's own `.claude/settings.json`.** That file is written by `bootstrap`
  for the sessions the USER opens by hand, and narrowing those is not what was asked for.

## 4. Coherence: everywhere Serena is named

`dispatch.py` documents a real failure — a tool listed in `tools:` but absent from
`permissions.allow` had its call blocked — and `SERENA_TOOL_PREFIXES` exists because a
plugin install and a manual `claude mcp add` produce different prefixes for one server. A
control that can deselect Serena must not reproduce either, so unwiring it removes it from
every place the OS names it:

1. the `permissions.allow` rules for its read-only tools (`serena_allow_rules`);
2. the navigation briefing handed to every worker — `worker_brief.navigation_section`
   grows a Serena-less form, and the answer travels to the separate `jarvis brief`
   process as `JARVIS_SERENA`, the way `JARVIS_GATES` already does;
3. the two planning seats a planner's subagents run as: their Serena tool grants are
   stripped and an override note is prepended at install time, because their first
   instruction is otherwise "your first tool call is a Serena call".

The reverse direction is the existing behaviour and is unchanged: while Serena is wired,
all three name it exactly as before.

## 5. Two decisions worth stating

**Ids are not validated against the machine.** A catalog is read by the daemon on a box
whose Claude configuration moves under it; a list that named an uninstalled plugin would
fail the whole catalog and take the fleet down over a deselection that had simply come
true. An id naming nothing is inert, as an allow rule naming an absent tool is.

**Wiring is not a `SAFETY_KEYS` path.** Unwiring only narrows what a worker can reach, and
wiring back only restores what the user's own configuration already says. Neither widens
what a worker is ALLOWED to do, which is what the mandatory `--reason` is for (kn-64f4922c:
the reason exists to justify a change in permission, and a write that moves none has
nothing to put on the row).

## 6. What this does NOT decide

The fleet default. Every key ships wired, and narrowing what every worker can reach is a
capability decision for the user — `docs/superpowers/findings/2026-09-14-which-mcp-server-
moves-the-prefix.md` says so explicitly, and this order was scoped to build the control
rather than to use it. The one row with a measurement behind it (the claude.ai block, at
83.7% — the worst cohort measured, with nothing clearing any other server) carries that
measurement on the page, to its own stated limit, so the decision can be made with the
evidence in front of it.
