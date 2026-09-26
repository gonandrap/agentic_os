# Attribution is not a caller's choice

wo-15f5d969, GitHub issue #749. One change to `claude_cli.run_headless_result` /
`run_headless` and the nine call sites that pass its opt-out.

Supersedes the `attribute` paragraph of `run_headless_result`'s docstring
(`src/jarvis/claude_cli.py:1546-1550`) and the `attribute=False` comment at each site.
Nothing in `agent_usage` or `ops.cost_report` moves: what is recorded, where it lands and
how it is reported are already right (issue #103).

## The problem

Issue #749 reports that `jarvis cost` misses the `claude -p` spend of LLM evals a worker
runs. The accounting for exactly that already exists and works:

| piece | where |
|---|---|
| the kind | `agent_usage.WORKER_SUBPROCESS`, `src/jarvis/agent_usage.py:69` |
| the recording seam | `claude_cli._attribute_subprocess`, `src/jarvis/claude_cli.py:1458`, keyed on `JARVIS_WO_ID` |
| reaching live state from a sandboxed eval | `testing._bills_real_tokens` / `agent_usage.SPEND_HOME_ENV`, `src/jarvis/agent_usage.py:117` |
| reaching the bill, the budget and the alarms | `budget.Spend.jarvis_usd` sums `agent_calls` |

So every fix the issue proposes — propagating env, gating `JARVIS_EVALS_LLM`, a `--bare`
prompt — aims at machinery that is not broken. The defect is one line in the transport's
signature:

```
src/jarvis/claude_cli.py:1507   attribute: bool = True,
src/jarvis/claude_cli.py:1585   if attribute:
src/jarvis/claude_cli.py:1594   attribute: bool = True,      # run_headless, forwarded
```

A bare boolean kill switch on the accounting, reachable by any caller, recording nothing
when used and asking nothing of the caller who uses it.

**The measured consequence.** `evals/llm/test_stakes_classifier_ab.py:361` on
wo-8a3bb528's unmerged branch
(`/home/gonzalo/workspace/agentic_os/.claude/worktrees/wo-8a3bb528/`):

```python
result = claude_cli.run_headless_result(
    stakes.question(row["text"]), system_prompt=stakes.PERSONA, model=model,
    cwd=cwd, timeout=stakes.TIMEOUT, tools="", attribute=False)
```

713 calls, ~$71 at list, zero `agent_calls` rows, no reason given for the flag anywhere in
the file. The spend is invisible to `jarvis cost`, to the order's budget ceiling and to the
re-write alarms — and it is invisible *because a caller asked for it to be*, in a keyword
that costs twelve characters and leaves no trace.

**Why no test caught it.** `tests/test_worker_subprocess_spend.py` has two tests on this
seam and both bless the hole:

* `test_attribution_can_be_switched_off` (line 105) asserts the switch WORKS, for any
  caller, calling it "the escape hatch the OS's own call sites use";
* `test_every_os_call_site_opts_out_of_the_transports_attribution` (line 116) checks that
  `"attribute=False" in inspect.getsource(...)` for four OS sites — a source-text check,
  so it says nothing about who else may write those characters.

The root cause is the signature, not the eval. An eval author who typed `attribute=False`
did what the parameter invites; the parameter has no way to distinguish "I record myself,
by name, with the work order and the question" (true of nine OS sites) from "I do not want
to be billed".

## The fix

Replace the boolean with a **declaration only a self-recording OS site can make**, and
enforce it at the call, before the subprocess runs. Ruled by Neo on the user's behalf:
Option A only, and the signature changes rather than a source-text lint. Breaking
wo-8a3bb528's branch loudly at merge time is the wanted outcome.

### §1 The signature

`src/jarvis/claude_cli.py:1503` and `:1589`:

```python
def run_headless_result(prompt, ..., records_itself: str = "", record=None, ...)
def run_headless(prompt, ..., records_itself: str = "", record=None, ...)
```

`attribute: bool` is DELETED, not deprecated — a kwarg kept as an alias is the same hole
with a longer name, and the loud break at merge is the point. `records_itself` names the
`agent_calls.kind` the caller will write for this call itself. Empty (the default) means
the transport attributes, which is today's behaviour for every caller that does not opt
out. `src/jarvis/claude_cli.py:1585` becomes `if not records_itself:`.

### §2 The check, at the call, before the subprocess

A new private helper in `claude_cli`, called from `run_headless_result` as its FIRST
statement — before `args` is built and before `_run` — so a refused call spends nothing:

```python
def _check_records_itself(kind: str) -> None      # raises AttributionRefused
```

Three conditions, all required:

1. `kind in agent_usage.KIND_LABELS` (`src/jarvis/agent_usage.py:87`). A caller that
   records itself knows which kind it writes; a caller that cannot name one is not
   recording itself. `KIND_LABELS` stays open for *recording* (`agent_usage.record` takes
   any string) and is closed only for *declaring* — the opt-out is the one place a
   made-up kind would buy silence.
2. `kind not in agent_usage.SUBPROCESS_KINDS` (`:81`). `worker_subprocess` is the kind
   this transport writes; a caller claiming it is claiming to have written the row that
   was not written.
3. The calling module is inside the `jarvis` package (§3).

### §3 Which module is calling, and which frame says so

Identified by walking `sys._getframe()` outward and reading each frame's
`f_globals.get("__name__", "")`. Not by an argument: anything the caller passes, the
caller can forge, and `records_itself="neo_answer", module="jarvis.neo"` typed in an eval
is exactly the misuse being closed. A frame's module is a fact about where the code is.

The walk, precisely:

1. Skip frames whose `__name__` is `jarvis.claude_cli` itself. `run_headless` forwards to
   `run_headless_result`, so without this the immediate caller of every `run_headless`
   call is the transport, and every check would pass.
2. Take the first frame that remains — the nearest real caller. If its module is
   `jarvis` or starts with `jarvis.`, accept.
3. Otherwise keep walking. Accept if ANY frame further out is inside `jarvis`; refuse if
   none is, naming the nearest caller's module in the message.

**The two `functools.partial` sites need no special case.** `functools.partial` is C-level
and pushes no Python frame, so the frame the walk finds is whoever *invoked* the partial —
for both `digest.CALL` and `structured.DEFAULT_CALL` that is `structured.request` at
`src/jarvis/structured.py:217`, inside `jarvis`. The bindings are created in
`jarvis.digest` / `jarvis.structured`, and those creation sites are invisible here; the
INVOCATION site is what is inspected, and it is the right one — what must be true is that
an OS code path is making the call now, not that an OS module typed the keyword once at
import time.

**Step 3 exists for test doubles that re-enter the transport.** Two eval fixtures replace
`claude_cli.run_headless_result` with a wrapper and forward `**kwargs` into the real one:

* `evals/llm/test_validation_judgment.py:1193` — `Meter.__call__`, `return self._real(prompt, system_prompt=system_prompt, **kwargs)`
* `evals/llm/test_neo_panel_judgment.py:214` — the same shape

Both drive the REAL panel: the declaration in those `kwargs` was made by `jarvis.seats`
and `jarvis.validation`, and the nearest frame is the eval module. Under a
nearest-frame-only rule both suites die at every seat, and the frames below them are
`jarvis.validation._run_seats` / `jarvis.panel._round` — genuinely the OS spending. So the
rule is "the OS's own code path is on the stack", not "the OS's own code typed the call".

Residual hole, accepted and named: a caller outside `jarvis` that gets itself invoked from
*within* an OS frame (a `call=` seam, an `on_usage` callback) can declare. It is already
inside a path that binds the row through `on_usage`, and closing it would cost the two
fixtures above. If it ever bites, the fix is a sanctioned-double registry, not a stricter
walk.

### §4 The refusal

```python
class AttributionRefused(RuntimeError)      # src/jarvis/claude_cli.py, beside ClaudeCliError
```

**NOT a subclass of `ClaudeCliError`**, and that is load-bearing.
`src/jarvis/seats.py:213`, `src/jarvis/panel.py:611` and `src/jarvis/validation.py:1165`
each catch `ClaudeCliError` and turn it into an abstention; a refusal caught there would
become a silent abstention with no row and no noise — the bug being fixed, wearing the
fix's clothes. `AttributionRefused` propagates past all three.

Messages, verbatim, one per condition:

```
records_itself='{kind}' is not an OS accounting kind (agent_usage.KIND_LABELS): a caller
that records a call itself must name the kind it writes.

records_itself='{kind}' is the kind this transport writes itself; a caller cannot claim it.

records_itself='{kind}' is the OS's own opt-out from subprocess attribution and {module}
may not make it: a call from outside the jarvis package is billed to JARVIS_WO_ID and
cannot be switched off. Delete the argument.
```

**Why this raises, when `agent_usage` says it never does.** The module docstring rule
(`src/jarvis/agent_usage.py:42-47`) is about a call that HAS HAPPENED and been paid for:
failing the work order because a row could not be written costs money twice, so the row is
lost and the total is a floor. This check runs before any money is spent — the refused call
makes no request, so there is nothing to under-report and no work to lose. A swallowed
refusal is precisely the defect above: $71 spent, nothing recorded, nobody told. The rule
is "never let accounting break paid work", and refusing an unpaid call does not break it.

### §5 Call sites

| site | today | becomes |
|---|---|---|
| `src/jarvis/neo.py:354` | `attribute=False` | `records_itself="neo_answer"` (written at `neo.py:356`) |
| `src/jarvis/panel.py:610` (chair) | `attribute=False` | `records_itself="panel_seat"` (written by `panel._record`, `:232`) |
| `src/jarvis/seats.py:212` (`_run_seat`) | `attribute=False` | `records_itself=kind`, from a new keyword (below) |
| `src/jarvis/seats.py:249` (`prime_cache`) | `attribute=False` | `records_itself=kind`, same |
| `src/jarvis/validation.py:1164` (chair) | `tools="", attribute=False` | `tools="", records_itself="validation_seat"` (written by `_record_usage`, `:1135`) |
| `src/jarvis/digest.py:106` | `partial(..., tools="", attribute=False)` | `partial(..., tools="")` — the declaration moves into `summarise` (below) |
| `src/jarvis/structured.py:47` | `partial(..., attribute=False)` | `partial(claude_cli.run_headless_result)` — the declaration moves to `request`'s caller (below) |

Three of those need a parameter threaded, because the module making the call does not know
which kind the caller writes:

* **`seats`** serves two kinds — `panel._record` writes `panel_seat` (`panel.py:232`),
  `validation._record_usage` writes `validation_seat` (`validation.py:1135`). So
  `_run_seat`, `prime_cache` and `run_blind` take a required keyword `kind: str` and
  forward it as `records_itself=kind`. Callers: `panel.py:204` passes
  `kind="panel_seat"`; `validation.py:987` (`prime_cache`) and `validation.py:993`
  (`run_blind`) pass `kind="validation_seat"`. A literal at each caller, matching the
  literal it already writes the row with — a hardcoded `"panel_seat"` inside `seats`
  would be false for every validation round.
* **`structured.request`** takes `records_itself: str = ""` and forwards it to `call(...)`
  **only when non-empty**. Non-empty-only keeps the kwarg out of every test fake's
  captured kwargs — `tests/test_structured.py:327-330` asserts on an exact kwargs dict —
  and leaves the `call=` fakes (all `**kw`) untouched. Its paying callers declare:
  `supervisor.py:718` passes `records_itself="supervisor"`, `supervisor.py:872` passes
  `records_itself="health"`, matching the `agent_usage.recorder` kinds they already bind
  at `:735` and `:889`.
* **`digest.summarise`** passes `records_itself="digest"` in its `structured.request` call
  (`digest.py:182`), matching the kind `daemon.py:2908` binds. `daemon` is unchanged.

`agent_usage.COMPACTION` is not a site here: `worker_session` compacts through the
resume/send transport, not `run_headless`, and records itself at `worker_session.py:498`.

### §6 Tests

`tests/test_worker_subprocess_spend.py`, replacing both of the tests that blessed the hole:

1. `test_an_outside_caller_cannot_switch_attribution_off` — replaces
   `test_attribution_can_be_switched_off` (line 105). With `JARVIS_WO_ID` set, a call
   naming a real kind from a module outside `jarvis` raises `AttributionRefused`, and
   `agent_calls` is empty because nothing ran. The test module is itself outside `jarvis`,
   so the call in the test body *is* the outside caller — no fixture needed.
2. `test_an_eval_shaped_caller_naming_a_real_kind_is_still_refused` — the #749 shape
   exactly: `records_itself="neo_answer"` from a frame whose module is
   `evals.llm.test_stakes_classifier_ab` (synthesised by executing the call inside a
   function compiled with that `__name__`). Naming a genuine kind buys nothing.
3. `test_the_refusal_happens_before_any_subprocess_is_spawned` — monkeypatch
   `claude_cli._run` to a function that fails the test if called; assert
   `AttributionRefused` and that it never ran. The whole value of checking at the call is
   that a misuse costs zero dollars.
4. `test_every_os_site_declares_a_kind_the_transport_accepts` — replaces the source-text
   test (line 116). Parametrised over the nine declarations, asserting each passes
   `_check_records_itself` from a `jarvis`-module frame: `neo_answer`, `panel_seat`
   (chair), `panel_seat` (seat), `panel_seat` (prime), `validation_seat` (chair),
   `validation_seat` (seats), `digest`, `supervisor`, `health`. Kept alongside a
   source-level assertion that no `src/jarvis` file still contains `attribute=False`, so
   a half-done migration is caught rather than silently reverting a site to being billed
   twice.
5. `test_a_declaration_of_an_unknown_or_subprocess_kind_is_refused` — `"neo_answers"`,
   `""`-adjacent junk, and `agent_usage.WORKER_SUBPROCESS` itself.
6. `test_a_test_double_between_the_os_and_the_transport_still_records_itself` — the
   `Meter` shape from `evals/llm/test_validation_judgment.py:1193`: an outside-module
   wrapper forwarding `records_itself` while a `jarvis` frame is below it is ACCEPTED.
   This is the rule in §3 step 3, and without a test the next tightening of the walk
   kills both LLM eval suites.

## Rejected alternatives

* **Keep `attribute: bool`, add a lint that greps `evals/` for it.** What the old
  `test_every_os_call_site...` already was, one directory over. It reads source, so it is
  evaded by `attribute=bool(0)`, by a forwarded `**kwargs`, or by a caller outside the
  globbed path — and evasion is not even needed, only ignorance. Neo's ruling: enforce at
  the call.
* **Deprecate `attribute` and warn.** A `DeprecationWarning` under `pytest` is captured
  and unread. The loud break at merge time is the deliverable; a warning is its opposite.
* **Take the caller's identity as an argument (`records_itself=("neo_answer", __name__)`).**
  Forgeable in one token by the caller with a motive to forge it. Frames are not.
* **Drop the opt-out entirely and dedupe rows later.** The two rows are indistinguishable
  after the fact: same tokens, same model, one with a work order and question and one
  without. Deduping would have to guess, and `agent_usage` exists because guessing after
  the fact is impossible here.
* **Fix the eval on wo-8a3bb528's branch instead.** Fixes one caller and leaves the
  signature that invited it. The branch breaking loudly at merge is the wanted outcome,
  not the work.
* **Whitelist the two eval `Meter` modules by name.** Every future test double edits the
  list, and a stale list fails as an outage in an eval suite nobody runs weekly.

## Not covered

* The floor claim stands and must: a bare `claude -p` from a shell reaches no seam Jarvis
  owns (`agent_usage.py:22-24`, `ops.COST_FLOOR_NOTE`). This closes the deliberate hole,
  not the unreachable one.
* No change to `KIND_LABELS`' openness for recording, to `SPEND_HOME_ENV`, to
  `_attribute_subprocess`'s keying, or to any report.
* Re-attributing wo-8a3bb528's 713 already-spent calls. They were never recorded; there is
  nothing to read them back from.
* `evals/llm/test_stakes_classifier_ab.py:361` itself — it lives on another branch. Its
  merge is where this fix is enforced: the line raises, and the eval's calls get billed to
  whatever work order runs them.

## Open

* Whether `records_itself` should be keyword-only. It is at every site listed, and
  positional use of a 7th parameter is not a real risk; making it keyword-only is one
  `*` and slightly louder. Implementer's call.
* Frame-walk depth is unbounded as written. Every stack here is shallow (transport, an OS
  function, the daemon or a fixture); if profiling ever objects, cap it at ~20 frames and
  refuse past that rather than accept.
