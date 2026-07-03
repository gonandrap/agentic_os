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
learnings, and the Jarvis persona's route-don't-do discipline. They cost tokens and
need a logged-in Claude Code, so they are opt-in:

```bash
JARVIS_EVALS_LLM=1 pytest evals/llm -q            # default model: sonnet
JARVIS_EVALS_LLM=1 JARVIS_EVALS_MODEL=opus pytest evals/llm -q
```

Run the LLM layer before changing any persona/prompt text (Neo's PERSONA, the worker
contract, CLAUDE.md) and paste the scorecard into the PR description.

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
