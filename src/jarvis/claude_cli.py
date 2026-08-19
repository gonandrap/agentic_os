"""Thin wrapper around the `claude` CLI.

All interaction with Claude Code goes through here so tests can substitute a fake
`claude` executable (JARVIS_CLAUDE_BIN) and so a different backend can be swapped in.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


#: `claude` location override, mirroring bugreport's GH_BIN_ENV. Tests point it at a
#: fake; the test-isolation gate points it at a stub that refuses to run.
CLAUDE_BIN_ENV = "JARVIS_CLAUDE_BIN"


class ClaudeCliError(RuntimeError):
    pass


def claude_bin() -> str:
    return os.environ.get(CLAUDE_BIN_ENV, "claude")


def available() -> bool:
    return shutil.which(claude_bin()) is not None


def version() -> str:
    out = _run(["--version"], timeout=30)
    return out.strip()


def _run(args: list[str], cwd: Path | None = None, timeout: int = 120,
         env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            [claude_bin(), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ClaudeCliError(f"`{claude_bin()}` not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClaudeCliError(f"`claude {' '.join(args[:3])}...` timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise ClaudeCliError(
            f"claude {' '.join(args[:4])}... failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:500] or proc.stdout.strip()[:500]}"
        )
    return proc.stdout


# Claude Code's session-state vocabulary, as emitted by `claude agents --json` and by
# the supervisor's own `~/.claude/jobs/<id>/state.json` (verified against CLI 2.1.220).
# NOTE: there is no "running" here — that word belongs to Jarvis's *work order* status
# vocabulary (see project_store.WO_STATUSES). Conflating the two silently misreads every
# healthy worker as needing the user, so always go through the helpers below.
ACTIVE_STATES = frozenset({"working", "starting", "queued"})
BLOCKED_STATES = frozenset({"blocked"})
FINISHED_STATES = frozenset({"done", "failed", "cancelled"})


@dataclass
class BgSession:
    """A background session as reported by `claude agents --json`."""
    id: str
    session_id: str
    cwd: str
    name: str
    state: str  # working | blocked | done | failed | cancelled | ...
    kind: str = "background"
    started_at: float | None = None

    @property
    def is_active(self) -> bool:
        """The agent is making progress on its own — nothing is wanted from the user."""
        return self.state in ACTIVE_STATES

    @property
    def is_blocked(self) -> bool:
        """The agent stopped mid-turn on a permission prompt or a question."""
        return self.state in BLOCKED_STATES

    @property
    def is_finished(self) -> bool:
        """The turn ended, whether it succeeded, failed or was cancelled."""
        return self.state in FINISHED_STATES


def list_background_sessions(cwd: Path | None = None, include_done: bool = True,
                             timeout: int = 60) -> list[BgSession]:
    """The Claude sessions the user has open. `timeout` is short for interactive callers
    (a web request must not hang on a slow CLI) and generous for the daemon."""
    args = ["agents", "--json"]
    if include_done:
        args.append("--all")
    if cwd is not None:
        args += ["--cwd", str(cwd)]
    out = _run(args, timeout=timeout)
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"unparseable `claude agents --json` output: {out[:200]}") from e
    sessions = []
    for item in data:
        sessions.append(
            BgSession(
                id=item.get("id", ""),
                session_id=item.get("sessionId", ""),
                cwd=item.get("cwd", ""),
                name=item.get("name", ""),
                state=item.get("state", "unknown"),
                kind=item.get("kind", "background"),
                started_at=item.get("startedAt"),
            )
        )
    return sessions


_JOB_ID_RE = re.compile(r"claude stop ([0-9a-f]{6,})")


def _briefing_args(
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    settings_file: Path | None = None,
    add_dirs: list[Path] | None = None,
    autocompact_window: int | None = None,
) -> list[str]:
    """The flags that constitute a worker's briefing, in one place.

    A resumed session RE-DERIVES its system prompt, model, effort, permission mode
    and reachable directories from the argv it is launched with — it does not
    inherit them from the transcript. So every path that starts a worker turn
    (initial dispatch, bg resume-fork, headless resume) must pass the same set, or
    the worker silently loses the project's standing instructions and the OS skills
    from that turn onwards. Shared here so a new launch path cannot forget one.

    `autocompact_window` is here for that same re-derivation reason and it is the
    load-bearing case: it caps how large the conversation may grow, so a turn that
    omitted it would let the context past the bound and every later turn would carry
    the excess for the rest of the work order's life. None means "no flag", which is
    the CLI's own behaviour — the model's full window, no early compaction.

    Callers must not append a bare positional after these: `--add-dir` is variadic
    and would swallow it (fence with `--` first, as spawn_background does).
    """
    args: list[str] = []
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    if append_system_prompt:
        args += ["--append-system-prompt", append_system_prompt]
    if settings_file:
        args += ["--settings", str(settings_file)]
    if autocompact_window:
        # `--autocompact <auto|tokens>`, single-valued. The CLI parses a bare integer
        # and rejects anything outside 100k-1M; `catalog._parse_autocompact` enforces
        # the same range up front so a bad catalog fails at boot rather than on the
        # first dispatch.
        args += ["--autocompact", str(autocompact_window)]
    for d in add_dirs or []:
        args += ["--add-dir", str(d)]
    return args


def spawn_background(
    prompt: str,
    cwd: Path,
    name: str,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    worktree: str | None = None,
    settings_file: Path | None = None,
    resume_session_id: str | None = None,
    add_dirs: list[Path] | None = None,
    autocompact_window: int | None = None,
) -> str | None:
    """Spawn a native Claude Code background session; returns the job id if the
    CLI reported one.

    The supervisor daemon assigns the session id (a --session-id flag is ignored for
    --bg dispatches — verified empirically), so the work order is bound to its session
    afterwards: the SessionStart hook reports the real id, and the reconciler falls
    back to matching the unique `[WO <id>]` name.

    With resume_session_id, the new background agent continues that conversation
    (fork semantics: full context carried over, fresh session id — verified live).
    This is how user feedback is delivered while keeping the worker visible in the
    agents view.

    add_dirs are extra directories the session may reach; Claude also loads skills from
    each `<dir>/.claude/skills/`, which is how the OS ships its own skills to a worker
    whose worktree contains only tracked files.

    settings_file carries the FULL settings for the worker (OS-injected project
    settings merged with per-work-order env like JARVIS_WO_ID). It must be passed
    explicitly: the worker runs in a fresh git worktree, and the project's
    .claude/settings.json — being deliberately untracked — does not exist there.
    """
    args: list[str] = ["--bg", "--name", name]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if worktree:
        args += ["--worktree", worktree]
    args += _briefing_args(model, effort, permission_mode, append_system_prompt,
                           settings_file, add_dirs, autocompact_window)
    # `--` fences the prompt off from option parsing. Without it a variadic option
    # (`--add-dir <directories...>` is one) keeps consuming positionals and swallows
    # the prompt as a directory: the session boots with nothing to do and parks at
    # the welcome screen forever, which reads as "created but never started".
    # It also lets a prompt begin with a dash. Never append anything after this.
    args.append("--")
    args.append(prompt)
    out = _run(args, cwd=cwd, timeout=120)
    m = _JOB_ID_RE.search(out or "")
    return m.group(1) if m else None


def stop_session(bg_id: str) -> bool:
    """Release a background session from the supervisor (`claude stop <id>`).

    Required before a headless resume: a session still owned by a live bg agent
    refuses `--resume` (verified live). Safe on already-stopped sessions.
    """
    try:
        _run(["stop", bg_id], timeout=30)
        return True
    except ClaudeCliError:
        return False


# -- worker turns (the transport every worker conversation runs on) --------------------


@dataclass
class TurnResult:
    """The parsed outcome of one headless turn (`claude -p --output-format json`)."""

    ok: bool
    result: str = ""
    session_id: str = ""
    error: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    subtype: str = ""
    #: Why the CLI's query loop stopped (`terminal_reason`), verbatim. One of a closed
    #: set the CLI defines — `api_error`, `aborted_streaming`, `aborted_tools`,
    #: `prompt_too_long`, `max_turns`, `completed`, … — and the first half of the
    #: structured evidence `transient_failure` classifies a retriable turn from.
    #: "" when the envelope carried none (a very old outfile, or a bypassed loop).
    terminal_reason: str = ""
    #: The HTTP status of the API error, when the failure was one. The CLI's own schema
    #: documents it as "HTTP status code of the API error", and it is the other half of
    #: that evidence: 500+ is the transport, 429 is the usage window.
    api_error_status: int | None = None
    #: Compact usage envelope derived from the result JSON (see `derive_turn_usage`).
    #: None when the CLI emitted no `usage` object — old outfiles, crashed turns.
    usage: dict[str, Any] | None = None


#: Which reading of a result envelope produced a stored `usage_json`. Bumped when the
#: MEANING of the numbers changes, so a persisted row can be re-derived rather than
#: silently compared against rows counted a different way.
#:
#: 1 — token totals taken from the envelope's top-level `usage` object.
#: 2 — token totals taken from `modelUsage`, which is what the turn actually spent.
USAGE_SCHEMA_VERSION = 2


def derive_turn_usage(data: dict[str, Any]) -> dict[str, Any] | None:
    """The turn's exact accounting, compacted from one result envelope.

    The raw JSON on disk (`<project>/.jarvis/turns/<wo>/<seq>.json`) stays the source
    of truth; this dict is what `wo_turns.usage_json` persists so the record outlives
    the file. Exact by construction — every number is the CLI's own, never a list-price
    estimate:

    * token totals come from `modelUsage`, summed across the models the turn used, and
      `by_model` keeps them split (a turn that fell back to a cheaper model is a fact
      about the bill, not noise);
    * `iterations` carries one entry per API call, so the context at a call is that
      iteration's input + cache_read + cache_creation — `context_peak` is the max,
      which is the /context statistic, available headlessly;
    * the ephemeral 1h/5m split comes from `usage.cache_creation`, the only place it is
      reported at all;
    * `context_window` and per-model `costUSD` come from `modelUsage`.

    WHY `modelUsage` AND NOT `usage`. The top-level `usage` object is NOT the turn's
    total — measured over the 186 live result files that carry both, it runs at 33-60%
    of `modelUsage`, which in turn agrees to the TOKEN with the transcript that
    `usage.read_session` derives independently, and whose `costUSD` sums to
    `total_cost_usd` to the cent. Two independent sources agreeing settled it (Neo,
    question 121 on wo-4576667e). Dollars were never wrong — they come from
    `total_cost_usd` — so what this corrects is every token figure the OS has recorded.

    THE 1h/5m SPLIT IS A SAMPLE, not a partition of `cache_write`: it is reported for
    whatever part of the turn the top-level `usage` speaks for, and `cache_1h + cache_5m`
    is therefore usually less than `cache_write`. `usage.write_rate` uses its RATIO for
    exactly that reason, and the two must not be presented as adding up to the whole.

    Returns None when there is neither a `modelUsage` block nor a `usage` object (a
    result written before the CLI carried either, or a crashed turn) — an absence,
    never a zero. When only `usage` is present the totals fall back to it and the
    envelope is stamped version 1, which is the honest label for those numbers.
    """
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    models = {name: mu for name, mu in (data.get("modelUsage") or {}).items()
              if isinstance(mu, dict)}
    if not usage and not models:
        return None
    cache_creation = usage.get("cache_creation") or {}
    iterations = [i for i in (usage.get("iterations") or []) if isinstance(i, dict)]
    contexts = [
        (i.get("input_tokens") or 0)
        + (i.get("cache_read_input_tokens") or 0)
        + (i.get("cache_creation_input_tokens") or 0)
        for i in iterations
    ] or [
        # No per-call breakdown: the whole-turn totals still bound the context.
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    ]
    windows = [mu["contextWindow"] for mu in models.values() if mu.get("contextWindow")]
    by_model = [
        {
            "model": name,
            "input": mu.get("inputTokens") or 0,
            "cache_write": mu.get("cacheCreationInputTokens") or 0,
            "cache_read": mu.get("cacheReadInputTokens") or 0,
            "output": mu.get("outputTokens") or 0,
            "cost_usd": mu.get("costUSD"),
            "context_window": mu.get("contextWindow"),
        }
        for name, mu in models.items()
    ]
    if by_model:
        totals = {c: sum(m[c] for m in by_model)
                  for c in ("input", "cache_write", "cache_read", "output")}
        version = USAGE_SCHEMA_VERSION
    else:
        totals = {
            "input": usage.get("input_tokens") or 0,
            "cache_write": usage.get("cache_creation_input_tokens") or 0,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
            "output": usage.get("output_tokens") or 0,
        }
        version = 1
    return {
        "usage_v": version,
        "total_cost_usd": data.get("total_cost_usd"),
        **totals,
        "cache_1h": cache_creation.get("ephemeral_1h_input_tokens") or 0,
        "cache_5m": cache_creation.get("ephemeral_5m_input_tokens") or 0,
        "api_calls": len(iterations) or data.get("num_turns"),
        "context_peak": max(contexts),
        "context_window": max(windows) if windows else None,
        "duration_api_ms": data.get("duration_api_ms"),
        "cost_by_model": {name: mu["costUSD"] for name, mu in models.items()
                          if mu.get("costUSD") is not None},
        "by_model": by_model,
    }


def turn_args(
    prompt: str,
    session_id: str,
    resume: bool,
    name: str | None = None,
    worktree: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    settings_file: Path | None = None,
    add_dirs: list[Path] | None = None,
    autocompact_window: int | None = None,
) -> list[str]:
    """argv for one worker turn. Split out from `spawn_turn` so tests can assert on it.

    `--session-id` on the opening turn, `--resume` on every one after. Both name the
    SAME id: unlike `--bg --resume` (which forks the conversation under a supervisor-
    assigned id), a headless resume reuses the id it is given — `--fork-session` exists
    to opt into the other behaviour. That is what lets Jarvis mint the id up front and
    treat it as immutable for the work order's lifetime.
    """
    args = ["-p", "--output-format", "json",
            "--resume" if resume else "--session-id", session_id]
    if name:
        args += ["-n", name]
    if worktree:
        args += ["--worktree", worktree]
    args += _briefing_args(model, effort, permission_mode, append_system_prompt,
                           settings_file, add_dirs, autocompact_window)
    # Same fence, same reason as `spawn_background`: `--add-dir` and `--tools` are both
    # variadic and will eat the prompt as an option value if it arrives bare. Nothing
    # may be appended after this.
    args += ["--", prompt]
    return args


def spawn_turn(prompt: str, cwd: Path, session_id: str, outfile: Path,
               errfile: Path, resume: bool = False, **kwargs: Any) -> int:
    """Start one worker turn as a detached process; returns its pid.

    Detached (`start_new_session=True`) on purpose: a turn can run for hours and
    `shipit` restarts jarvisd on every release, so a turn parented to the daemon would
    lose its reply on each deploy. Its own process group is also what makes `cancel()`
    able to take the whole tree down.

    stdin is /dev/null because `claude -p` otherwise spends three seconds waiting for
    input that is never coming, on every turn.
    """
    outfile.parent.mkdir(parents=True, exist_ok=True)
    args = turn_args(prompt, session_id, resume, **kwargs)
    try:
        with outfile.open("w") as out, errfile.open("w") as err:
            proc = subprocess.Popen(
                [claude_bin(), *args],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
    except (FileNotFoundError, OSError) as e:
        raise ClaudeCliError(f"could not start `{claude_bin()}`: {e}") from e
    return proc.pid


def read_turn_result(outfile: Path, errfile: Path | None = None) -> TurnResult | None:
    """Parse a finished turn's output. None means "nothing usable there (yet)".

    The caller decides what None means: still running (the process is alive) or a
    turn that died without saying anything (it is not).
    """
    try:
        raw = outfile.read_text()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    stderr_tail = ""
    if errfile is not None:
        try:
            stderr_tail = errfile.read_text().strip()[-1000:]
        except OSError:
            stderr_tail = ""
    ok = not data.get("is_error")
    return TurnResult(
        ok=ok,
        result=data.get("result") or "",
        session_id=data.get("session_id") or "",
        error="" if ok else (data.get("result") or stderr_tail or "turn reported is_error"),
        cost_usd=data.get("total_cost_usd"),
        num_turns=data.get("num_turns"),
        subtype=data.get("subtype") or "",
        terminal_reason=data.get("terminal_reason") or "",
        api_error_status=_int_or_none(data.get("api_error_status")),
        # Derived on the failed path too: the usage rides on a failed result JSON just
        # the same (a 429'd turn has already paid for everything up to the refusal).
        usage=derive_turn_usage(data),
    )


def _int_or_none(value: Any) -> int | None:
    """`api_error_status` is declared nullable AND optional, so absent, null and a
    number are all legal. Anything else is a shape this parser does not know."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# -- the usage limit ----------------------------------------------------------------
#
# Claude Code refuses a turn outright once the account's usage window is spent, and it
# says so *in the turn's own result* rather than on stderr. The refusal is free and
# instant — `duration_api_ms: 0`, `total_cost_usd: 0` — so from the outside it is
# indistinguishable from any other `is_error` turn, and Jarvis used to fail the work
# order and wait for a human to come back after midnight and retry it by hand.
#
# The message carries the one fact needed to recover unattended: WHEN the window
# reopens. Parsing it here, deterministically and with no LLM anywhere, is what lets
# `worker_session.turn_pause` and `Daemon.retry_paused_turns` resume the conversation by
# themselves. Observed live on wo-2fa7c0e9 turn 4:
#
#     You've hit your session limit · resets 11:50pm (America/Los_Angeles)
#
# THE MESSAGE IS ASSEMBLED, NOT WRITTEN, and matching the assembly is what makes this
# survive the next model launch. Read out of the CLI's own string table (2.1.226): the
# body is `You've hit your ${label}${suffix}`, the label comes from ONE table keyed by
# rate-limit type, and the suffix is ` · resets ${when}`.
#
#     five_hour                   session limit        <- the 5-hour window
#     seven_day                   weekly limit         <- the 7-day window
#     seven_day_opus              Opus limit
#     seven_day_sonnet            Sonnet limit
#     seven_day_overage_included  Fable 5 limit        <- per-MODEL, and it is named
#     overage                     usage credit limit
#
# So the label carries a MODEL NAME that changes with the fleet, and enumerating the
# words in it is a guarantee of missing the next one — this parser did miss "Fable 5
# limit" and "usage credit limit" until the user asked. What does not change is the
# shape: the word "limit", then "resets", then a moment. That pair is what is matched.
#
# The suffix is also the reason a SPEND cap is deliberately left alone. Those render
# ` · run /usage-credits to raise it` instead of a reset, so they never match, and they
# should not: a spend cap does not reopen on its own and the user has to act.
#
# The moment itself has three renderings, all three handled (`_reset_moment`), because
# the CLI picks between them by how far away the reset is:
#
#     under 24h    resets 11:50pm (America/Los_Angeles)      <- the 5-hour case
#     over 24h     resets Aug 14, 9:50am (America/Los_Angeles)  <- typically the 7-day
#     new year     resets Dec 31, 2027, 11:50pm (…)
#     relative     resets in 2h 15m                          <- fast mode


@dataclass(frozen=True)
class UsageLimit:
    """A turn Claude Code refused because the usage window is spent.

    `reset_at` is the epoch moment the window reopens, or None when the message named a
    reset but no time this parser could read — the caller then falls back to a fixed
    delay rather than guessing a moment.
    """

    message: str
    reset_at: float | None


#: The legacy shape, and the only one that states the moment unambiguously:
#: "Claude AI usage limit reached|1786344785". Kept as a belt-and-braces branch, NOT
#: verified: that literal is absent from 2.1.226's string table (the only
#: "usage limit reached" in there is inside Claude Code's own auth-error classifier),
#: so it is either gone or composed somewhere strings cannot see. It costs one regex and
#: it is the shape older tooling keys on, so removing it would only lose coverage.
_LIMIT_EPOCH_RE = re.compile(r"usage limit reached\s*\|\s*(\d{10,13})", re.IGNORECASE)

#: A spent window, matched by SHAPE rather than by wording: the word "limit" and then a
#: reset, close together. Matching the label instead would mean listing the model names
#: it can contain ("Fable 5 limit", "Opus limit", …) and silently missing whichever one
#: ships next — see the table above.
#:
#: The proximity is what keeps this honest. It runs against the error text of EVERY
#: failed turn, which can be a tail of the worker's own stderr, and a false positive
#: would retry a genuinely broken turn until the cap stops it. Prose mentioning a limit
#: does not go on to name a reset time sixty characters later; this message always does.
_LIMIT_RE = re.compile(r"\blimits?\b.{0,60}?\bresets?\b", re.IGNORECASE | re.DOTALL)

#: "resets in 2h 15m" — the relative rendering (fast mode). The easiest of the three:
#: no timezone, no calendar, nothing to resolve against a clock that might disagree.
_RESET_IN_RE = re.compile(
    r"""resets?\s+in\s+
        (?:(?P<days>\d+)\s*d\b\s*)?
        (?:(?P<hours>\d+)\s*h\b\s*)?
        (?:(?P<minutes>\d+)\s*m\b\s*)?
        (?:(?P<seconds>\d+)\s*s\b)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: The absolute renderings: "resets 11:50pm (America/Los_Angeles)" under 24h,
#: "resets Aug 14, 9:50am (…)" over it, and the same with a year when the reset falls in
#: the next one. Every part before the hour is optional because the CLI drops whichever
#: ones it judges obvious, and the minutes go too when they are zero ("resets 3pm").
_RESET_RE = re.compile(
    r"""resets?\s+(?:at\s+)?
        (?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s*,?\s*
           (?:(?P<year>\d{4})\s*,?\s*)?(?:at\s+)?)?
        (?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*
        (?P<ampm>[ap]\.?m\.?)?
        (?:\s*\((?P<tz>[^)]{1,64})\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def usage_limit(text: str | None, *, now: float | None = None) -> UsageLimit | None:
    """Read a failed turn's error as a usage-limit refusal, or None if it is not one.

    None is the answer for every ordinary failure, so callers can use this as the
    predicate itself. A message that names a limit but no reset at all is also None:
    prose about limits (a worker's own log line, a stderr tail) mentions the word far
    more often than the CLI refuses a turn, and "resets …" is what tells the two apart.
    """
    if not text:
        return None
    epoch = _LIMIT_EPOCH_RE.search(text)
    if epoch:
        raw = int(epoch.group(1))
        # 13 digits is milliseconds, 10 is seconds; both shapes have been seen.
        return UsageLimit(message=_summarise(text),
                          reset_at=float(raw / 1000 if raw > 1e11 else raw))
    if not _LIMIT_RE.search(text):
        return None
    ref = time.time() if now is None else now
    # The relative form first: "resets in 2h 15m" needs no timezone and no calendar, so
    # where it is offered it is the more trustworthy of the two.
    relative = _RESET_IN_RE.search(text)
    if relative and any(relative.group(g) for g in
                        ("days", "hours", "minutes", "seconds")):
        return UsageLimit(message=_summarise(text),
                          reset_at=ref + _duration(relative))
    reset = _RESET_RE.search(text)
    if reset is None:
        return None
    return UsageLimit(message=_summarise(text), reset_at=_reset_moment(reset, ref))


def _duration(m: re.Match[str]) -> float:
    """Seconds in a "2d 3h 15m" clause. Absent units are zero, never an error: the CLI
    drops the ones that would read as zero rather than printing them."""
    return (int(m.group("days") or 0) * 86400
            + int(m.group("hours") or 0) * 3600
            + int(m.group("minutes") or 0) * 60
            + int(m.group("seconds") or 0))


def _summarise(text: str) -> str:
    return " ".join(text.split())[:200]


def _reset_moment(m: re.Match[str], now: float) -> float | None:
    """The epoch moment a "resets …" clause names, or None if it does not name one.

    The clause gives a wall-clock time in some timezone and, usually, no date at all, so
    the moment is resolved against `now`: the next occurrence of that clock time, in that
    zone. That is exactly the rule a human applies reading it, and it is why an
    unparseable zone falls back to the daemon's own clock rather than to UTC.

    A date appears once the reset is more than 24h away, which is the ordinary case for
    the 7-day window, and a YEAR appears with it only when the reset falls in the next
    one. The year is read rather than skipped because skipping it does not fail loudly:
    "Aug 14, 2027, 9:50am" would otherwise parse its year as the hour and quietly
    schedule a retry for 8pm tonight.
    """
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").replace(".", "").lower()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23:
        return None
    ref = datetime.fromtimestamp(now, _zone(m.group("tz")))
    month = _MONTHS.get((m.group("month") or "")[:3].lower())
    day = int(m.group("day") or 0)
    if month and day:
        year = int(m.group("year") or 0)
        try:
            when = ref.replace(year=year or ref.year, month=month, day=day, hour=hour,
                               minute=minute, second=0, microsecond=0)
        except ValueError:
            return None  # e.g. "Feb 30"
        # A date with no year is next year's when it has already gone past — the
        # December-to-January case, and the only one worth handling. When the message
        # states the year there is nothing to infer, so this must not fire.
        if not year and when < ref - timedelta(days=180):
            try:
                when = when.replace(year=when.year + 1)
            except ValueError:
                return None  # Feb 29 into a non-leap year
    else:
        when = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= ref:
            when += timedelta(days=1)  # "resets 3am" said at 4pm means tomorrow
    return when.timestamp()


def _zone(name: str | None) -> tzinfo | None:
    """The named timezone, or the machine's own if it is missing or unknown.

    Falling back to local rather than UTC is the conservative choice: the daemon runs on
    the same machine the user reads the clock on, so a mis-parse lands minutes or hours
    out instead of half a day, and the retry floor absorbs the rest.
    """
    if name:
        try:
            return ZoneInfo(name.strip())
        except (KeyError, ValueError, OSError):
            pass
    return datetime.now().astimezone().tzinfo


# -- the transport failing under us ---------------------------------------------------
#
# The other way a turn dies without the work being wrong: the API was busy or broken.
# `wo-4f460495` turn 2 hit a 500 five hours into a work order, and because nothing in
# the OS recognised it the order sat in `failed` for TWENTY-SEVEN HOURS until the user
# came back and typed "retry" by hand. Claude's models get busy; an OS that needs a
# human for that is not self-healing.
#
# THIS ONE IS STRUCTURED, WHICH THE USAGE LIMIT WAS NOT. The result envelope carries two
# fields the OS used to drop on the floor, both verified live on that very turn and both
# in the CLI's own published result schema:
#
#     terminal_reason: "api_error"        # why the query loop stopped
#     api_error_status: 500               # "HTTP status code of the API error"
#
# So there is no prose to parse. The usage-limit matcher above has to key on the SHAPE
# of an assembled English sentence because the CLI states the reset nowhere else; here
# the CLI hands over a status code, and matching an integer beats matching a sentence in
# every way that matters — no model name to rot, no wording to change, no false positive
# from a worker's own log line quoting an error it read.
#
# WHY 500-AND-UP AND NOTHING ELSE (Neo, question 126). Every failed turn the live fleet
# has ever recorded — ten of them — is one of exactly three shapes:
#
#     api_error + 429                          6   the usage window; `usage_limit` owns it
#     api_error + 500                          1   this, and it stalled for 27h
#     aborted_streaming / aborted_tools        3   no status at all
#
# 429 is deliberately NOT retried here. It is the usage limit's own code, and the branch
# above already handles the recoverable form of it; the form it declines to touch is a
# SPEND cap, which never reopens on its own. Sending 429 down this path would auto-retry
# a cap that needs the user, which is precisely the mistake `usage_limit` was written to
# avoid.
#
# The aborts are excluded for a different reason: they land mid-tool-use, so the turn may
# have half-run a side effect, and replaying that is worse than asking. They are also the
# one class the CLI's own telemetry does not count as an error (`m3n` returns false for
# both), which is a hint they are not simply "the API broke".
#
# THE TEXT BRANCH IS A FALLBACK, NOT THE MECHANISM. Two cases reach us with no status:
# a turn reaped before these columns existed, and the CLI's own status-less transient
# wordings — it assembles those from a fixed table, so they are matched as literals:
#
#     API Error: Connection to the API was lost (ECONNRESET). This is usually temporary…
#     API Error: Repeated 529 Overloaded errors. The API is at capacity …
#
# Both are prefixed `API Error: `, which is what keeps prose that merely mentions one of
# them (a worker's stderr, this very comment quoted into a log) from matching.


#: The transient wordings the CLI assembles when the failure carried no HTTP status,
#: read out of 2.1.235's string table. Anchored on the `API Error: ` prefix the CLI puts
#: in front of every one of them, so only the CLI's own voice matches.
_TRANSIENT_TEXT_RE = re.compile(
    r"API Error:.{0,200}?(?:"
    r"Connection to the API was lost"
    r"|The API is at capacity"
    r"|server-side issue, usually temporary"
    r")",
    re.IGNORECASE | re.DOTALL,
)

#: The status the CLI names in the message body when the envelope's own field is missing
#: — "API Error: 500 Internal server error". Only 5xx: see the comment above on 429.
_TRANSIENT_STATUS_RE = re.compile(r"API Error:\s*(5\d\d)\b", re.IGNORECASE)


@dataclass(frozen=True)
class TransientFailure:
    """A turn the transport lost, not one the work broke.

    `status` is the HTTP status when there was one, and None for the connection-lost and
    at-capacity shapes, which name no code. Nothing decides anything from it — it is
    what the timeline shows the user so "retrying" is a claim they can check.
    """

    message: str
    status: int | None


def transient_failure(text: str | None, *, terminal_reason: str | None = None,
                      api_error_status: int | None = None) -> TransientFailure | None:
    """Read a failed turn as a transient transport failure, or None if it is not one.

    None for every ordinary failure, so callers use it as the predicate itself — and
    None for a usage-limit refusal too, which `usage_limit` owns end to end. The two are
    checked in that order everywhere, and this function re-checks it rather than trusting
    the caller: a 429 that reached here would otherwise auto-retry a spend cap.

    Structured evidence wins whenever the envelope carried any. Only when it carried
    none does the text get a look, which is what stops a worker's stderr quoting an
    error message from parking its own work order on a retry loop.
    """
    if not text:
        return None
    if usage_limit(text) is not None:
        return None  # the window, not the transport — the branch above owns it
    if api_error_status is not None:
        # The CLI said what happened. Believe it and read nothing into the prose.
        if api_error_status < 500:
            return None
        return TransientFailure(message=_summarise(text), status=api_error_status)
    if terminal_reason and terminal_reason != "api_error":
        # It stopped for a reason it named, and that reason was not the API: an abort,
        # a prompt too long, the turn cap. None of those get better by being repeated.
        return None
    named = _TRANSIENT_STATUS_RE.search(text)
    if named:
        return TransientFailure(message=_summarise(text), status=int(named.group(1)))
    if _TRANSIENT_TEXT_RE.search(text):
        return TransientFailure(message=_summarise(text), status=None)
    return None


def process_alive(pid: int | None) -> bool:
    """Is this turn's process still running?

    Three cases, in the order they are asked, because no single check covers them all:

    1. **Still our child** (the usual case — same daemon that launched it).
       `waitpid(WNOHANG)` is authoritative *and* reaps it. `os.kill(pid, 0)` is not
       enough here: a detached child nobody waits on becomes a **zombie**, and signalling
       a zombie succeeds, so a finished turn would read as running forever.
    2. **Ours, but mid-launch.** `waitpid` returns 0 for a child that has forked but not
       yet exec'd, which is correct. Inspecting `/proc` instead would not be: in that
       window the cmdline is still the *parent's*, so a cmdline test declares a
       just-launched turn dead and reaps it before it has written anything. That is
       exactly what CI caught.
    3. **No longer our child** — jarvisd restarted, so the turn was reparented to init
       (which reaps it, making `kill` honest again). Only here is the `/proc` cmdline
       consulted, purely as a pid-reuse guard: a turn can run for hours, and a recycled
       pid read as "still running" would hang its work order forever. Matching is on a
       whole path component, never a substring — `claude` appears in plenty of unrelated
       command lines, this suite's own `.claude/worktrees/*/.venv/bin/python` included.
    """
    if not pid:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        return reaped != pid  # 0 → still running; pid → it just exited (now reaped)
    except ChildProcessError:
        pass  # not (or no longer) our child — fall through to the signal check
    except (OSError, ValueError):
        pass
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True  # no /proc (or it vanished mid-read) — trust the signal
    want = Path(claude_bin()).name
    return any(Path(tok).name == want
               for tok in raw.decode(errors="replace").split("\0") if tok)


def kill_process_group(pid: int | None) -> bool:
    """Terminate a turn and everything it spawned. False if it was already gone."""
    if not pid:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False


@dataclass
class HeadlessResult:
    """One `claude -p` one-shot call: what it said, and what it cost.

    `usage` is the SAME envelope `derive_turn_usage` compacts out of a worker turn's
    result JSON, so the OS's own calls (Neo, the panel's seats, the digest) and its
    workers' turns are accounted in one shape and can be added up together. None when
    the CLI emitted no usage object — an old CLI, or output that did not parse as JSON
    at all — which is an absence, never a zero.
    """

    text: str
    usage: dict[str, Any] | None = None
    session_id: str = ""
    #: The model that actually served the call, read back from `modelUsage` where the
    #: CLI reports it and falling back to the one that was asked for. Recorded because
    #: the caller's `--model` may be an alias, and an OS call priced against the wrong
    #: family is a wrong number in the only report that says what Jarvis costs.
    model: str = ""


def _attribute_subprocess(result: HeadlessResult, record: Any = None) -> None:
    """Record a headless call made from inside a work order's process tree.

    A worker's environment carries `JARVIS_WO_ID` (`dispatch._write_worker_settings`),
    and a `--settings` env block reaches the CLI's own `process.env`, not just its
    children — so every subprocess the worker spawns inherits it, however deep. Its
    presence here therefore means one thing: this call is being made BENEATH a worker,
    and its tokens are that work order's cost.

    Nothing else can establish that. The call gets a session id Jarvis never minted, a
    transcript under whatever cwd it ran in, and no mention of a work order anywhere in
    it — the same dead end that forced the OS's own calls to be recorded at the moment
    they return (`agent_usage`). Recorded here, on the one seam every Jarvis-transport
    call passes through, rather than at each caller: the callers are eval suites and
    scripts that have no idea they are inside a work order.

    `ok` follows whether an envelope came back at all: a call the CLI answered with
    something unparseable was paid for just the same, and a zero-token row that says so
    is a different fact from no row.
    """
    wo_id = os.environ.get("JARVIS_WO_ID", "").strip()
    if not wo_id:
        return
    from . import agent_usage

    # What RAN the call, not where it ran. An eval suite's cwd is a fresh pytest tmp dir
    # per scenario, so labelling by directory yields forty labels naming nothing; the
    # program name groups the forty into the one line a reader wants ("pytest: 44 calls").
    label = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    (record or agent_usage.record)(
        agent_usage.WORKER_SUBPROCESS, usage=result.usage,
        project=os.environ.get("JARVIS_PROJECT", ""), wo_id=wo_id,
        label=label or "claude -p", model=model_of(result), ok=bool(result.usage))


def model_of(result: HeadlessResult) -> str:
    """The model to price a headless call at. `"unknown"` when it was never captured.

    Never `""`: an empty model prices at ZERO in `usage.price_for` (that branch is for
    `<synthetic>`, which was never billed), so a real call whose model went unrecorded
    would silently cost nothing — the exact bug this accounting exists to fix.
    """
    return result.model or "unknown"


def run_headless_result(prompt: str, system_prompt: str | None = None,
                        model: str | None = None, cwd: Path | None = None,
                        timeout: int = 300, tools: str | None = None,
                        attribute: bool = True, record: Any = None,
                        permission_mode: str | None = None,
                        env_extra: dict[str, str] | None = None) -> HeadlessResult:
    """One-shot headless call (`claude -p`), with its accounting kept.

    The transport every OS-side agent runs on — Neo, the panel's seats, the dashboard
    digest. Same call `run_headless` makes; the difference is that the usage object the
    CLI puts in its own result JSON is READ rather than thrown away. Jarvis spends real
    tokens answering its workers' questions, and a spend nobody records is a spend
    nobody can see (see `agent_usage`, which is where these envelopes are persisted).

    Used by Neo: the system prompt (persona + learnings) is byte-stable across
    calls, so consecutive invocations within the Anthropic cache TTL share a
    cached prefix — question-specific content rides in `prompt`, after it.

    `tools` selects the callee's built-in tool set: `None` (default) leaves it
    alone, `""` strips every tool, or a comma-separated list ("Read,Bash").
    Strip them when the answer must come from the prompt rather than from the
    machine — a tooled callee will happily go read the real state and answer
    about *that*. Note this is availability, not permission: `--allowedTools`
    and `--disallowedTools` do not remove a tool, and under
    `permissions.defaultMode: auto` they do not stop it being used either.

    `attribute` is the accounting for calls made from INSIDE a work order — see
    `_attribute_subprocess`. It defaults ON because the callers that need it are eval
    suites and scripts that do not know they are inside one; the OS's OWN call sites
    (Neo, the panel's seats, the digest) switch it OFF because they record themselves,
    with the work order and the question they were made for, which this seam cannot know.

    `permission_mode` and `env_extra` exist for the other direction: running a
    subject that is *supposed* to touch the machine, in a controlled one. A
    headless callee cannot answer a permission prompt any more than a `--bg`
    worker can, so a tooled call needs the same `auto` mode dispatch gives real
    workers; `env_extra` is how the sandbox gets on its PATH. Note it does NOT
    suppress attribution: a measured subject still spends the work order's tokens,
    and `env_extra` overriding `PATH` leaves `JARVIS_WO_ID` untouched.
    """
    args: list[str] = ["-p", prompt, "--output-format", "json"]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    if model:
        args += ["--model", model]
    if tools is not None:  # "" is meaningful: it disables every tool
        args += ["--tools", tools]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    out = _run(args, cwd=cwd, timeout=timeout, env_extra=env_extra)
    data: Any = None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        pass
    if not isinstance(data, dict):
        # Not JSON at all, or not an object: the text is still the answer (that is what
        # `run_headless` has always returned here), and there is nothing to account.
        result = HeadlessResult(text=out, model=model or "")
    else:
        served = [name for name in (data.get("modelUsage") or {})]
        result = HeadlessResult(
            text=data.get("result", ""),
            usage=derive_turn_usage(data),
            session_id=data.get("session_id") or "",
            # One key is the ordinary case; more than one means the call was served by
            # several models and no single name is honest, so the requested one stands.
            model=(served[0] if len(served) == 1 else "") or model or "",
        )
    if attribute:
        _attribute_subprocess(result, record)
    return result


def run_headless(prompt: str, system_prompt: str | None = None,
                 model: str | None = None, cwd: Path | None = None,
                 timeout: int = 300, tools: str | None = None,
                 attribute: bool = True, record: Any = None,
                 permission_mode: str | None = None,
                 env_extra: dict[str, str] | None = None) -> str:
    """`run_headless_result`, keeping only the text.

    Kept for callers that have nothing to account against — and for the `call=` seams,
    whose fakes return a plain string. Anything the OS pays for should call
    `run_headless_result` and record what comes back.

    It still ATTRIBUTES, though, and that is the point of passing the flags through: the
    LLM-graded evals reach the model through this wrapper, and they are the spend issue
    #103 was filed about. `permission_mode`/`env_extra` ride through for the same
    callers — the retrieval eval's tooled subject is both the thing being measured and,
    when the eval runs inside a work order, a real charge against it.
    """
    return run_headless_result(prompt, system_prompt=system_prompt, model=model,
                               cwd=cwd, timeout=timeout, tools=tools,
                               attribute=attribute, record=record,
                               permission_mode=permission_mode,
                               env_extra=env_extra).text


def unpack_headless(value: HeadlessResult | str) -> tuple[str, dict[str, Any] | None]:
    """(text, usage) from whichever of the two a transport seam returned.

    The `call=` seam in `structured.request` predates usage capture and its test fakes
    return bare strings. Rather than force every one of them to grow a dataclass, both
    shapes are accepted here: a string is text with nothing to account.
    """
    if isinstance(value, HeadlessResult):
        return value.text, value.usage
    return value, None


def session_transcript_path(cwd: Path, session_id: str) -> Path:
    """Location of the session transcript (~/.claude/projects/<munged-cwd>/<id>.jsonl)."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    munged = "".join(c if c.isalnum() else "-" for c in str(cwd))
    return config_dir / "projects" / munged / f"{session_id}.jsonl"
