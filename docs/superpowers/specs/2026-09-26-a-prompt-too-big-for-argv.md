# A prompt too big for argv

Work order wo-956c29bc, issue #797. Neo ruling q749 (binding).

## The problem

Linux caps ONE argv entry at `MAX_ARG_STRLEN` = 131,072 bytes. `execve` fails with
`OSError` errno 7 (`E2BIG`, "Argument list too long") before `claude` starts, so no CLI
output, no result envelope, nothing for `_cli_failure` to classify. Two sites put a whole
prompt in one argv entry:

* `src/jarvis/claude_cli.py` — `run_headless_result`: `args = ["-p", prompt, …]`.
  This is the transport for Neo (`neo.answer_question`), every panel seat
  (`panel`, `validation`, `seats`) and the dashboard digest.
* `src/jarvis/claude_cli.py` — `turn_args`: `args += ["--", prompt]`, the worker's
  whole dispatch brief, on both spawn paths in `spawn_turn` (`claude_cli.py`).

The system prompt already has the door: `SYSTEM_PROMPT_ARGV_LIMIT` (`claude_cli.py`)
and `_system_prompt_arg` (`claude_cli.py`) switch to
`--append-system-prompt-file` above 64 KiB. The USER prompt has no such door on either
site.

### What it did, measured

Neo question 722 — an `auto_review` confirmation for wo-00bd1096, prompt ~151.7K chars —
failed three times with `E2BIG` and ended `unreachable`. The dashboard digest of the same
question failed identically.

Those three failures each cost ~15 minutes and were not retries in any useful sense,
because errno 7 does not travel the failure path Neo has:

1. `_run` (`claude_cli.py`) catches `FileNotFoundError` and
   `subprocess.TimeoutExpired`. A bare `OSError` is neither, so it propagates RAW out of
   `claude_cli` — it is never a `ClaudeCliError`.
2. `neo.drain_queue` (`src/jarvis/neo.py`) branches on
   `claude_cli.UsageLimitError` then `claude_cli.ClaudeCliError`. The raw `OSError`
   matches neither and escapes the whole drain loop, so the REST of the FIFO queue is
   abandoned for that tick too.
3. It lands in `Daemon._neo_drain`'s `except Exception` (`src/jarvis/daemon.py`),
   logged as "neo drain failed". `store.release_claim` is never called, so the row stays
   `status='answering'` with `attempts` unchanged.
4. Only `NeoStore.reclaim_stale` recovers it, after `STALE_ANSWERING_SECONDS = 900`
   (`neo_store.py`), and it takes `MAX_ANSWER_ATTEMPTS = 3` sweeps
   (`neo_store.py`) to reach `failed`. ≥45 minutes of a worker parked on
   `waiting_input`, three identical doomed `execve` calls, to arrive at an outcome that
   was knowable deterministically at the first byte count.

The worker-turn site has not been hit yet. A dispatch brief is spec text plus a knowledge
index and grows monotonically; it is the same cliff with no warning at the edge.

### Root cause

A prompt is unbounded data and argv is a bounded channel. The fix is not "make prompts
smaller" (a size ceiling on a brief is a different, unrelated argument) — it is to stop
putting unbounded data through a bounded channel, at both sites, and to classify the
failure deterministically where the channel is still too small.

## The fix

Five changes, all named below by file and symbol.

### 1. The threshold

New constant in `claude_cli.py`, beside `SYSTEM_PROMPT_ARGV_LIMIT`:

```python
#: Above this many BYTES the USER prompt goes to the CLI on STDIN rather than in argv.
PROMPT_ARGV_LIMIT = 64 * 1024
```

Same number, same reasoning as `SYSTEM_PROMPT_ARGV_LIMIT`: half of `MAX_ARG_STRLEN`, so a
caller near the line cannot cross it with a few bytes of prose. A SEPARATE constant, not a
reuse of the system-prompt one: the two doors are different mechanisms
(`--append-system-prompt-file` vs stdin) and a future change to either ceiling must not
silently move the other. Both being 64 KiB also keeps the pair under `ARG_MAX` (~2 MB)
with room to spare when one call carries a large system prompt AND a large user prompt.

**The small case keeps the argv door.** A hard switch to stdin for every call would change
the observed argv of every `claude` invocation in the fleet — including
`jarvis.testing`'s fake, and the ~30 test files that read
`c["argv"][c["argv"].index("-p") + 1]` — for a defect that only exists above 128 KB. The
argv door is also the one that survives a CLI build that reads its prompt differently;
`SYSTEM_PROMPT_ARGV_LIMIT`'s comment already states this rule and this follows it.

### 2. Headless: `run_headless_result` over stdin

`claude -p` with no positional prompt reads the prompt from stdin. That is a documented
property of the CLI and it is the reason `spawn_turn` passes `stdin=DEVNULL`
(`claude_cli.py`, stated in its docstring): otherwise `claude -p` waits three seconds for
input that never comes.

In `run_headless_result` (`claude_cli.py`):

```python
over = len(prompt.encode()) > PROMPT_ARGV_LIMIT
args: list[str] = ["-p", *([] if over else [prompt]), "--output-format", "json"]
```

and pass `stdin_text=prompt if over else None` down to `_run`.

`_run` (`claude_cli.py`) grows `stdin_text: str | None = None` and passes
`input=stdin_text` to `subprocess.run`. Two required details:

* **`encoding="utf-8"` on that `subprocess.run` call.** It currently uses bare
  `text=True`, which encodes stdin and decodes stdout with the LOCALE encoding. A daemon
  under `LANG=C` would raise `UnicodeEncodeError` on any non-ASCII prompt the moment
  prompts start travelling this way. This is why the tests below require a multibyte
  case.
* `input=None` must remain the behaviour for every existing caller — `subprocess.run`
  with `input=None` and no `stdin=` leaves stdin inherited, exactly as today. No caller
  outside this function changes.

`run_headless` (`claude_cli.py`) forwards unchanged; it already delegates.

### 3. Worker turn: a prompt FILE opened as stdin, on both transports

`turn_args` (`claude_cli.py`) grows a keyword:

```python
def turn_args(prompt: str, session_id: str, resume: bool, …,
              prompt_via_stdin: bool = False) -> list[str]:
```

When `prompt_via_stdin` is true the trailing `args += ["--", prompt]` is omitted
ENTIRELY — both the fence and the prompt. The `--` fence exists to stop `--add-dir` and
`--tools` eating the prompt as an option value; with no prompt in argv there is nothing to
fence, and a trailing bare `--` is a token the fake would have to special-case. Argv ends
after the briefing flags.

`spawn_turn` (`claude_cli.py`) decides, before building args:

```python
big = len(prompt.encode()) > PROMPT_ARGV_LIMIT
args = turn_args(prompt, session_id, resume, prompt_via_stdin=big, **kwargs)
```

**The prompt file.** When `big`, write the prompt UTF-8 to `outfile.with_suffix(".prompt")`
— i.e. `<turns_dir>/<seq>.prompt`, beside the `<seq>.json` and `<seq>.err` that
`worker_session._launch` (`src/jarvis/worker_session.py`) already created for this
turn. Not `tempfile.NamedTemporaryFile` and not `/tmp`.

**Why a context manager is wrong here, and who deletes it.** `spawn_turn` RETURNS
IMMEDIATELY; the detached `claude` reads its stdin later, and on the systemd path the unit
may not even have started when `systemd-run` exits. A `NamedTemporaryFile(delete=True)`
context manager would unlink the file on the way out of `spawn_turn`, and the worker would
read EOF — a turn dispatched with an EMPTY BRIEF, which fails silently and looks like a
confused worker, not like a transport bug. Ownership instead:

* the file belongs to the TURN, like `<seq>.json` and `<seq>.err`;
* `worker_session._reap` (`worker_session.py`) deletes it —
  `Path(turn["outfile"]).with_suffix(".prompt").unlink(missing_ok=True)` — after
  `read_turn_result` has returned, at which point the process has ended and stdin has
  been read or never will be;
* a turn that is never reaped leaves the file in the turns directory, where
  `ops.delete_work_order`'s tree removal takes it with everything else. A stale prompt
  file is bytes on disk in the work order's own state directory, not a leak into `/tmp`.

**Direct `Popen` path** (`claude_cli.spawn_turn`): open the prompt file for reading inside
the existing `with` block and pass it as `stdin=`. The child inherits a dup of the fd;
the parent's handle closing on block exit is harmless. Keep `stdin=subprocess.DEVNULL`
for the small case, unchanged — that is what stops the 3-second wait.

**systemd path**: `systemd_units.run_prefix` (`src/jarvis/systemd_units.py`) hardcodes
`--property=StandardInput=null`. It grows `stdin: Path | None = None` and emits
`--property=StandardInput=file:{stdin}` when given one, `null` otherwise. `spawn_turn`
passes the prompt file through. `StandardInput=file:<path>` is systemd's own documented
form and is the exact counterpart of the `StandardOutput=file:` already used two lines
above; the default stays `null`, so every existing call site and the no-prompt-file case
are byte-identical.

### 4. `InputTooLargeError`: errno 7 is deterministic, not an outage

New class in `claude_cli.py`, beside `ClaudeCliError`:

```python
class InputTooLargeError(ClaudeCliError):
    """`execve` refused the argv: one argument was past `MAX_ARG_STRLEN`.

    A ClaudeCliError subclass so existing handlers still catch it, but a SEPARATE class
    because it is deterministic: the same call will fail the same way for ever, so a
    retry is only a delay. Distinguished from every transient outage by that fact.
    """
```

`_run` grows, AFTER the existing `except FileNotFoundError` (which is an `OSError`
subclass and must keep its own message):

```python
except OSError as e:
    if e.errno == errno.E2BIG:
        raise InputTooLargeError(
            f"`{claude_bin()}` could not be started: the input was too large for the "
            f"command line ({sum(len(a.encode()) for a in args)} bytes of arguments)"
        ) from e
    raise ClaudeCliError(f"could not start `{claude_bin()}`: {e}") from e
```

The second arm matters on its own: today ANY `OSError` from `subprocess.run` escapes
`claude_cli` unclassified. `spawn_turn` already wraps `OSError` into `ClaudeCliError`
(`claude_cli.py`) and gains the same errno branch, so a too-large worker brief on
either transport surfaces as `InputTooLargeError` and `worker_session._launch`'s existing
`except claude_cli.ClaudeCliError` (`worker_session.py`) fails the turn with a message
that says what happened.

The message must say the input was too large. It must NOT read as an outage: this string
reaches the user through `answer_reason` and the inbox.

### 5. `neo.drain_queue`: straight to unreachable, no retries, no verdict

In `drain_queue` (`neo.py`), a new arm BEFORE `except claude_cli.ClaudeCliError` —
ordering is the whole mechanism, exactly as it is for `UsageLimitError` above it:

```python
except claude_cli.InputTooLargeError as e:
    outcome = store.release_claim(q["id"], f"input too large: {e}", max_attempts=0)
    log.error("neo question %s could not be sent: %s", q["id"], e)
    if unreachable:
        unreachable(q, f"input too large: {e}")
    results.append({"question": q, "verdict": None, "outcome": outcome})
    continue
```

**The give-up mechanism is `release_claim`'s existing `max_attempts` parameter**
(`neo_store.release_claim`), and no new store method is needed. `release_claim` reads
`attempts` and takes the `failed` branch when `attempts >= max_attempts`; passing
`max_attempts=0` makes that true at the first attempt, writes
`status='failed'`, `claimed_at=NULL` and
`answer_reason = UNREACHABLE_PREFIX + detail + " (after 0 retries — nobody has judged
this)"`, and returns `"unreachable"`. `UNREACHABLE_PREFIX` is what every surface keys on
to tell "Neo was never reached" from "Neo could not settle it"
(`neo_store.py`, read by `ops._unreachable_asks`,
`daemon.Daemon._note_stranded_unreachable`, `cli.cmd_neo`), so this
outcome renders correctly on `jarvis neo list`, the dashboard and the attention list with
no further change.

Do NOT add a `give_up` method, and do NOT set `attempts` to the ceiling first: both
re-express a decision `max_attempts` already expresses, and the second writes a false
history (three retries that never happened).

`outcome` is asserted, not assumed: `release_claim` returns `"unreachable"` for a missing
row too, and the `unreachable` hook fires on both, which is correct — there is nothing to
retry in either case.

**Still no synthesised verdict.** The pinned ruling from
`docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md` §2 holds unchanged: no
`mark("failed")` with a fabricated `{escalate: True}`, no `deliver`. A call that never
happened is not an answer, and a deterministic refusal is even less of one. `unreachable`
is honest; a default verdict would be a decision nobody made.

### 6. The fake `claude` must learn the second door

`src/jarvis/testing.py`. kn-39a4dc03 is precisely this failure mode: a fake that parses
argv goes stale silently and makes one caller look like another. The existing
`system_prompt_seen` resolution (`testing.py`, in `FAKE_CLAUDE`'s `_write_call_record`)
is the precedent to follow — it exists because the system prompt already arrives by two
doors.

**Resolution rule, used identically by both dispatch branches and by the call record:**
the prompt is the token after `-p` (headless) or `argv[-1]` after a `--` fence (worker
turn) when argv carries it, and `sys.stdin.read()` when it does not. Precisely:

* worker-turn branch (`testing.py`'s `FAKE_CLAUDE`, `prompt = argv[-1]`): argv carries the
  prompt iff `"--" in argv`;
* headless branch (`testing.py`'s `FAKE_CLAUDE`, `prompt = argv[argv.index("-p") + 1]`):
  argv carries the prompt iff a token follows `-p` and it does not start with `-`.

Read stdin AT MOST ONCE, into a module-level cache, and ONLY when the guard above says
argv has no prompt — `claude --version` and `claude agents --json` must never read stdin,
or a fake invoked with an inherited terminal blocks for ever.

The read has to happen at ENTRY, because `_write_call_record` runs at entry
(`testing.py`) as well as at exit, and a record that resolved the prompt only at exit
would never arrive for a test using `hold_turns`.

**The call record gains a `"prompt"` key** carrying the resolved prompt whichever door it
came through, exactly as `system_prompt_seen` does. `"argv"` stays as it is — it is
verbatim evidence and ~30 test files read it. New and moved assertions read `c["prompt"]`;
existing ones keep working because the small case still travels in argv.

A prompt whose FIRST CHARACTER is `-` is misread by the headless rule. That hazard exists
today (the real CLI misparses it too) and is out of scope; note it in the fake's comment.

**Other readers of the recorded argv that recover a prompt** — all of these read a small
fixture prompt and therefore keep passing unchanged; they are listed so the implementer
can confirm rather than assume, and any that are moved to a large prompt must move to
`c["prompt"]`:
`tests/test_digest.py`, `tests/test_neo.py`, `tests/test_structured.py`,
`tests/test_validation_seats.py`, `tests/test_validation_panel.py`,
`tests/test_validation_loop.py`, `tests/test_health_sweep.py`,
`tests/test_pipeline.py`, `tests/test_question_diet.py`,
`tests/test_feature_orders.py`, `tests/test_feature_order_team.py`,
`tests/test_feature_agent.py`, `tests/test_analyst_dispatch.py`,
`tests/test_worker_spawn_args.py`, `tests/test_claude_cli.py`.
`testing.py`'s own `Handle.turns` reads the fake's per-session log, which is written from
the resolved `prompt` variable, so it needs no change once the branch resolves correctly.

**The fake `systemd-run`** (`testing.py`'s `FAKE_SYSTEMD_RUN`) parses `--unit`,
`--working-directory`, `StandardOutput=`, `StandardError=` and `--setenv`, and hardcodes
`stdin=subprocess.DEVNULL`. It must parse `--property=StandardInput=file:<path>` and open
that file as the child's stdin. Without it the transient-unit case cannot be tested at
all: the turn would launch with an empty brief and the fake would record it as such.

## Tests

`tests/test_claude_cli.py` unless stated. Every prompt-arrival assertion is
BYTE-IDENTICAL (`==`), never `in` — a truncated prompt is exactly the failure being
prevented.

1. **Headless, over the line.** `run_headless_result` with a prompt >131,072 bytes: the
   process launched (a call record exists), `"-p"` is in argv and is NOT followed by the
   prompt, and `record["prompt"] == prompt`.
2. **Headless, multibyte.** The same with a prompt built from non-ASCII text (and run
   with `LANG=C` monkeypatched into the environment), asserting byte-identical arrival.
   This is the `encoding="utf-8"` pin; without it the test fails with
   `UnicodeEncodeError`.
3. **Headless, under the line.** A small prompt still arrives as
   `argv[argv.index("-p") + 1]` — the no-change pin for the ~30 existing readers.
4. **Worker turn, direct Popen.** `spawn_turn` with a >131,072-byte prompt, no unit: the
   turn launched, `"--"` is absent from argv, `record["prompt"] == prompt`, and the
   `<seq>.prompt` file exists while the turn is held (`hold_turns`).
5. **Worker turn, transient unit.** The same through the fake `systemd-run`
   (`tests/test_turn_transport.py`): the unit's argv carries no prompt, the prefix carries
   `--property=StandardInput=file:…`, and the prompt arrives byte-identical.
6. **`turn_args` arity** (`tests/test_worker_spawn_args.py`): `prompt_via_stdin=True`
   emits neither `--` nor the prompt; the default still ends `["--", prompt]`.
7. **The prompt file is deleted by the reap**, and NOT before the turn is spawned: assert
   it exists after `spawn_turn` returns and is gone after `worker_session.poll` settles
   the turn.
8. **errno 7 maps to the new class** (unit): a `claude` stub, or a monkeypatched
   `subprocess.run` raising `OSError(errno.E2BIG, …)`, makes `_run` raise
   `InputTooLargeError`, which is a `ClaudeCliError`, whose message says the input was
   too large.
9. **`drain_queue` gives up at the first attempt** (`tests/test_neo.py` or
   `tests/test_transport_resilience.py`): an answerer raising `InputTooLargeError` leaves
   the question `status='failed'` with `attempts == 0`, `answer_reason` starting
   `UNREACHABLE_PREFIX` and containing `input too large`, the `unreachable` hook called
   ONCE, `deliver` NOT called, no `escalated` row, and the NEXT queued question in the
   same drain still answered (the loop `continue`s rather than aborting).

## Rejected alternatives

* **Truncate the prompt to fit.** Silently changes what Neo is asked. The bug becomes a
  wrong answer instead of a loud failure, which is strictly worse than today.
* **Always use stdin, drop the argv door.** Changes observed argv for every call in the
  fleet, including the fake's dispatch branches and ~30 test files, to fix a defect that
  exists only above 128 KB. `SYSTEM_PROMPT_ARGV_LIMIT` already made this call the other
  way for the same reason.
* **A `--prompt-file` flag.** There is no such flag on `claude -p`; stdin is the
  mechanism the CLI actually offers, and `spawn_turn`'s existing `stdin=DEVNULL` is proof
  it reads it.
* **`NamedTemporaryFile(delete=True)` for the worker turn.** Unlinked before the detached
  `claude` reads it; produces an empty brief that fails silently. Covered at §3.
* **Catch `OSError` in `drain_queue` instead of adding an exception class.** Puts
  transport knowledge in `neo`, and lumps errno 7 in with every transient `OSError`, so
  the "no retries" decision could not be made without re-inspecting errno at the wrong
  layer.
* **A new `NeoStore.give_up(qid, detail)`.** `release_claim(…, max_attempts=0)` already
  expresses it, by the same code path `reclaim_stale` agrees with. A second writer of the
  `failed`/`UNREACHABLE_PREFIX` row is a second thing to keep in step.
* **Cap the size of a Neo prompt / a dispatch brief.** A real conversation to have, and
  not this one: it is a policy about cost and attention, it would not fix the worker-turn
  site, and a cap that is ever raised re-opens exactly this bug.

## Out of scope

* Shrinking the 151.7K `auto_review` confirmation prompt that triggered this.
* `spawn_background` (`claude --bg`): a different transport with the same theoretical
  cliff, unreached and not exercised by the dispatch path being fixed. If it is fixed
  later it uses `PROMPT_ARGV_LIMIT` too.
* A prompt whose first character is `-` (§6).
* Any change to how many attempts a TRANSIENT Neo failure gets: `MAX_ANSWER_ATTEMPTS`,
  `RETRY_BACKOFF_SECONDS` and `STALE_ANSWERING_SECONDS` are untouched.
