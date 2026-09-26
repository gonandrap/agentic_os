# Claude Code's OTEL export, measured

Findings for §9 of `docs/specs/2026-09-24-order-observability.md`. Not a design, not a
plan. Measured 2026-09-25 against CLI **2.1.282** with `scripts/spike_otel.py`.

## Verdict — DECLINE

Claude Code's OpenTelemetry export carries **none** of the three things this feature
needs (tool parameters, cache-write cause, context composition), and on the case most
worth debugging — a turn killed mid-flight — it exports **no metrics at all** and loses
its last ~5.5 s of log events. The transcript is on disk throughout.

No follow-on feature order is filed. Nothing is built. The env seam stays one line — the
`env = {**os.environ, **cache_env()}` dict inside `claude_cli.spawn_turn`
(`src/jarvis/claude_cli.py`) — so adopting OTEL later remains a dict, not an
architecture. Knowledge entry kn-a2504a39 records this same seam as line 805 as of
2026-09-25; that ledger is append-only and cannot be corrected in place, so the symbol is
the address to trust if the line has moved.

**A decline is a full result.** The four reasons in §9 were reasoned, not measured; one
of them turned out wrong and another understated what arrives. The measurement decided
this, not the prior.

## The problem — what was undecided

§9 recorded that **nobody had ever run `claude` with `CLAUDE_CODE_ENABLE_TELEMETRY=1`
and enumerated what arrives**, so the four reasons for declining settled nothing. Three
sections of the feature depend on the answer:

- §4 needs a tool name **with its parameters**.
- §2 / `inspection.classify_writes` (`src/jarvis/inspection.py:685`) needs a cache-write
  **cause** — the `cold-start` / `ttl-expiry` / `prefix-miss` distinction, and the
  5m-versus-1h TTL split under it.
- §5 needs **context composition** — the share of the window taken by system prompt,
  skills, agents.

If OTEL carried any of those, the feature would be building something the CLI already
ships.

## The fix — decline, and keep the door open at zero cost

Adopt nothing. §8 of the parent spec gains a DECLINED entry pointing here and at
`scripts/spike_otel.py`. The env seam is left exactly as it is: the
`env = {**os.environ, **cache_env()}` dict literal in `claude_cli.spawn_turn`, which is
where an adoption would land if the answer ever changes. Nothing in this finding costs
anything to keep open. The six answers below are complete **for 2.1.282 only** — a later
CLI can emit anything and has to be re-measured. `scripts/spike_otel.py` is committed so
that re-measurement is a script run, not a fresh spike.

Two things OTEL uniquely has are parked as backlog notes in §8 rather than built; see
"What OTEL has that the transcript does not" below.

## How it was measured

`scripts/spike_otel.py`, hand-run, committed in the same pull request, stdlib only: a
throwaway `ThreadingHTTPServer` on an ephemeral 127.0.0.1 port that records every POST
body verbatim and interprets nothing, plus a real headless `claude -p` turn spawned
against it. Linux, one machine, one worker.

Env overlay, verbatim:

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:<port>
```

Four script runs:

1. `--mode clean` — default intervals, 120 s drain after exit.
2. `--mode killed --kill-after 25` — SIGKILL the process group mid-tool-call, 120 s drain.
3. `--mode clean --interval-ms 3000` — control: does the interval knob work at all.
4. `--mode dead` — no listener, endpoint at a port the script **proves** closed: bind an
   ephemeral port, close it, require the connect to be refused. The original closed-port
   measurement was an uncommitted shell run; re-done through the script on 2026-09-25 so
   the finding is reproducible from the artifact.

The turn's prompt made three Bash calls each running `sleep 4; echo SPIKE-MARKER-<n>`,
Read a file, and dispatched one built-in `Explore` subagent — so tool parameters, a file
path and a subagent were all present to be exported if the export carried them.

`scopeMetrics[].scope` on the wire: `{"name": "com.anthropic.claude_code", "version":
"2.1.282"}`. Bodies were uncompressed `application/json`, so `http/json` is honoured and
no protobuf decoder is needed.

## The six answers

### Q1 — what arrives

Endpoints `POST /v1/metrics` and `POST /v1/logs`. **Nothing ever arrived on
`/v1/traces`** despite `OTEL_TRACES_EXPORTER=otlp` — there are no spans.

Four metrics, all `sum`, all `aggregationTemporality: 1`, all monotonic:

- `claude_code.session.count` (attr `start_type=fresh`)
- `claude_code.cost.usage` (USD)
- `claude_code.token.usage` (tokens)
- `claude_code.active_time.total` (s, attr `type=cli`)

Twelve log-event names (the `event.name` attribute, un-prefixed; the log record's `body`
carries the same name prefixed `claude_code.`): `user_prompt`, `api_request`,
`assistant_response`, `tool_decision`, `tool_result`, `subagent_completed`,
`hook_registered`, `hook_execution_start`, `hook_execution_complete`,
`mcp_server_connection`, `plugin_loaded`, `managed_settings_resolved`.

Useful attribute sets, verbatim:

- `api_request`: `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens`, `cost_usd`, `cost_usd_micros`, `duration_ms`, `ttft_ms`,
  `request_id`, `client_request_id`, `speed`, `effort`, `query_source`, `prompt.id`,
  `session.id`, `event.sequence`.
- `tool_result`: `tool_name`, `tool_use_id`, `success`, `duration_ms`,
  `tool_input_size_bytes`, `tool_result_size_bytes`. Nothing else.
- `tool_decision`: `tool_name`, `tool_use_id`, `decision` (measured `accept`), `source`
  (measured `config`), `tool_source` (measured `builtin`).
- `subagent_completed`: `agent_type` (`Explore`), `agent.source` (`built-in`),
  `is_built_in`, `is_async`, `model`, `final_model`, `model_swapped`, `total_tokens`
  (17958), `total_tool_uses` (2), `duration_ms` (10160).
- `hook_execution_complete`: `hook_event`, `hook_name`, `hook_source`, `num_hooks`,
  `num_blocking`, `num_success`, `num_cancelled`, `num_non_blocking_error`,
  `num_outputs_persisted`, `total_duration_ms`, `stdout_chars`,
  `additional_context_chars`, `initial_user_message_chars`, `system_message_chars`.

Every single event and every metric datapoint also carries `user.id`, **`user.email`**,
`user.account_id`, `user.account_uuid`, `organization.id`, `session.id`, `terminal.type`
(measured `non-interactive`). That is the user's real email address on every record,
leaving the process on every flush, to whatever the endpoint points at. It is a reason of
its own to keep the door shut until there is a need.

Correlators: `prompt.id` is per-turn, shared by every log event of one turn; `session.id`
is the session. There is **no** work-order id and no way to add one, so any adoption would
have to map `session.id` to a work order — which Jarvis already can.

### Q2 — tool name with its parameters: NOTHING

`tool_result` carries `tool_input_size_bytes` — the SIZE of the input, not the input.
Literal searches over the concatenation of every decoded body: `SPIKE-MARKER` not found,
`sleep` not found, `README` not found, `parameters` not found. The three Bash commands and
the file path were in the turn and in none of the payloads. §4 cannot be served by this
export; the transcript has the parameters.

### Q3 — cache-write cause and the TTL split: NOTHING of the cause, TOTALS ONLY of the tokens

`api_request` carries `cache_read_tokens` and `cache_creation_tokens` as flat per-request
integers. `claude_code.token.usage` splits its datapoints by a `type` attribute whose
measured values are `input`, `output`, `cacheRead`, `cacheCreation`.

`ephemeral_5m` and `ephemeral_1h` were not found anywhere. `modelUsage` was not found
anywhere. So there is no 5m-versus-1h split, and nothing from which
`inspection.classify_writes`'s `cold-start` / `ttl-expiry` / `prefix-miss` distinction
could be derived — a counter cannot express a cause.

One near-miss, and nothing is to be read into it: the model string is
`claude-opus-5-5[1m]`. `[1m]` is the 1-million-token context-window variant, **not** a
cache TTL.

### Q4 — context composition: NOTHING

No attribute anywhere reports the share of the window taken by the system prompt, a skill
or an agent. Three attributes look like it and are not:

- `hook_execution_complete`'s `system_message_chars`, `additional_context_chars`,
  `initial_user_message_chars` measure what a HOOK emitted and read, in characters — not
  the window.
- `plugin_loaded`'s `agent_path_count`, `skill_path_count`, `command_path_count` count the
  files a plugin ships — not what reached a prompt.

§5 stands exactly as written.

### Q5 — flush interval, and the killed turn

**This is the finding that decides it.**

Logs, default interval: arrivals at t = 6.12, 14.85, 20.84, 26.63, 33.53, 39.09, 39.91 s
— gaps 8.73, 5.99, 5.79, 6.90, 5.56, 0.82 s. Roughly every 5-6 s, plus a flush at exit.

Metrics, default interval: **one** export, at t = 39.91 s, with the turn exiting at
t = 40.30 s. Nothing in the preceding 39.9 s. Stated exactly: the default metric interval
was **not pinned** by this run, only bounded — it is longer than 39.9 s — and the single
export coincided with process exit, so what was observed is the shutdown flush and not a
periodic one. (The documented SDK default is 60 s; that is documentation, not this
measurement.)

Killed turn, SIGKILL to the process group at t = 25.02 s: `requests_before_kill: 3`,
`requests_after_kill: 0`, metric names collected: **none at all**. The last log export was
at t = 19.54 s, so about 5.5 s of the turn's log events — including its final
`tool_result` — went with it. A turn that dies exports NO metrics whatsoever and loses its
last few seconds of events, and that is the case most worth debugging. The transcript is
on disk throughout.

Control run, `OTEL_METRIC_EXPORT_INTERVAL=3000` / `OTEL_LOGS_EXPORT_INTERVAL=3000`:
metrics arrived at t = 4.08, 10.08, 16.08, 22.08, 28.09, 31.09, 34.14, 37.14, 39.91. The
knob works — so shortening the interval would narrow the loss window but cannot close it.
The export is in the process being killed.

### Q6 — what it costs to run

CLI side: nothing new. The exporter is in-process and POSTs over HTTP, so no extra process
per worker.

The new process is the **collector**: one long-lived listener on one port that every
concurrent headless worker POSTs to, against a core that is deliberately stdlib-only.

Measured failure mode, endpoint pointed at a CLOSED port with telemetry on. First
measurement, ad-hoc shell: the turn was completely unharmed — `rc=0`, result envelope
`subtype: success`, `is_error: false`, `duration_ms: 7893`, and the only stderr line was
the unrelated ordinary `Warning: no stdin data received in 3s`. Re-run through
`--mode dead` on 2026-09-25, CLI 2.1.282: closed port 43883, proof
`ConnectionRefusedError(111, 'Connection refused')`; `turn_rc: 0`,
`result_subtype: success`, `result_is_error: false`, `result_duration_ms: 35848`,
`request_count: 0`, and stderr EMPTY — `stderr_bytes: 0`, cap 20000 chars, not truncated.

So export failure is **silent** in both runs: it does not break a turn, and it does not
report that data was dropped either. Knowledge entry kn-a2504a39's "no stderr line" holds
for the script path. The shell run's stdin warning came from how that invocation handled
stdin, not from telemetry — the script spawns with `stdin=subprocess.DEVNULL`. The two
runs differ in stdin handling, not in what telemetry reported.

Concurrency was NOT measured — one worker only.

## The four planner reasons, scored

1. **Refined, not simply confirmed.** The three specific absences (tool parameters,
   cache-write cause, context composition) are CONFIRMED. The framing "metrics and log
   events aggregated on a flush interval" is too coarse: log events are per-event and not
   aggregated, and `api_request` gives genuine per-API-call token and cost detail keyed by
   `request_id`. It is still strictly less than `usage.py` and `inspection.py` parse, and
   `modelUsage` is absent — but the reason as written understated what arrives, and that
   correction is part of the finding. Survives, corrected.
2. **Confirmed on process and port; the "new failure mode" half is CONTRADICTED.** A dead
   endpoint costs the turn nothing at all: rc=0, success, normal duration, no error line.
   The real risk is the opposite of what was feared — silent loss, not breakage. And a new
   failure mode the reason did not name arrived instead: `user.email` and the account
   identifiers on every record. Survives only as the process-and-port half.
3. **Confirmed, and STRONGER than stated.** The reason said a dying turn "may never
   flush". Measured: it never flushes metrics at all, and loses its last ~5.5 s of log
   events. Survives.
4. **Confirmed and unchanged.** The seam is one dict in `claude_cli.spawn_turn` —
   `env = {**os.environ, **cache_env()}`; nothing in this finding costs anything to keep
   open. Survives.

## What OTEL has that the transcript does not — and why it is still a decline

Two things, named so nobody has to re-measure:

1. Per-hook execution timings — `hook_execution_complete`'s `total_duration_ms`,
   `num_blocking` and friends.
2. `ttft_ms` per API request.

Neither serves any section of this feature. §8 rejects hook-recorded spans and any
live-state table on the merits, and §§2/4/5 need precisely the three things the export
lacks. So both are recorded here and noted in §8 as backlog notes, and **no feature order
is filed** — filing one would invent work nobody asked for.

## What was not measured

- One CLI version (2.1.282) on one Linux machine.
- One worker. Nothing here says how N concurrent workers behave against one collector.
- No real collector, deliberately: the question was what arrives, not what a collector
  does with it.
- `grpc` and `http/protobuf` transports untested.
- `OTEL_LOG_USER_PROMPTS=1` was NOT set. With it unset, the `user_prompt` event's `prompt`
  attribute and the `assistant_response` event's `response` attribute both arrived with the
  literal value `<REDACTED>`, while `prompt_length` (465) and `response_length` (390)
  arrived as real integers. The default is safe; what setting that flag exports was not
  measured.
