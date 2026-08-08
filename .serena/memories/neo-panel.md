# Neo as a panel — the primitive, the seats, and the fast path

Feature `fo-a73ac63c`, design at `docs/superpowers/specs/2026-08-02-neo-team-design.md`.
`src/jarvis/panel.py` replaces Neo's single headless call with a roster of profiled seats
plus a chair. **It ships DISABLED and at that default the OS is byte-identical to the
single-agent path** — same call count, same system prompt bytes, same message to the
worker, nothing in `panel_opinions`. Pinned by
`tests/test_neo.py::test_with_the_panel_disabled_neo_answers_exactly_as_before`.

## What exists today (wo-5b434f77)

| Piece | Where |
|---|---|
| the primitive + `premise` + `chair` | `src/jarvis/panel.py`, `src/jarvis/assets/neo-seats/*.md` |
| config | `catalog.PanelConfig`, nested under `NeoConfig`, parsed by `catalog._parse_panel` |
| injection | `daemon.Daemon._panel_answer` → `neo.drain_queue(answer=…)` |
| storage | `neo_store.panel_opinions`, `record_opinion` / `opinions` (wo-72382f17) |
| tests | `tests/test_neo_panel.py` |

Still to come: the `record`, `blast` and `taste` seats and the veto arbitration; the
`--panel` / `--seat` CLI flags and the dashboard; the LLM eval that measures cost and the
chair's observed brevity; enabling it by default (a catalog edit, gated on that eval).

## The five things that are not obvious

**1. `neo` MUST NEVER IMPORT `panel`.** The design says "`answer_question` becomes a caller
of it" and "fall back to the single-agent path if the premise seat fails", which taken
literally is an import cycle in a codebase that has none. The seam is instead a
`drain_queue(answer=…)` keyword the daemon injects; `panel` imports `neo`, never the
reverse, and the fallback is simply *not passing* `answer=`. A test AST-walks `neo.py` for
`Import`/`ImportFrom` nodes — a `sys.modules` check would miss it, because this codebase's
style is lazy imports inside function bodies.

**2. ON `route=fast` THE PREMISE SEAT'S OWN REPLY IS THE VERDICT AND THE CHAIR IS SKIPPED.**
The design claimed the fast path costs "one call total" for the ~95% of gate reviews that
are classifier false positives. It does not: a premise call plus "the chair then answers
directly" is TWO, i.e. a 2x cost and latency regression on Neo's highest-volume channel.
So `premise.md` emits the FULL verdict shape plus a `route`, and carries the answer-length
budget wording, because on that route its answer is what the worker reads.

**3. Two safety rules live in CODE, not in a prompt** (`panel.fast_is_permitted`): the fast
route is permitted only for a proposed `dismiss` or a question of kind `question`, and a
`kind=approval` may never reach verdict `approved` on it — a real privileged action always
costs the full panel. The second clause is redundant with the first *today* and is written
out anyway: a rule that survives only as a consequence of another rule stops holding the
moment that other rule is relaxed.

**4. Silence and unusable output are different failures, and only one falls back.**
`Opinion.replied` carries the distinction. A premise seat that replied with something that
will not parse HAS routed — toward `panel`, the expensive-but-safe side. Only a premise
that never replied at all (`ClaudeCliError`, timeout, no definition shipped in this build)
falls back to `neo.answer_question`, whose system prompt the test asserts BYTE-EQUAL to
`neo.build_system_prompt` so the fallback cannot drift into a third behaviour. A chair that
cannot be reached is total failure: `ClaudeCliError` propagates and `drain_queue`'s existing
rescue applies.

**5. Seats are DATA.** A seat is `assets/neo-seats/<seat>.md` (frontmatter + mandate) and a
name in `PanelConfig.roster`; adding one needs no change to `panel.py`. Deliberately NOT
`assets/agents/` — `bootstrap._rebuild` copytrees that directory wholesale into every
feature-order planner's `.claude/agents/`, so a seat dropped there becomes a bogus subagent
every planner can invoke (`tests/test_feature_order_team.py` now asserts that directory
holds EXACTLY the two planning seats). Frontmatter is parsed by a two-line `---` splitter:
the core is stdlib-only and does not grow a YAML dependency. No `tools:` key — meaningful
for a subagent, meaningless for a headless `claude -p` call.

## Config

`os.neo.panel`: `enabled` (default **false**), `roster` (default `("premise","chair")` — the
seats that ship), `seat_models`, `chair_model`, `timeout` (120s, and it must stay well below
Neo's own 300s: the seats run concurrently but inside the daemon's single Neo thread, so the
whole FIFO drain waits on the slowest seat), `kinds` (default `question` + `approval`;
`plan` is excluded because a plan review has its own reviewed persona the seats' mandates
say nothing about), `fast_path`.

A roster naming a seat that is not in `neo_store.SEATS` is a `CatalogError` — a typo that
silently drops a seat removes a safety check and tells nobody. But `SEATS` is the
VOCABULARY, not the set of seats shipped in this build: a roster naming a seat whose
markdown arrives in a later release parses, and is caught at run time (a `failed` opinion,
a loud log, the panel proceeds) rather than refusing to boot the fleet.

## Testing rules this feature runs on

* Assert on CALL COUNTS and `panel_opinions` ROWS, never on "a verdict came back". The panel
  defaults off, so any test that reaches it through the daemon without explicitly enabling
  it exercises the single agent and still gets a perfectly good verdict.
* The fake `claude`'s seat branch keys on `--append-system-prompt` (the header line
  `# Neo panel seat: <seat>`) and is placed FIRST, before the `PRIVILEGED ACTION REQUEST`
  branch: a premise call on a gate question carries that phrase in its USER prompt, so
  otherwise it comes back as a well-formed gate verdict with NO `route` key and a lenient
  `decide` defaults the route — a whole fast-path suite passing having exercised nothing.
  `fake_claude.fail_seat("premise")` fails exactly one seat; the shared `FORCE_FAIL` keys on
  the user prompt, which every seat on one question shares.
* The fake's canned `answer` fields contain NO seat name, on purpose: the pin that
  deliberation never reaches the worker asserts no seat name is in the delivered message.
* "This must not appear" needs a same-test "and here is where it does": the blindness test
  asserts no seat saw another's reply AND that the chair's prompt contains them; the
  never-pushed test asserts the seats deliberated and recorded distinct replies AND that the
  worker's message is the chair's answer alone.
* Assert seat prose against the SHIPPED MARKDOWN (`bootstrap.ASSETS / "neo-seats"`), never
  against a Python constant — the file the runtime reads is the enforcement.
