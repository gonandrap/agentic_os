# A headless call that starts from nothing

Work order wo-00bd1096. GitHub issue #750. Neo question 670 ruled option B: minimal
context is the DEFAULT of `run_headless_result`, for every caller, production included.
That decision is settled; this spec is how.

## The problem

**Every `claude -p` call the OS makes pays for Claude Code's whole default context, and no
caller wants any of it.**

One transport: `claude_cli.run_headless_result` (src/jarvis/claude_cli.py:1503).
`run_headless` (:1589) is that function keeping only `.text`. Its system-prompt plumbing,
`_system_prompt_arg` (:80), emits `--append-system-prompt` or, past
`SYSTEM_PROMPT_ARGV_LIMIT` (:74, 64 KiB), `--append-system-prompt-file`. APPEND means the
caller's persona is added ON TOP of: the CLI's base system prompt, CLAUDE.md
auto-discovery, the user's plugins, skills, hooks, MCP servers and settings.

Measured on this branch, claude-haiku-4-5-20251001, CLI 2.1.282, identical one-word answer
in both arms:

| call shape | input tokens |
|---|---|
| today (`--append-system-prompt`, `--tools ""`, `--strict-mcp-config`, repo cwd) | 17,161 |
| minimal | 1,033 |

16,128 tokens of context nobody asked for, on every call, forever.

Production consequence, from #750: the stakes-classifier A/B eval spent ~37k input tokens
per call, ~$71 of list price in one day, and 46.6% of all spend on the machine inside one
38-minute window.

Root cause is the append, not the eval. The six production call sites are all prompt-only
judges from a neutral cwd — neo.py:347, panel.py:609, seats.py:210 and :247,
validation.py:1163, digest.py:107, every one `cwd=ensure_home()`, `attribute=False`. None
of them reads `HeadlessResult.session_id`; nothing in `src/` does.

## The fix

Make `run_headless_result` build a call that starts from nothing, in
`src/jarvis/claude_cli.py`, in the isolation block at lines 67-129 — beside
`SYSTEM_PROMPT_ARGV_LIMIT`, `cache_env` and `_run`. There, not at the call sites, for the
reason the existing `--tools ""` → `--strict-mcp-config` coupling already gives at :1553:
what "judge the prompt and only the prompt" costs is not a caller's to remember, and five
of the evals #750 names never touch `claude_cli` at all — they drive the production code
above.

### Two groups of flags, split on ONE question: does the caller want tools?

Principle: **the CLI's own baggage goes for everyone; the USER'S CONFIGURATION survives for
a callee that has tools.** A tooled callee runs under the permissions and servers the
settings sources supply, so stripping them changes what it is ALLOWED to do, not merely
what it reads (Neo's carve-out, question 670).

Applies to every call, `tools` irrelevant:
- `--system-prompt <text>` instead of `--append-system-prompt <text>` — replaces the base
  prompt.
- `--no-session-persistence` — free: no consumer of `session_id` exists in `src/`.

Applies only when `tools == ""`:
- `--mcp-config <tmpfile containing {"mcpServers":{}}>` with the `--strict-mcp-config`
  already sent at :1558.
- `--setting-sources ""` — no user, project or local settings, therefore no hooks and no
  plugins.
- `--disable-slash-commands` — the CLI help calls it "Disable all skills"; skills arrive
  through the same configuration as plugins, so they belong in this group.

`tools=None` and `tools="Read,Bash"` are the tooled case and keep all of the above. That
is deliberate and it has a price: **Neo (neo.py:347) and Neo's panel (panel.py:609,
seats.py via `run_blind`) pass no `tools` at all**, so they keep the user's settings, MCP
and skills. seats.py:269 and tests/test_claude_cli.py:25 both record that as intended
("behaviour must not change"; "Neo relies on the default: a normal session with its tools
intact"). Consequence to state plainly: `test_neo_panel_judgment` and Neo's own answers get
the base-prompt and session saving but not the settings saving. Whether the panel needs
tools at all is a separate question and is NOT decided here.

cwd is untouched. CLAUDE.md discovery follows the working directory, every production
caller is already on `ensure_home()`, and silently relocating a tooled callee would break
the one eval whose subject is the repo (below).

### The system prompt above the ceiling

CLI 2.1.282 has NO `--system-prompt-file`; only `--append-system-prompt-file` exists.
`MAX_ARG_STRLEN` is a hard 131,072 bytes per argv entry and crossing it is an `OSError`
from `execve` that no CLI can report — which is why `_system_prompt_arg` spills at 64 KiB.

**A real caller lives past the line, and it is the normal case, not an edge.** A validation
seat's system prompt carries the packet at `DEFAULT_VALIDATION_DIFF_CHARS = 150000`
(catalog.py:396); test_claude_cli.py:77-83 already exists because of it. So "fail loudly"
would take down validation on every ordinary round, and "keep the append path for oversize
prompts" would leave the full default context on the OS's single most expensive call.

Chosen: **a short stub in `--system-prompt`, the real prompt in
`--append-system-prompt-file`.** The replacement door shuts out the base prompt; the file
door carries the bytes. Stub text is a fixed one-liner, byte-stable, so Neo's cached prefix
survives.

This combination is the one thing in this spec not yet verified against the CLI. Before
writing the code, confirm with one real call that `--system-prompt` and
`--append-system-prompt-file` together deliver stub-then-prompt and do not error. If the
CLI rejects the pair, fall back to `--append-system-prompt-file` alone for oversize prompts
and log at WARNING that the call kept the default context; do not fail the call.

### Where the evals enter

Under decision B the shared entry point is thin, because the transport is already minimal:
`claude_cli.run_headless` / `run_headless_result`. Fourteen of the fifteen files in
`evals/llm/` already go through it. Two cases opt parts back in BY NAME:

1. `evals/llm/test_navigation_judgment.py:183` builds its own argv with
   `subprocess.run`. Its SUBJECT IS THE ENVIRONMENT — it needs Serena MCP and its
   settings. Move it onto `run_headless` passing `tools` (so the settings group is left
   alone), keeping its own repo cwd.
2. `evals/llm/test_knowledge_retrieval.py:575` (and :667) already passes real tools and a
   `permission_mode`: tooled case, unchanged.

Guard test: extend `tests/test_eval_harness.py`, which already AST-parses
`evals/llm/test_jarvis_judgment.py` and whose docstring at :43 records why a substring
check is a trap here. Widen it to sweep EVERY `*.py` under `evals/llm/` and fail when a
file calls `subprocess.run`/`subprocess.Popen`/`os.exec*` with `claude` or
`claude_cli.claude_bin()` as the first element of argv, unless the file is in an explicit
allow-list carrying the reason. AST, not grep: an eval's docstring explaining the flags
would keep a grep green after the flag was deleted. New file under `evals/llm/` therefore
fails on arrival.

### What the tests pin

In `tests/test_claude_cli.py`, beside the existing argv-shape coverage at :24-100 — do not
open a new file:

1. Prompt-only (`tools=""`): `--system-prompt` carries the text; NEITHER
   `--append-system-prompt` nor `--append-system-prompt-file` in argv.
2. Same call: `--setting-sources` present with value `""`; `--mcp-config` present and the
   file it names parses to `{"mcpServers": {}}`; `--strict-mcp-config`;
   `--disable-slash-commands`; `--no-session-persistence`.
3. Tooled (`tools=None`, and `tools="Read,Bash"`): `--setting-sources`, `--mcp-config` and
   `--disable-slash-commands` all ABSENT; `--system-prompt` and
   `--no-session-persistence` still present. Without this the change silently re-permissions
   Neo.
4. Oversize: at `SYSTEM_PROMPT_ARGV_LIMIT + 1` bytes, `--append-system-prompt-file` carries
   the text (assert the marker ARRIVES, as :91 does, not merely that a flag was passed),
   `--system-prompt` carries the stub, bare `--append-system-prompt` absent, temp files
   cleaned up.
5. No model call in any of them: the `fake_claude` fixture, as today.

Two migrations, both mechanical:

- `src/jarvis/testing.py:236-249` already resolves both flag families into
  `system_prompt_seen` — but it loops `("--append-system-prompt", "--system-prompt")` and
  each branch ASSIGNS, so with a stub in `--system-prompt` and the real text in
  `--append-system-prompt-file` the later iteration overwrites and every oversize prompt
  reads back as the stub. Concatenate in CLI order (system prompt, then append) instead.
  Fix this first; the oversize test above is the pin.
- Tests that read the persona by indexing argv for `--append-system-prompt` must read
  `call["system_prompt_seen"]` instead — that is what it is for, and it is flag-agnostic.
  The rule, not a list: any test asserting on a HEADLESS call migrates; the worker-spawn
  argv builder is a different code path and its tests (tests/test_worker_spawn_args.py and
  friends) are untouched. Each file's own assertions say which it is.

## Rejected

- **`--bare` (#750's own proposal).** Never reads OAuth or the keychain. Under the user's
  subscription login it either fails outright or bills a separate API account. The flag
  list in this spec is verified accepted by CLI 2.1.282 under that login, with no API key
  and no keychain problem.
- **An eval-only minimal helper.** Cannot fix five of the evals #750 names — neo,
  neo_panel, validation, plan_review, gate_review — because those spawn nothing
  themselves; they drive neo.py, panel.py, seats.py and validation.py. This is Neo's
  reasoning in question 670 and the reason B won.
- **A new `minimal=True` keyword, default off.** Every one of the six production call sites
  would have to pass it, and the seventh — written next month — would not. The default is
  the fix.
- **Making `cwd` default to `ensure_home()`.** Would move a tooled callee's filesystem root
  under it. No production caller needs it: all six already pass a neutral cwd.
- **`--system-prompt` for prompt-only calls only, keeping append for tooled ones.** The
  base prompt is not a permission. Splitting there would hand Neo the whole 16k for no
  stated benefit.

## Baseline, measured before the change

Unmodified HEAD 18568fc, `JARVIS_EVALS_MODEL=sonnet`. Re-run these after the change; a
minimal-context subject that scores the same is the whole acceptance argument.

- `test_neo_judgment` 9/9
- `test_validation_judgment` 17/17
- `test_plan_review_judgment` 3/4 — `test_clean_plans_are_released_by_neo_alone` fails,
  pre-existing.
- `test_gate_review_judgment` 6/12 and `test_neo_panel_judgment` 0/9 — both from ONE
  obsolete fixture, unrelated: `grep -rn shipit.sh src/jarvis/gates.py` no longer trips a
  gate, because the recogniser now correctly reads a chain of read-only commands as
  read-only, so those evals' `assert action is not None, "bad eval fixture"` fires.

## Not in scope

- That obsolete gate fixture. Pre-existing defect, recorded above, NOT fixed here and no
  fix designed for it.
- Whether Neo and Neo's panel should pass `tools=""`. It is the only remaining source of
  default-context spend in production after this change, and it is a behaviour decision
  Neo explicitly protected.
- The worker dispatch argv. Real workers need the full environment; nothing here touches
  them.
