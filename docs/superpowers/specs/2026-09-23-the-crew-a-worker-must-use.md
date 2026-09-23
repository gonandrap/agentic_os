# The crew a worker must use

A work-order lead stops being a lone session. Two seats it MUST delegate to, one transport
it declares rather than assumes, and three PreToolUse refusals that make the briefing's
oldest unenforced rules actually hold.

## 1. The problem

**A rule that lives only in the briefing is not a rule.**

`worker_brief.core_contract` (src/jarvis/worker_brief.py:193) has forbidden backgrounding in
prose since TEMPLATE_VERSION v10:

> **A turn is one-shot and NOTHING wakes you when a background job ends.** … ending the turn
> kills whatever you left running.

wo-d81fcc15 backgrounded anyway, on two consecutive turns, and the second one cost 62 hours
of wall clock (issue #575). wo-2df8828c did the same before it (tests/test_one_shot_turn.py
docstring). Knowledge entry kn-6dcaf055 states the general form: *a rule the OS only asks for
in the briefing is not enforced*.

The mechanism behind the loss is in the transport. Every work-order turn is
`claude -p --output-format json` — `claude_cli.turn_args` (src/jarvis/claude_cli.py:696),
spawned by `claude_cli.spawn_turn` (src/jarvis/claude_cli.py:761). One process, one shot: the
turn's exit reaps its children, and the background-task notification the model remembers from
interactive Claude Code never arrives. `claude_cli.spawn_background` (src/jarvis/claude_cli.py:281)
is the native `claude --bg` supervisor-owned transport where those notifications DO arrive — it
has NO production caller, only tests. So headless is today's only transport, and it is
assumed, not stated, everywhere that matters.

**Why the obvious fix is the wrong one.**

Obvious fix 1 — reword the contract again. Already tried at v10, already failed twice.
kn-6dcaf055 is the finding, not a hypothesis.

Obvious fix 2 — detect it afterwards. That is wo-dd8668fa (PR #607, unmerged): `worker_session._reap`
(src/jarvis/worker_session.py:647) spots the orphaned job at turn reap, the false "I'll be
re-invoked" message is voided at render time, and the worker is re-sent. It works, and it costs
a WHOLE EXTRA TURN every time it fires — a full prompt rewrite at the cache-write rate, plus
the wall clock of a dispatch. It also cannot see what it cannot reap: a job started through
the shell, or by a subagent's own shell.

Obvious fix 3 — hardcode "turns are one-shot" into the hook. That bakes today's transport into
a rule, so the day `spawn_background` acquires a production caller every one of these refusals
becomes a lie the OS tells its own workers.

Same shape one level up: the lead does spec work and code work itself, in prose, with no seat
boundary, so "write the problem before the fix" and "TDD first" are requests too. A feature
order's planner already has a team (`dispatch._planner_prompt`, "# Your team"); an ordinary
work order has nobody.

## 2. The fix

**Declare the transport; enforce the consequences at the tool call; give the lead a crew it
must delegate to.**

Mechanism: PreToolUse. `hooks.preflight_decision` (src/jarvis/hooks.py:651) already resolves
gates, PR-title and PR-body rules and the finish-summary cap before any auto-allow, using the
existing `_deny(reason)` / `_allow(reason)` helpers. New decision functions join that chain.

Why there and not elsewhere:

- It fires BEFORE the call, so a refusal costs zero turns. `_reap` detection costs one.
- It fires for SUBAGENT tool calls too, under the parent session, carrying `agent_type` — and
  it OMITS that key for the lead's own calls (src/jarvis/hooks.py:514: *absence is the
  discriminator, not a sentinel value*). One hook therefore governs the lead and every seat,
  and the seat is nameable in the record.
- A subagent inherits the parent's `--settings`, so permissions AND hooks govern it
  (kn-07b64deb). It does NOT inherit `--append-system-prompt`, `--agent`, or SessionStart
  context — which is precisely why prose cannot govern a seat and a hook can.

What this does NOT replace: wo-dd8668fa. The hook prevents the CALL; the detector stays as the
backstop for the routes a hook cannot see (a job started inside a script, a shell spawned by a
subagent's own shell). Prevention plus backstop, not either.

## 3. The transport is declared, not assumed

**What it does.** Names the transport in the worker's environment so every rule below can ask
instead of assume.

**Where.** New constant in `src/jarvis/claude_cli.py`:

```python
TURN_TRANSPORT_ENV = "JARVIS_TURN_TRANSPORT"
TRANSPORT_HEADLESS = "headless"      # spawn_turn: `claude -p`, one-shot
TRANSPORT_BACKGROUND = "background"  # spawn_background: `claude --bg`, supervisor-owned
```

`dispatch._write_worker_settings` (src/jarvis/dispatch.py:58) writes
`JARVIS_TURN_TRANSPORT` into the settings `env` block, taking the value from that constant and
never spelling the string again — the same argument the file already makes for
`claude_cli.PROMPT_CACHE_5M_ENV` (src/jarvis/claude_cli.py:42): the launcher and the settings
file must not be able to disagree. `worker_session.briefing_for` (src/jarvis/worker_session.py:151)
calls `_write_worker_settings` on EVERY turn, so the value is per-turn and can never be stale.

Env, not a catalog read: `JARVIS_GATES`, `JARVIS_SERENA` and `JARVIS_SUMMARY_MAX_WORDS` all
carry the same note — the hook runs on every Bash call and a catalog parse is a ~39% tax on a
~155ms hook process.

Semantics: every rule below that depends on one-shot-ness applies ONLY when the value is
`headless`. Absent key = not a Jarvis worker = the hook no-ops, which is how every other hook
already behaves (`JARVIS_WO_ID` is the same test in `hooks._subagent_start`).

**How it is proved.** `tests/test_turn_transport.py`:
`test_worker_settings_declare_headless_transport`,
`test_transport_value_comes_from_claude_cli_constant`,
`test_background_spawn_declares_background_transport`.

## 4. Backgrounding is refused at the tool call

**What it does.** Denies the call that starts a job the turn cannot outlive.

**Where.** New `hooks.background_task_decision(payload, env)`, called from
`preflight_decision` in the Bash chain, before the `is_jarvis_command_chain` auto-allow (same
ordering argument the docstring already makes for the PR checks and the summary cap: an
auto-allow below it would make the rule unreachable).

Denies when `env[JARVIS_TURN_TRANSPORT] == "headless"` AND either:

- (a) tool is `Bash` and `tool_input.run_in_background` is true; or
- (b) tool is `Bash` and the command backgrounds through the shell: a trailing `&`, `nohup`,
  `setsid`, `disown`.

Not matched, and this is the whole difficulty of (b): `&&`, `&>`, `&>>`, `2>&1`, an `&` inside
a quoted string or a comment. The matcher works on the command with quoted spans masked, and
`&` counts only as a trailing statement terminator.

Deny text states the correction and the reason in one line: re-run in the foreground, because
this turn is one `claude -p` and ending it kills the job — nothing will wake you. The worker
fixes it inside the same turn, so the correction costs zero turns rather than the one
wo-dd8668fa's re-send costs.

Subagent calls are covered for free: PreToolUse fires on them under the parent session.

**How it is proved.** `tests/test_background_refusal.py`:
`test_run_in_background_denied_on_headless`,
`test_trailing_ampersand_denied`,
`test_nohup_setsid_disown_denied`,
`test_double_ampersand_and_redirects_allowed`,
`test_ampersand_inside_quotes_allowed`,
`test_allowed_when_transport_is_background`,
`test_no_op_without_transport_env`,
`test_subagent_call_denied_too`.

## 5. The crew: two agent types an ordinary worker must use

**What it does.** Ships two subagent definitions to every ordinary worker, so the lead has
somewhere to delegate that the CLI, not prose, bounds.

**Where.** New assets root `src/jarvis/assets/worker-agents/`, installed by
`bootstrap.install_agent_assets` (src/jarvis/bootstrap.py:108) when `kind == "worker"`, into
`project_state_dir(project_path) / "agent-crew"`, and handed over `--add-dir` (which loads
subagent definitions from `<dir>/.claude/agents/`).

A THIRD root, not a share of the planner's `agent-seats` or the shared `agent-skills`. The
existing docstring gives the reason: each dispatch owns its whole destination tree and rebuilds
it wholesale, so a shared root means one population's rebuild lands while the other
population's concurrently-dispatched turn is reading it. Separate roots keep worker, planner
and skills dispatches independent.

`serena=False` strips the Serena grants from these definitions through the existing
`_strip_serena` path, exactly as the planner seats already do.

### 5.1 `jarvis-spec-writer`

Tools: `Read, Grep, Glob, Write` plus the Serena read tools — tool list copied verbatim from
`src/jarvis/assets/agents/jarvis-architect.md` so the two seats cannot drift. No Edit, no Bash.

Its contract IS section 1 and 2 of this document: a spec states WHAT IS BROKEN with evidence
(ids, `file:line`, measured facts), then states HOW it is fixed — the mechanism, where it
lives, and why there. It must name the ROOT CAUSE it addresses rather than the symptom, and
say plainly when it is fixing a symptom on purpose.

### 5.2 `jarvis-implementer`

The coding seat. Tools: `Read, Edit, Write, Bash, Glob, Grep` plus Serena.

- MUST use the superpowers plugin, TDD first: skill `superpowers:test-driven-development`. The
  skill LISTING is inherited by a subagent (kn-07b64deb), so naming the skill is enough.
- MUST never background anything. The definition says why (the turn dies with the job); §4
  enforces it, so the sentence is orientation, not the control.
- MUST NOT run `jarvis wo finish`, `gh pr create`, or any gated command. Those belong to the
  lead. A gate fired from a seat is still filed against the work order, with the seat named
  (src/jarvis/hooks.py:514) — so a seat that tries it produces a record saying a seat did it,
  which is noise on the lead's work order, not a shortcut.

Withholding tools is a wall only for the tool surface. `jarvis-implementer` has Bash, so the
CLI does not stop it running anything; `dispatch._planner_prompt` already concedes the same
about its own seats (withholding Write while granting Bash is not a prohibition — a heredoc
writes a file just as well).

**How it is proved.** `tests/test_worker_crew.py`:
`test_worker_kind_installs_crew_root`,
`test_crew_root_is_separate_from_planner_seats`,
`test_planner_kind_does_not_get_crew`,
`test_briefing_add_dirs_include_crew_for_worker`,
`test_crew_definitions_parse_with_expected_tools`,
`test_spec_writer_has_no_bash_or_edit`,
`test_serena_stripped_when_unwired`.

## 6. The lead delegates

**What it does.** Tells the lead the division of labour and makes it non-optional.

**Where.** `worker_brief.core_contract` (src/jarvis/worker_brief.py:193) gains a "Your crew"
block, emitted for `kind == "worker"` ONLY (the planner has its own team prose in
`dispatch._planner_prompt` and must not be given a second, contradictory one). Bumps
`TEMPLATE_VERSION`.

Content:

- Spec writing goes to `jarvis-spec-writer`. Code and tests go to `jarvis-implementer`.
- What stays with the LEAD: git, the PR, every `jarvis …` command, the work-order record
  (messages, assumptions, the finish summary), and REVIEW of what each seat produced. A seat's
  output is a draft until the lead has read it.
- Delegating is expected, not politeness — and §7 makes the lead's own editing refused, so the
  block states the mechanism rather than implying goodwill.

`evals/llm/test_worker_judgment.py` and `tests/test_worker_brief.py` pin core phrases; the new
block is added to the pinned set.

**How it is proved.** `tests/test_worker_brief.py`:
`test_worker_core_names_both_crew_seats`,
`test_planner_core_omits_crew_block`,
`test_template_version_bumped_for_crew`.

## 7. Prose is not a control — the lead's own edits are refused

**What it does.** Makes the delegation in §6 real by denying the lead the file-editing tools.

**Where.** New `hooks.crew_edit_decision(payload, env)`, called from `preflight_decision` in
the `Edit`/`Write`/`NotebookEdit` branch, after `under_review_decision` (which must keep
winning: a narrowed session is a stricter state) and before the worktree auto-allow.

Denies when ALL hold:

1. the payload carries NO `agent_type` — the LEAD, per src/jarvis/hooks.py:514;
2. the session is a Jarvis worker of `kind == "worker"` (`JARVIS_WO_ID` set, transport
   declared, `JARVIS_REQUIRE_CREW` on);
3. the target path is INSIDE the work order's worktree.

Exempt: anything under `.jarvis/` (generated state the lead owns), and any path outside the
worktree (already refused elsewhere, and not this rule's business).

Deny text: name the seat to delegate to — `jarvis-spec-writer` for a spec under `specs/`,
`jarvis-implementer` otherwise.

**The knob.** Catalog `worker.require_crew` (bool, default `true`) on `WorkerDefaults`
(src/jarvis/catalog.py:357) with its parse in the project block (src/jarvis/catalog.py:~1795),
travelling as env `JARVIS_REQUIRE_CREW` written by `_write_worker_settings`. A project turns it
off with `jarvis config set <project> worker.require_crew false` and does not wait for a
release.

**The hole, stated plainly.** Bash is NOT denied. A heredoc, `sed -i`, `tee` or `python -c`
writes a file anyway. This is a WALL for the tool surface and a SPEED BUMP for the shell —
exactly the concession `dispatch._planner_prompt` already makes about the planner seats.
Closing it would mean denying the lead the shell, and the lead needs the shell for git, pytest
and every `jarvis …` command. A lead that routes around this rule through `sed -i` has decided
to, and that is a different failure from forgetting.

**How it is proved.** `tests/test_crew_enforcement.py`:
`test_lead_write_inside_worktree_denied`,
`test_lead_edit_inside_worktree_denied`,
`test_seat_write_allowed`,
`test_dot_jarvis_path_exempt`,
`test_path_outside_worktree_not_this_rule`,
`test_require_crew_false_disables`,
`test_under_review_narrowing_still_wins`,
`test_planner_kind_unaffected`.
Catalog: `tests/test_catalog.py::test_worker_require_crew_defaults_true`,
`tests/test_catalog.py::test_worker_require_crew_parsed`.

## 8. A spec missing EITHER its problem or its fix is refused at the write

**What it does.** Refuses to let a spec file exist without the two sections this document is
built on.

**Where.** New `hooks.spec_shape_decision(payload, env)`, called from `preflight_decision` in
the same Edit/Write branch, on `Write` only.

Denies when the tool is `Write`, `tool_input.file_path` has a `specs/` path component and ends
`.md`, and `tool_input.content` is MISSING EITHER a markdown heading matching a problem
(`problem`, `what is broken`) OR a heading matching a fix (`fix`, `solution`). Either one
missing is a refusal — a document with a problem and no fix is the exact defect the user
raised on wo-dd8668fa, so requiring both to be absent would let it through. Matching is
case-insensitive and anchored to markdown headings (`^#{1,6}\s`), never to body prose — a spec
that merely says "the problem" in a sentence has not got a section.

`Edit` is NOT checked: an Edit payload carries a fragment, not the document, so the same test
applied there would refuse every legitimate incremental edit to a conforming spec.

A PLANNER is not checked either (`JARVIS_WO_KIND == "worker"`). A feature spec is a different
artifact — sections a child work order each implements, plus the `Agent profile` appendix — and
`plans` already validates it on those terms. Feature orders are a different shape (user's
ruling on wo-f4ad14b8); widening this rule to them would block a planner mid-turn on a
requirement its own briefing never states.

**Why at Write and not at `jarvis wo finish`.** At finish the spec is already committed and
already in the pull request, so the correction costs a validation round and a re-delivery. At
the Write it costs the seat one retry inside the turn it is already in.

**How it is proved.** `tests/test_spec_shape.py`:
`test_spec_write_without_problem_heading_denied`,
`test_spec_write_without_fix_heading_denied`,
`test_conforming_spec_allowed`,
`test_heading_match_is_case_insensitive`,
`test_body_mention_is_not_a_heading`,
`test_edit_not_checked`,
`test_non_spec_markdown_allowed`,
`test_no_op_for_non_worker_session`,
`test_planner_design_doc_is_not_checked`.

## Rejected alternatives

- **Reword the briefing again.** Tried at TEMPLATE_VERSION v10, failed on wo-2df8828c and
  again on both turns of wo-d81fcc15. kn-6dcaf055 already names the class.
- **Check it in `Daemon.settle_work_order` instead of the hook.** The reconciler re-derives
  every tick, so a finding raised there comes back after the user acks it — kn-089de524. It is
  also strictly later: the job has already run and the turn has already ended.
- **Make the seats advisory.** That is §6 without §7, which is prose, which is the problem
  section of this document.
- **Deny Bash to the lead.** Removes git, pytest and every `jarvis …` command — the lead's
  entire job. The hole in §7 is cheaper than the hole this opens.
- **Detect-and-re-send only (wo-dd8668fa alone).** Kept as the backstop, rejected as the
  primary: it costs a full extra turn per occurrence and cannot see a job started by a shell it
  does not reap.

## Test plan

| File | Tests |
|---|---|
| `tests/test_turn_transport.py` | `test_worker_settings_declare_headless_transport`, `test_transport_value_comes_from_claude_cli_constant`, `test_background_spawn_declares_background_transport` |
| `tests/test_background_refusal.py` | `test_run_in_background_denied_on_headless`, `test_trailing_ampersand_denied`, `test_nohup_setsid_disown_denied`, `test_double_ampersand_and_redirects_allowed`, `test_ampersand_inside_quotes_allowed`, `test_allowed_when_transport_is_background`, `test_no_op_without_transport_env`, `test_subagent_call_denied_too` |
| `tests/test_worker_crew.py` | `test_worker_kind_installs_crew_root`, `test_crew_root_is_separate_from_planner_seats`, `test_planner_kind_does_not_get_crew`, `test_briefing_add_dirs_include_crew_for_worker`, `test_crew_definitions_parse_with_expected_tools`, `test_spec_writer_has_no_bash_or_edit`, `test_serena_stripped_when_unwired` |
| `tests/test_worker_brief.py` | `test_worker_core_names_both_crew_seats`, `test_planner_core_omits_crew_block`, `test_template_version_bumped_for_crew` |
| `tests/test_crew_enforcement.py` | `test_lead_write_inside_worktree_denied`, `test_lead_edit_inside_worktree_denied`, `test_seat_write_allowed`, `test_dot_jarvis_path_exempt`, `test_path_outside_worktree_not_this_rule`, `test_require_crew_false_disables`, `test_under_review_narrowing_still_wins`, `test_planner_kind_unaffected` |
| `tests/test_spec_shape.py` | `test_spec_write_without_problem_heading_denied`, `test_spec_write_without_fix_heading_denied`, `test_conforming_spec_allowed`, `test_heading_match_is_case_insensitive`, `test_body_mention_is_not_a_heading`, `test_edit_not_checked`, `test_non_spec_markdown_allowed`, `test_no_op_for_non_worker_session` |
| `tests/test_catalog.py` | `test_worker_require_crew_defaults_true`, `test_worker_require_crew_parsed` |
| `tests/test_bootstrap.py` | `test_install_agent_assets_returns_three_roots_for_worker` |
| `tests/test_one_shot_turn.py` | unchanged — the backstop keeps its coverage; §4 does not replace it |
