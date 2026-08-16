# Evals — behavioral scorecards for Jarvis and Neo

Unit tests (`tests/`) check that the code works. **Evals check that the agents behave**:
that Jarvis surfaces exactly what needs the user and routes work correctly, and that
Neo answers, escalates, learns, and spends tokens the way it promises to.

## Two layers

**Deterministic evals** (`evals/test_*.py`) — scenario batteries against the fake
`claude` CLI. Every user-visible guarantee is a named scenario; the suite prints a
scorecard by category and writes `evals/results.json`. These run in CI and gate merges.

```bash
pytest evals -q
```

**LLM-graded evals** (`evals/llm/`) — batteries against the real `claude` CLI, judging
the parts only a model can get wrong: Neo's escalation judgment, adherence to
learnings, the Jarvis persona's route-don't-do discipline, the worker contract's
assume-vs-ask judgment, and whether a worker handed a knowledge *index* actually
retrieves from it. They cost tokens and need a logged-in Claude Code, so they are
opt-in:

```bash
JARVIS_EVALS_LLM=1 pytest evals/llm -q            # default model: sonnet
JARVIS_EVALS_LLM=1 JARVIS_EVALS_MODEL=opus pytest evals/llm -q
JARVIS_EVALS_LLM=1 pytest evals/llm -q -s         # -s for the per-case breakdown
```

Run the LLM layer before changing any persona/prompt text (Neo's PERSONA, the worker
contract, CLAUDE.md, the seat mandates in `src/jarvis/assets/neo-seats/`) and paste the
scorecard into the PR description.

### The panel eval

`evals/llm/test_neo_panel_judgment.py` is the measurement that Neo's panel is gated on:
it grades `panel.decide` on the same twelve invented gate commands that
`test_gate_review_judgment.py` grades the single agent on, so the two scorecards are
directly comparable. It also prints a **cost reading** — calls per decision and
wall-clock — that nothing asserts on: the design calls the cost claim "a claim to
measure, not to assert", and this repo has no baseline.

It does **not** replay the 34 real production decisions the design's Measurement section
proposes. That is deferred to **bl-cc5df0bd** and needs two things a worker cannot supply:
the corpus checked in as a fixture (this repo is public, and those questions carry project
names, PR numbers and work-order prose), and the user's own re-labelling of a ground truth
that contradicts itself.

`tests/test_neo_panel_eval_harness.py` runs for free on every `pytest tests/` and proves
this eval is still wired — that its skip gate reads exactly `JARVIS_EVALS_LLM`, that the
batteries are non-empty module-level literals with no production path in them, and that
`panel.decide` is what it calls.

`test_knowledge_retrieval.py` is the odd one out: its subject is **tooled and does real
work** in a throwaway sandbox with a real `jarvis` on its PATH, because asking a
tool-less model "what command would you run?" primes the very answer being graded. It
is therefore the slowest battery here (seven agent runs, ~10 minutes) and the only one
that needs `--permission-mode auto` — the same mode dispatch gives every real worker,
since a non-interactive session cannot answer a permission prompt.

`tests/test_knowledge_retrieval_eval_harness.py` is its free companion, and this eval
needs one more than the others: its grading apparatus is bespoke (a generated `jarvis`
shim, a call log, mtimes compared against it) and its subject is the only caller in the
repo of `run_headless(permission_mode=…, env_extra=…)`. Both can break without a single
other test noticing. The companion drives the shim end-to-end against a seeded store,
checks each case is still rigged (the deciding text off the headline, the `applied()`
predicate false before and true after), and asserts the subject is configured the way
dispatch configures a real worker.

Its sandbox `JARVIS_HOME` is passed to the subject explicitly, never exported by the
harness: the runner keeps the throwaway home the repo-root isolation gate (`#36`,
`tests/test_isolation_gate.py`) gave it, and the subject gets its own through
`env_extra`. Two stores, one variable, neither process fighting the other for it — and
no eval run can reach production state even if the gate were removed.

## Reading the scorecard

Each scenario is `category :: name`. The terminal summary groups by category with a
pass ratio; anything below 100% on a deterministic category is a regression, and the
LLM categories declare their own thresholds (e.g. escalation accuracy ≥ 0.8) inside
the test.

## Adding scenarios

Add a `case()` entry to the relevant battery (they're plain parametrize lists). Keep
one behavior per scenario, name it after the guarantee ("escalations reach the inbox"),
and prefer asserting on user-visible surfaces (`jarvis status` output, queued messages,
inbox rows) over internals.
