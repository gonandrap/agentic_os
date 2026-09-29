# A heredoc edit is not a merge

Work order wo-42639749. GitHub issue #849, plus its follow-up comment. Neo question 1001
approved fixes 1-3; fix 4 is the issue's own follow-up.

## The problem

wo-37f2fc2c's `jarvis-implementer` subagent appended tests to `tests/test_automerge.py`
by piping a heredoc into an interpreter (`python3 - <<'PYEOF' … PYEOF`). The test TEXT
contained `gh pr merge` and `['pr','merge']`. Gate 315 matched `pr_merge`
(rule gr-5b7176ece8), the request was filed `awaiting_case`, nobody argued it, and
`gates.sweep_unargued` abandoned it 4 minutes later. For 4m44s the work order read
"Waiting on you" while nothing was owed by the user. 42 of the last 200 gates were
dismissed as classifier false positives; 23 were abandoned.

Four independent defects produced that, and each is a defect on its own:

1. **A file edit was written through an interpreter at all.** Nothing stops a worker or
   its subagent from writing files with `python3 - <<PY`, so arbitrary file content is
   routinely handed to a command classifier. `Edit`/`Write` are auto-allowed inside the
   worktree (`hooks.preflight_decision`, src/jarvis/hooks.py:1312-1316) and cost nothing;
   the heredoc route is the only one that can trip a gate.

2. **A held request renders as a user blocker.** `gates.file_request` parks the work
   order in `waiting_input` down BOTH roads, `hold=True` included
   (src/jarvis/gates.py:996-998). `waiting_input` renders "Waiting on you"
   (src/jarvis/timeline.py:88) and `invariants.true_blockers` derives
   `"worker is waiting on your input"` from it (src/jarvis/invariants.py:750-765). An
   `awaiting_case` request is owed by the WORKER — by construction, it is the one status
   whose only exit is the worker's own next command
   (`gates.queue_for_review`, src/jarvis/gates.py:1021-1040).

3. **A gate a SUBAGENT raised can never be argued.** `hooks._resolve_gate` files
   `hold=True` (src/jarvis/hooks.py:1109) and records `agent_type=payload.get("agent_type")`
   (src/jarvis/hooks.py:1115), which is set iff a subagent ran the command. The only
   mechanism that forces a held request to be argued is `held_request_turn_block`
   (src/jarvis/hooks.py:1196-1246), which runs on `Stop` — the LEAD's turn boundary. No
   `SubagentStop` hook is registered: src/jarvis/assets/settings.base.json declares
   PreToolUse, PostToolUse, PreCompact, SessionStart, SubagentStart (:42-49), Stop (:50),
   SessionEnd and Notification, and nothing else. The lead is never told. So a gate raised
   inside a subagent is structurally guaranteed to reach `sweep_unargued`
   (src/jarvis/gates.py:1074-1101) — abandonment is not a worker failure here, it is the
   only reachable outcome.

4. **The state view describes the newest ROW, not the newest activity.** 45 minutes after
   the abandonment wo-37f2fc2c still read `00:36 · gate_abandoned · still in it` while its
   implementer subagent was running pytest and its transcript grew from 699 to 963 rows.
   `ops._activity_of` (src/jarvis/ops.py:1653-1679) reads five DB tables — `wo_events`,
   `wo_turns`, `wo_messages`, `validation_rounds`, `approvals` — and none of them is
   written DURING a turn that is still in flight, so `last_activity_ts` freezes at the
   turn's start and the span's `trigger` line (src/jarvis/ui/templates/_states.html:72)
   keeps naming the transition that opened the span.

### Root cause, and the symptom being patched on purpose

Root cause of the incident is defect 1: a file edit was routed through a command. Defects
2-4 are the OS's handling of the consequence, and each is a genuine defect of its own —
they are fixed here because they misreport state to the user regardless of how the gate
was raised, not to paper over defect 1.

### What is NOT the fix

Issue #849's suggestion (4) — treat a literal that only ever sits in a string assignment
as non-executing — is REFUSED. `gate_rules` is not touched by any fix below.

- A heredoc body handed to an interpreter IS code. `gate_rules._EXECUTORS`
  (src/jarvis/gate_rules.py:317-321) contains `python3`, `python`, `perl`, `ruby`, `node`,
  `awk` on purpose, and `heredoc_spans`' owner test (src/jarvis/gate_rules.py:528-539) is
  exactly the `git commit -F - <<EOF` (data) versus `python - <<PY` (program) distinction.
- `scannable()` (src/jarvis/gate_rules.py:468-482) deliberately does not blank heredoc
  bodies, on the stated rule that a payload something re-parses is code and that a
  spurious gate costs one review while a missed one ships unreviewed.
- The shape is PINNED: `SEED_CANARIES` carries
  `("release", "python3 - <<'PY'\nscripts/shipit.sh\nPY")` (src/jarvis/gate_rules.py:1328),
  added by the issue #203 fix precisely to stop this relaxation, and
  `RuleSet.check_canaries` (src/jarvis/gate_rules.py:1533-1547) re-probes it on every
  reconcile tick as `INV-GATE-CANARY`.
- Knowledge entries kn-986fc008 and kn-f5e46f07 rule the relaxation out for the same
  reasons. Reaching for it a second time is the expected failure mode of this file; the
  refusal is stated here so the next reader does not have to re-derive it.

The classifier is right. What is wrong is that a file edit ever reached it, that a held
request reads as a user wait, that nothing will ever argue a subagent's request, and that
the state view goes stale under a live turn.

## The fix

Four changes. None touches `gate_rules`; none makes any privileged command reachable.

### Fix 1 — steer file edits away from heredocs (issue item 3)

New PreToolUse decision `heredoc_write_decision(payload, env)` in src/jarvis/hooks.py,
called from `preflight_decision` (src/jarvis/hooks.py:1249) in the Bash block **before**
`gate_decision` (src/jarvis/hooks.py:1271-1274).

DENY only on POSITIVE EVIDENCE OF A FILE WRITE inside the worker's own worktree, in a
command that pipes or redirects a heredoc into an interpreter
(`python3`/`python`/`perl`/`ruby`/`node`/`sed`/`awk`/`cat`). Evidence means one of:

- a shell redirect to a path — `> path`, `>> path`, `| tee path`;
- `sed -i` (reuse the left-to-right cluster parse in `_sed_writes`,
  src/jarvis/hooks.py:692-737, rather than a second spelling of it);
- a Python write target in the heredoc body — `open(<path>, 'w'|'a'|'w+')`,
  `.write_text(`, `Path(...).open('w'`.

Never on "the gate matched" and never on "this is a heredoc". A heredoc that computes and
prints is untouched, as is `git commit -F - <<EOF` (not an interpreter). A write whose
path resolves OUTSIDE the worktree is not this rule's business and is left to the rules
that already own it — the same boundary `investigator_write_decision` draws
(src/jarvis/hooks.py:740-784).

Deny text: use `Edit` or `Write`, which are auto-allowed in your worktree; a heredoc that
writes a file hands your file's CONTENT to the command classifier.

**Timeline event.** On the deny path only, open the project store (`_worker_context`,
src/jarvis/hooks.py:1165) and `add_event(wo_id, "heredoc_write_refused", {...})` with
`session_id`, `agent_type` and a truncated command — the shape
`held_request_turn_block` already writes (src/jarvis/hooks.py:1222-1226). Event kinds are
not registered anywhere; `timeline.event_level` (src/jarvis/timeline.py:101-108) levels
by membership in `DEBUG_KINDS` (src/jarvis/timeline.py:32-59) and an unknown kind is
`signal`. Leave it OUT of `DEBUG_KINDS`: a refused write is a fact about the work, and
the whole point is that the denial stays visible on the record. Opening the store only on
the deny path keeps the common case at zero extra I/O.

**Why before `gate_decision`.** `gate_decision` never returns None once a command is
recognised as privileged (src/jarvis/gates.py / src/jarvis/hooks.py:958-991), so placed
after it this check is unreachable for exactly the commands it exists for. Running first
means **no approval row is filed for this class at all** — that is the point: the incident
cost was a filed, held, abandoned request, not a blocked command.

`preflight_decision`'s docstring sentence "Gated privileged actions are resolved FIRST, so
no auto-approval below can hand out a merge or a release by accident"
(src/jarvis/hooks.py:1259-1261) must be amended, and the amendment must say why this one
check may legally precede the gate: **it only ever denies.** It has no `_allow` branch,
so it cannot hand out a merge or a release, and the command it refuses stays BLOCKED — a
strictly smaller set of commands runs than before. The ordering argument the docstring
makes is about auto-ALLOWS, and this is not one.

**Must remain true.** No `_allow` return in the new function. `gate_rules` untouched, so
`check_canaries()`/`INV-GATE-CANARY` are unaffected. A command that performs a privileged
action and also writes a file is denied here instead of gated — still blocked, and a retry
without the file write reaches the gate normally.

**First failing test** (new `tests/test_heredoc_writes.py`, beside
`tests/test_gate_enforcement.py`):
`test_a_heredoc_that_writes_a_worktree_file_is_denied_before_a_gate_is_filed` — a
`python3 - <<PY` whose body contains `open(<worktree path>, "a")` and `gh pr merge` text;
assert the hook denies, that the reason names `Edit`/`Write`, that
`store.list_approvals(wo_id)` is EMPTY, and that one `heredoc_write_refused` event exists.
Negatives in the same file: a compute-and-print heredoc is allowed through to
`gate_decision`; `git commit -F - <<EOF` is untouched; a heredoc writing a path outside
the worktree is not denied by this rule.

### Fix 2 — a held gate is never "Waiting on you" (issue item 2, Neo option B2)

No new status. `gates.file_request` (src/jarvis/gates.py:963-999) sets `waiting_input`
only when `hold` is False; a held request leaves the order in `running`/`dispatching`.

That alone would make an idle worker holding an unargued gate fall to
`Daemon.settle_work_order`'s `else` (src/jarvis/daemon.py:4578) and be filed
`needs_review` + `IDLE_NO_FINISH_BLOCKER`. Two coordinated edits stop that:

1. **src/jarvis/daemon.py:4504.** Widen the parked-on-delegate branch to
   `something_is_out` (src/jarvis/invariants.py:1550-1568), which already unions
   `pending_approvals`, `held_approvals` and `awaiting_neo` — and EXCLUDE managers from it
   (`wo.get("kind") != "manager"`), so the manager branch at src/jarvis/daemon.py:4517
   stays reachable exactly as today. Inside the branch, the WRITE is conditional: set
   `waiting_input` only when the wait is genuinely outward-facing.

2. **A second resolver in `invariants`**, `user_facing_wait(store, wo_id)` =
   `pending_approvals(wo_id) or awaiting_neo(wo_id)`. It answers a different question from
   `something_is_out` — "is somebody ELSE holding this" versus "is anything out at all" —
   and both live next to each other so the pair cannot drift, which is kn-4ea33fe6's rule
   applied rather than broken. `something_is_out` keeps its current membership and its
   current callers.

Consequences, each one to be pinned:

- Non-manager, held-only: stays `running`, no status write, no attention flag.
- Manager, held-only: src/jarvis/daemon.py:4548's `something_is_out` guard becomes
  `user_facing_wait`, so it falls through to `idle` (src/jarvis/daemon.py:4564). That is
  the truthful label — nothing is owed by the user and its feature wakes it — and it is
  the one deliberate behaviour change beyond the status itself. The comment at
  src/jarvis/daemon.py:4554-4562, which argues that the `elif` above "misses
  `awaiting_case`", becomes false and must be rewritten to say that the branch above now
  covers it and that a manager is excluded from it on purpose.

**Docstrings that become false and must be edited:**

- `invariants.something_is_out` (src/jarvis/invariants.py:1550-1568): the sentence
  "`gates.file_request` parks it in `waiting_input` down BOTH its roads" is exactly the
  claim this fix falsifies. Rewrite it: a held request parks NOTHING; the work order is
  still out because the command is blocked and the `case_ttl_seconds` clock is running.
  The function's RETURN is unchanged — `held_approvals` still counts as out.
- `gates.file_request` (src/jarvis/gates.py:984-997): the closing "True of a held request
  too" paragraph.

**Every other surface that leans on it, and whether it still holds:**

| Surface | Verdict |
|---|---|
| `gates.sweep_unargued`'s `end_wait_if_nothing_is_out` (src/jarvis/gates.py:1099) | HOLDS, and must stay. It is a no-op from `running` (src/jarvis/invariants.py:1589) and is still needed for the order that is in `waiting_input` for a DIFFERENT reason. |
| `invariants.true_blockers` waiting_input branch (src/jarvis/invariants.py:750-765) | HOLDS unedited; a held-only order no longer reaches `waiting_input`, so the generic blocker is unreachable for it. `_waiting_on_neo_gate` reads pending gates and is unaffected. |
| `remedies._can_drop_hold` / `_apply_drop_hold` (src/jarvis/remedies.py:931, :966) | HOLD. Both test `approval["status"] != "awaiting_case"` — the APPROVAL's status, which this fix does not touch. |
| `ui/app.py:1516` held list | HOLDS, same reason: it partitions `ops.list_gates` by approval status. |
| `hooks.held_request_turn_block` (src/jarvis/hooks.py:1214-1219) | HOLDS. It reads `held_approvals` and `TERMINAL_STATUSES`, never `waiting_input`. |
| `timeline.STATUS_LABEL` (src/jarvis/timeline.py:88) | Unchanged — the label is right, the status was wrong. |

**Must remain true.** The command stays blocked while `awaiting_case`; `sweep_unargued`
still abandons on the same clock; nothing in `gate_rules` changes.

**First failing test** (`tests/test_gates.py`):
`test_a_held_request_leaves_the_work_order_running` — file via the hook with `hold=True`,
assert `get_work_order(...)["status"] == "running"`, `true_blockers(...) == []` and
`needs_attention` falsy. Then, per surface:
`tests/test_gates_pipeline.py::test_an_idle_worker_holding_a_gate_is_not_filed_for_review`
(settle a turn with no summary; assert still `running`, not `needs_review`, no
`IDLE_NO_FINISH_BLOCKER`); `tests/test_gates.py::test_the_sweep_still_abandons_a_held_request_on_a_running_order`;
`tests/test_manager_order.py::test_a_manager_holding_an_unargued_gate_is_idle_not_waiting_on_you`;
`tests/test_invariants.py` for `user_facing_wait` both ways;
`tests/test_remedies.py` unchanged and must stay green.

### Fix 3 — a gate a subagent raised must reach an actor (issue item 1)

In `hooks._resolve_gate` (src/jarvis/hooks.py:1106-1116): when `agent_type` is set, file
with `hold=False`. Everything else about the call is unchanged, including
`justification=gates.no_case_justification(...)` — `file_request` then routes through
`queue_for_review` (src/jarvis/gates.py:1021-1040) and a reviewer decides within the
minute instead of nobody deciding in four.

Nothing else in `gates.file_request` or `queue_for_review` changes: `hold` is already the
single discriminator, and the `pending` road already sets `waiting_input` (correct here —
a reviewer really is holding it).

**The lead can still attach a case.** `ops.request_gate_approval`
(src/jarvis/ops.py:8676-8714) accepts a `pending` row and routes it to
`gates.amend_request` (src/jarvis/gates.py:1124-1153), which merges the case onto the
standing row and revises the reviewer's question IN PLACE through
`neo.revise_question`, never re-pointing `approvals.neo_question_id`. It also already
tells the caller when the reviewer may have read the earlier text
(src/jarvis/ops.py:8703-8709). **No change is needed here** — the smallest change is
none.

**The deny text must change.** The current text (src/jarvis/hooks.py:1122-1139) instructs
the caller to make the case or contest the match, which a subagent cannot reliably do: it
has no turn boundary the OS can block and no channel to its lead. For the `agent_type`
branch it must say: the request is already with a reviewer; report the block to your lead
and END; a file edit belongs in `Edit`/`Write`. Do not print `exits_advice` there — it
names actions this actor cannot take.

**The counter-argument, and why it does not apply.** GitHub issue 185 is why the hook
holds at all: a placeholder handed to a reviewer gets decided before the worker's real
case arrives. That argument assumes an actor that will argue. Here there is none — see
defect 3 above — so the choice is not "placeholder now versus case later", it is
"placeholder now versus abandoned, every time". If the lead does argue afterwards,
`amend_request` attaches it and the `read_already` note says what happened.

**Must remain true.** A LEAD's request (no `agent_type`) still holds — this branch is
keyed on `agent_type` alone. The command stays blocked until a verdict. Nothing about
grants, scope or `gate_rules` changes.

**First failing test** (`tests/test_gates.py`):
`test_a_subagents_gate_goes_straight_to_a_reviewer` — call the hook with
`payload["agent_type"] = "jarvis-implementer"`; assert the approval's status is `pending`,
that it has a `neo_question_id`, that the deny reason does NOT tell the subagent to make
the case, and that the same payload WITHOUT `agent_type` still files `awaiting_case`.
Then `tests/test_gates_pipeline.py::test_a_subagent_gate_is_decided_by_the_neo_drain`.

### Fix 4 — the state view must describe the latest activity (the issue's follow-up)

Add a SIXTH activity source to `ops._activity_of` (src/jarvis/ops.py:1653-1679): the
worker session transcript's last write, plus the subagent transcripts beside it.

- Path: `claude_cli.session_transcript_path(cwd, session_id)`
  (src/jarvis/claude_cli.py:1873-1877), with `cwd` the worktree —
  `store.project_path / ".claude" / "worktrees" / wo["worktree"]` when set, else
  `store.project_path`. That is `worker_session.worktree_path`'s own computation
  (src/jarvis/worker_session.py:100-105) without the `ProjectSpec` import; `ops` must not
  load the catalog for a read this cheap. Same construction as
  `worker_session._conversation_started` (src/jarvis/worker_session.py:1341-1347).
- Subagents: `inspection._subagent_transcripts(path)` (src/jarvis/inspection.py:1722-1730)
  — `<session>/subagents/*.jsonl`. This is the case in the incident: the LEAD's transcript
  was quiet while the implementer's grew.
- Contribute `(max(mtime), "transcript")`.

**mtime, not a parsed last row — stated and justified.** `state_durations`'s contract is
"one indexed read per table, no model, nothing written" (src/jarvis/ops.py:1716). A
`stat()` is the only reading of a transcript that keeps that promise: parsing the last
JSONL row of a file that reached 963 rows mid-turn means reading or seeking a
multi-megabyte file on every dashboard render, and the extra precision buys nothing — the
question is "did anything happen recently", not "what". Cost is one `glob` of a small
directory plus N `stat()`s. mtime is coarser than a row timestamp in exactly one
direction (a flush lag of seconds), which is immaterial against the 45-minute error.

**Absent contributes NOTHING, never a zero.** Wrap the path work so `OSError` /
`FileNotFoundError` / a missing `session_id` appends no tuple at all. `ops.NO_ACTIVITY_NOTE`
(src/jarvis/ops.py:1532-1533) and `_states.html:35-36`'s "nothing on the record at all"
branch distinguish absent from old, and a `0.0` would be rendered as activity in 1970.
`state_durations`' `if not activity_kind` note (src/jarvis/ops.py:1750-1752) keeps
working because the new source, like the others, is only appended when it has a value.

**What the span line says.** For an OPEN span whose last activity is NEWER than the
transition that opened it, `_states.html:72` should read
`{{ s.trigger }} · still in it · active <N> ago` — the "still in it" suffix is not wrong,
it is just not the live fact. The comparison is `states.last_activity_ts > s.entered` on
the open span only, and it is the renderer's, not a new field: `as_dict` already exposes
`last_activity_ts`, `last_activity_age` and `last_activity_age_human`
(src/jarvis/ops.py:1633-1636), so no payload key is added and the `--json` contract
pinned by `tests/test_time_in_state.py::PAYLOAD_KEYS` is unchanged.

**Must remain true.** Read-only and model-free; no write; `_last_activity` for a feature
order still unions its family (src/jarvis/ops.py:1682-1706) and now inherits the same
source per child.

**First failing test** (`tests/test_time_in_state.py`):
`test_a_live_subagent_transcript_counts_as_activity` — backdate all five tables to
`NOW - 45*MINUTE`, write `<CLAUDE_CONFIG_DIR>/projects/<munged>/<session>/subagents/a.jsonl`
with mtime `NOW - 30*SECOND`, assert `last_activity_age == 30` and
`last_activity_kind == "transcript"`. Negatives beside it:
`test_a_missing_transcript_contributes_nothing` (no file: `last_activity_ts is None` and
`ops.NO_ACTIVITY_NOTE` in notes, NOT a 1970 timestamp) and
`test_an_unreadable_transcript_directory_is_absent_not_zero`.

## Coverage already in place

`tests/test_gates.py` (units, both sides of the boundary), `tests/test_gates_pipeline.py`
(the loop through the real daemon and the Neo drain), `tests/test_gate_enforcement.py`,
`tests/test_gate_rules.py` (canaries — must stay green untouched, since no fix here edits
`gate_rules`), `tests/test_gate_user_authorisation.py`, `tests/test_invariants.py`,
`tests/test_parked_work.py`, `tests/test_needs_review_reason.py`,
`tests/test_manager_order.py`, `tests/test_remedies.py`, `tests/test_time_in_state.py`,
`tests/test_ui.py`. `uv run pytest tests/ evals/` before the PR.

## Out of scope

- Registering a `SubagentStop` hook. It would be the other answer to defect 3 and is a
  bigger change (a new hook event, a new handler branch, and a second forcing mechanism
  beside `held_request_turn_block`); fix 3 removes the need for one in this class by
  making the request reviewable without an actor. If subagent turn boundaries are wanted
  for other reasons, that is its own work order.
- Narrowing `gate_rules`' recognisers, learned exemptions, or the dismissal rate itself.
  Fix 1 removes one large source of false positives at the origin; the remaining rate is
  a separate measurement.
- Anything about how a subagent reports a block back to its lead beyond the deny text.
