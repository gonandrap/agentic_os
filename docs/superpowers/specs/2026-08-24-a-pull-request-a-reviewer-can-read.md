# A pull request a reviewer can read

Work order wo-5def741d. Two complaints about PR 143, both true, and the second is the
reason the first is not just an off day.

1. The body carried no test evidence, no questions, no learnings — nothing but three
   sentences of prose. The concision work of PR 131 said "a PR body hints to the
   reviewer; it does not explain the feature", and it was read as licence to cut what
   the reviewer needs rather than what the diff already says.
2. The body said `#2`, copied from the work order description's numbered item 2. GitHub
   turned it into a link to pull request 2 — someone else's.

This spec is the design behind the three artifacts that answer them.

## S1. Terse is not thin, and the brief could not tell the difference

The rule in `worker_brief.concision_section` is right and stays: a reviewer has the diff
and does not need it narrated. What it lacked was a floor. "Hint, do not explain" with
no statement of what survives the cut is an instruction to minimise, and a model
minimising against no floor arrives at three sentences.

So the fix is not a better sentence about brevity. It is a **named set of things the
body must contain**, which brevity applies *within* rather than *to*. Five sections:

| Section | Why the diff cannot supply it |
|---|---|
| Summary | what changed and why |
| Implementation notes | the alternatives rejected, the risk, where to start |
| Questions asked to Neo | the decisions that were not the worker's to take |
| Learnings | what the next work order inherits, by `kn-` id |
| Test evidence | what was run and what it reported |

Test evidence keeps all four of its rows — unit, UI, eval, A/B — filled or explicitly
n/a **with a reason**, because "no UI test" and "no UI change" are different facts and
only the reviewer gets to decide whether the first is acceptable.

## S2. Why this is a hook and not more contract prose

kn-fe226ab1 is the measurement that settles it: a rule shipped as contract prose scored
0/5 against the model's prior, twice, on two wordings. A worker that has internalised
"be concise" will not be talked out of a thin PR body by another sentence asking it not
to be.

`hooks.pr_title_decision` is the working precedent, on this exact command: a rule the
contract also states, made an invariant on the one path that opens pull requests in
practice. `pr_body_decision` sits beside it and inherits its posture in full —

- **Denies, never rewrites.** The body is the worker's to write; a hook editing the
  argument of a command it was asked to approve is a worse surprise than being told
  what to fix.
- **Narrow.** A body it cannot read — `--fill`, an editor prompt, `--body-file -`, an
  unresolvable path — is not its business, and it returns None.
- **The deny names the fix**, per problem, so the retry is one edit rather than a guess.

Ordering in `preflight_decision` matters and is unchanged in kind: gates first, then
title, then body, all before any auto-approval that could otherwise route around them.

## S3. The `#N` rule, and the two false positives it must not have

GitHub autolinks `#N` anywhere it renders. Denying every `#N` would block the two
legitimate uses, so the check allows:

- a **deliberate reference** — `issue #133`, `PR #143`, `fixes #133`, `closes #12`.
  The disambiguating word has to immediately precede the number, which is also the
  form that reads correctly to a human.
- anything GitHub **does not render**: inside a code span, a fenced block, or an HTML
  comment. Blanking those out preserves offsets rather than deleting them, so the
  "word immediately before" test cannot read across an erased region.

Cross-references GitHub resolves differently are excluded by the lookbehind, not by the
allow-list: `owner/repo#12` and a `…/pull/143#2` fragment are not bare refs.

Everything else is denied with all three ways out named — `item 2 of the work order`,
`issue #2`, or backticks.

## S4. Three artifacts, one source of truth for the section list

- `.github/pull_request_template.md` — what GitHub offers a human opening a PR here.
- `src/jarvis/assets/skills/open-a-pull-request/` — the worker skill, shipped to every
  managed project by `bootstrap.install_agent_assets`, carrying a byte-identical copy of
  the template for repositories that have none of their own.
- `hooks.PR_BODY_SECTIONS` — the list the hook enforces.

The templates are not generated from the constant; they are **asserted against it**, by
`tests/test_pr_body.py`, along with the byte-equality of the two copies. A generated
template would have to be built at install time in every project; an asserted one fails
CI the moment the three disagree, which is the only failure mode that matters.
