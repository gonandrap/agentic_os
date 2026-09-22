# How worker output is kept short

Added by PR #574 (commit 5aff3ce). Supersedes the August output-style work, whose tests
asserted the *arrival* of assets and not their effect.

## The failure it fixes

`caveman` and `i-have-adhd` shipped in August, reached every worker, and were invoked
**0 times in 142 sessions**. Median finish summary: 40 words (July) → 344 (September).
Two causes, both in `caveman`:

- it triggered on a user saying "caveman mode" — a headless `-p` worker has no user;
- its `## Boundaries` section exempted every persisted artifact, which is every surface
  a Jarvis worker writes. A *loaded* caveman would have compressed nothing.

Do not fix this class of bug with more contract prose. `kn-fe226ab1`: a contract rule
measured **0/5 twice** against the model's prior. It wants a mechanism.

## The two mechanisms

`src/jarvis/concision.py` — stdlib only, deliberately no `catalog` import. The hook runs
on every Bash command, and parsing the catalog is a ~39% tax on a ~155 ms hook.

| Piece | Where | Note |
|---|---|---|
| `house_style()` | `hooks.py` SessionStart → `hookSpecificOutput.additionalContext` | Fires every turn (`source: resume` from turn two). The only channel that reaches every byte a turn generates. Wrapped in `HOUSE_STYLE_BEGIN`/`END` so the A/B can cut exactly one substring. |
| `finish_summary_decision()` | `hooks.py` PreToolUse deny | Denies `jarvis wo finish --summary` over the cap. **Must stay wired before `is_jarvis_command_chain`** — that auto-allow waves every `jarvis …` through and makes the cap unreachable. |

Cap: `DEFAULT_SUMMARY_MAX_WORDS = 120`, handed to the worker as env
`JARVIS_SUMMARY_MAX_WORDS` by `dispatch.py`, configured per project as
`concision.summary_max_words` (`catalog.ConcisionConfig`). `0` switches it off; the
parser refuses 1–19 as a nonsense value. `ops.APPLY_RULES` scopes it `next-dispatch`.

## caveman is no longer a verbatim vendor copy

Per the user's decision it applies to **all** generated output at level `full` — commits,
PR bodies, code comments and public bug reports included. `## Boundaries` is gone.
`assets/skills/caveman/README.md` enumerates the diff against upstream; keep it accurate
if the skill is touched again. Never compressed: code, commands, paths, identifiers,
quoted error text.

## The contract contradiction that caused the doubling

`worker_brief.py` called `--summary` a one-line headline and also warned that a detail
living only there "ceases to exist". Satisfiable only by writing everything twice, which
is what workers did. Removed; `tests/test_worker_brief.py` carries a negative assertion so
it cannot come back. `CORE_BUDGET_CHARS = 3220` is asserted by two tests — trim your own
prose rather than raising it.

## Proof

`evals/llm/test_house_style_ab.py` (opt-in, `JARVIS_EVALS_LLM=1`). A marker-cut A/B scored
on word count, not on a judge. The cap sentence lives in `worker_brief` and is therefore in
BOTH arms on purpose — what is measured is the injection on top of it. The third assertion
guards the real hazard: a final message under 60 words means brevity was bought by gutting
the report. Plus 15 mechanism tests in `tests/test_concision_mechanisms.py`.

Design record: `docs/superpowers/specs/2026-09-19-concision-enforced.md`.
Open calls with reversal criteria: `ASSUMPTIONS.md` 82 and 83.
