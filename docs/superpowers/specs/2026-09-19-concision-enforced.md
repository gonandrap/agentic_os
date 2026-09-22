# Concision, enforced: why the two skills never fired, and the mechanism that replaces asking

The 2026-08-22 concision work (`2026-08-22-agent-concision.md`) shipped an output style
and two skills. This spec is the follow-up that makes them take effect. It supersedes
that spec's S4 and S5; everything else there still stands.

## S1. What was measured, 2026-09-19

Across 142 dispatched worker transcripts under `~/.claude/projects/`:

| Skill | Invocations |
|---|---|
| `open-a-pull-request` | 78 |
| `report-jarvis-bug` | 5 |
| `superpowers:systematic-debugging` | 2 |
| `superpowers:receiving-code-review` | 1 |
| `shipit` | 1 |
| **`caveman`** | **0** |
| **`i-have-adhd`** | **0** |

`jarvis brief concision` is named in 119 worker prompts and was actually run in 11
sessions. `outputStyle: Concise` reached every worker correctly — it is in each
`worker-settings/*.json` and in each session's rendered system prompt — and did not hold.

Finish-summary length, project `jarvis_os`, n=214:

| Month | n | Median | 
|---|---|---|
| 2026-07 | 16 | 40 w |
| 2026-08 | 112 | 212 w |
| 2026-09 | 86 | 344 w |

p90 560 w, max 1319 w. The 2026-08-22 work landed mid-window and the median rose 62%
after it. Shipping an asset is not the same as the asset having an effect, and nothing in
the old test suite could tell the two apart: `tests/test_concision.py` asserted delivery.

## S2. Why each skill never fired

Two independent causes, and fixing only one would have changed nothing.

1. **The trigger could not match.** caveman's description gates on the user saying
   "caveman mode", "be brief", "less tokens", or "token efficiency is requested". No
   Jarvis worker prompt contains any of those, and a headless worker has no user to say
   them. The trigger described a conversation that never happens here.

2. **caveman excluded itself from every surface.** Its `## Boundaries` section sent all
   persisted artifacts back to normal prose — "code, comments, commits, docs,
   issue/PR/MR/defect/ticket/bug-report text, memory files, third-party messages". Every
   single thing a Jarvis worker writes is on that list, including the work-order record,
   because the record is written through a CLI call. So even a caveman that loaded would
   have compressed nothing. This is the cause that matters; the trigger is downstream
   of it.

`i-have-adhd` had only the first problem, in a milder form. "Use when writing anything a
person will read" is true of every turn, which reads as ambient advice rather than as a
moment to act on. A skill that always applies is a skill with no trigger.

## S3. Why the fix cannot be prose

`kn-fe226ab1` is the OS's own measurement of exactly this: a rule added to the worker
contract, tried in two wordings, scored **0/5 both times** against the model's prior. Its
recorded conclusion is that this class of problem "wants a MECHANISM ... or a hook that
refuses, not more contract". `hooks.pr_body_decision` is the precedent that worked.

So S5 is a mechanism. The contract edit in S7 exists only so the brief stops contradicting
the mechanism, and is not expected to move the number by itself.

## S4. The house style: caveman `full` over all output, `i-have-adhd` on top

The user's decision, taken with S4.1 in front of them. caveman's `## Boundaries` section
is **deleted, not narrowed**: compression applies to every byte a worker generates,
including commit messages, PR bodies, code comments and public bug reports.

The level is pinned to **`full`** — drop articles, fragments allowed, short synonyms — and
not `ultra`. `ultra` strips conjunctions, and a large share of work-order content is
multi-step sequences where "then" and "unless" carry the meaning.

`i-have-adhd` layers on top and does a different job. caveman decides how many words a
sentence costs; `i-have-adhd` decides what order the sentences go in, what gets cut
entirely, and that the first line is the answer. Compression without shaping produces a
short pile; shaping without compression produces what S1 measured.

### S4.1 What this costs, recorded because it was accepted rather than avoided

Commits, PR bodies and code comments outlive the OS. They are read by future sessions, by
reviewers, and by anyone who clones the repository — none of whom opted into the style.
That is precisely why upstream drew the boundary. The trade was put to the user
explicitly and whole-output compression was chosen anyway. This section is the record of
the trade, not an objection to it, and it is the thing to re-read if repository
readability later looks like the wrong price.

### S4.2 What is never compressed

These survive because they guard accuracy, not length, and dropping them would trade
correctness for brevity:

- exact error strings, numbers, units, code blocks, API names, symbol names, CLI commands
- the negations `not` / `never` / `no` / `only` / `except`, which flip meaning
- the Auto-Clarity carve-outs: security warnings, irreversible-action confirmations,
  multi-step sequences where fragment order risks a misread, and anywhere compression
  itself creates technical ambiguity
- failing test output and the things the worker did NOT do

## S5. The mechanisms

### S5.1 `SessionStart` injects the house style — every turn, no invocation needed

`hooks.house_style_context` returns the rules as `additionalContext` on `SessionStart`,
which fires on every turn (`source: resume` from turn two on). This is the same injection
channel the compaction brief already uses on `PostToolUse`, so it is proven in this
codebase.

This is what makes the skills *used* in the only sense that matters: their rules govern
every token of every turn, with no dependence on the model electing to load a file. The
skill files remain the source of truth and `concision.house_style` is generated from the
same rules, so there is one place to edit.

It is deliberately a digest and not the two files inlined. The full skills are ~2,900
tokens; the digest is ~400 and carries every operative rule. The examples, the intensity
table and the wenyan modes stay in the skill, which a worker can still open.

### S5.2 A deny hook on an over-long `--summary`

`hooks.finish_summary_decision`, a `PreToolUse` check on Bash, positioned beside
`pr_body_decision` and **before** the `is_jarvis_command_chain` auto-allow — otherwise the
auto-approval reaches around it, which is the same ordering argument `preflight_decision`
already makes for the PR checks.

Over the cap, it denies and says the count, the cap, and where the detail should go. It
never rewrites: a hook that edited the worker's summary would be putting words the worker
did not write onto a record the user reads as the worker's.

The denial is the second reason the skills now load. A worker has no reason to open
`i-have-adhd` until something tells it its output is wrong, and the denial says so at the
exact moment it is writing the thing.

### S5.3 The cap

120 words, from `JARVIS_SUMMARY_MAX_WORDS`, which `dispatch._write_worker_settings`
resolves from the catalog (`concision.summary_max_words`) and exports. It travels as env
for the reason `JARVIS_GATES` does: the hook runs on every Bash command and must not load
and parse the catalog to decide it has nothing to do.

120 is three times July's median of 40 and a fifth of today's p90. It is set generously on
purpose. A denied finish costs a whole extra turn, and every turn re-sends the entire
conversation at the cache-write rate — so a cap tight enough to catch good summaries would
cost more than the verbosity it prevents.

## S6. What the user sees

- Work-order summaries at roughly 100 words rather than 350–550, so `jarvis status`, the
  inbox and the dashboard are scannable again.
- No detail lost: the denial directs it to the turn-final message, which the brief already
  makes the record of the work.
- The compression rides along in every subsequent turn's re-send, so the saving compounds
  over a long work order rather than being paid once.

## S7. The contract contradiction this also fixes

`worker_brief.py` told workers both that `--summary` is "a one-line headline for that
answer, never a substitute for it" and that "a detail that lives only in the summary is a
detail that ceases to exist". Those are answerable together only by writing everything
twice, which is what the record shows workers did. The summary is now stated once as the
headline, with the turn-final message named as the place detail belongs, and the cap
quoted so the contract and the hook cannot disagree.

## S8. How this is proved

Unit tests pin the hook's decisions and the injection. They cannot show that behaviour
changed — S1 is the evidence that delivery tests never could.

The evidence that counts is `evals/llm/test_house_style_ab.py`, built in the shape of
`evals/llm/test_one_shot_turn_ab.py`: arm WITHOUT is the shipped prompt with the house
style cut by marker substring, arm WITH is untouched, and the score is the word count of
the summary each arm produces. kn-fe226ab1 is explicit that an A/B which re-composes the
arms, or which adds the rule to both, measures nothing — cutting by marker is what keeps
the arms byte-equal everywhere else.
