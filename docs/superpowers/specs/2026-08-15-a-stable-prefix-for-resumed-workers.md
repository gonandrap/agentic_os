# The fourth exit: a worker prefix that survives its own turn

**Work order:** wo-5722b6dc · **Measured:** 2026-08-15 · **CLI:** 2.1.233
**Corrects:** `docs/superpowers/specs/2026-08-10-resume-cost-and-the-cache.md` §"git status
in the system prompt is an anti-pattern, is upstream, and has no exit", and kn-625e79f1

## The report

The work order asked two questions: did the last round of PRs fix the cache problem, and
if not, refactor towards the practices in Anthropic's prompt-caching guidance.

**They did not, and they never claimed to.** PR #94 shipped three *mitigations* —
`--autocompact` to carry less context across a cold boundary, `FORCE_PROMPT_CACHING_5M=1`
to buy the cheaper write TTL, and the PreCompact/PostToolUse checkpoint. All three lower
the price of the boundary. None of them make it warm. The write-up said so plainly and
concluded the cause was upstream and unreachable: three exits, all closed.

**There is a fourth exit, it is one settings key, and it works.**

## The defect, restated

Claude Code composes its system prompt in two halves. The dynamic half carries cwd, env
info, memory paths and a **git-status snapshot** — branch, `git status --short`, the last
five commits, `git config user.name`. It is built once per *process*, and a Jarvis worker
turn is a process: `claude -p --resume <session-id>`.

So the sequence is:

1. Turn 1 starts, snapshot says `(clean)`.
2. The worker does its job, which means editing files.
3. Turn 2 resumes. The snapshot is rebuilt and now lists the changed files.
4. The system prompt differs, and prompt caching is a prefix match over
   tools → system → messages. A changed system prompt invalidates system **and every
   message after it** — the entire conversation.

The worker invalidates its own cache by working. This part was already established
(kn-625e79f1) and is not in dispute.

## The exit

`GOn()` in the 2.1.233 bundle:

```js
function GOn(){
  let e = process.env.CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS;
  if (Mn(e)) return !1;
  if (af(e)) return !0;
  return Wo().includeGitInstructions ?? !0
}
```

It has exactly four call sites — the definition and three consumers, enumerated from the
binary so the blast radius is a fact rather than an estimate:

| consumer | what it emits |
|---|---|
| `Lsv` → `Rsv` → `pOp` | **the git-status snapshot itself** |
| `jgS` | the long `# Committing changes with git` workflow |
| `zgS` | the lean `# Git` block, carrying both attribution trailers |

The first row is the one that matters and the reason the earlier investigation missed it:
the setting is named for *instructions*, and it turns out to gate the *status snapshot*
too. `--exclude-dynamic-system-prompt-sections`, the flag that was tested and rejected in
the previous round, only relocates the section; this one removes it.

## The measurement

Two arms, identical but for one settings key, each run twice. Both arms: turn 1 creates a
file (dirtying the tree), turn 2 resumes and says one word.

| arm | turn-2 cache_write | turn-2 cache_read | |
|---|---:|---:|---|
| default, run 1 | 10,983 | 15,995 | cold |
| default, run 2 | 10,993 | 15,995 | cold |
| `includeGitInstructions: false`, run 1 | 552 | 26,113 | **warm** |
| `includeGitInstructions: false`, run 2 | 632 | 26,121 | **warm** |

The 15,995 is the signature kn-625e79f1 documented: a cold boundary reads back the static
system prompt and nothing after it. In the treated arm the whole turn-1 prefix is read and
only the new user message is written.

## What it costs, and how that cost is paid back

Removing the snapshot also removes the two instruction blocks — measured at ~622 tokens in
a scratch repo. That is not free, but it is *static* text, so Jarvis can simply restate it
from a surface that cannot move between turns. `worker_brief.git_briefing()` does exactly
that on `--append-system-prompt`, which is precisely the rule the linked guidance states:
keep the system prompt frozen, and put anything that varies *after* the cached prefix.

Two deliberate departures from a verbatim copy:

* **The trailer names the model.** `Cpt()` derives it from the session's model, so a
  static copy would have to hardcode one. `MODEL_ATTRIBUTION_NAMES` mirrors the CLI's own
  id→name table and longest-prefix matches it, so dated ids resolve; anything unrecognised
  — including a floating alias like `opus`, whose target only the CLI knows — falls back
  to the CLI's own fallback, plain `Claude`. A generic trailer is correct. A wrong model
  name is not.
* **"Commit only when the user asks" is not copied.** For a worker the work order *is* the
  ask, and the operating contract already tells it to commit and open a PR. Copying that
  line would have the system prompt contradict the contract.

The briefing also tells the worker the snapshot is gone and to run `git status` itself,
which is the one real behavioural loss: it no longer opens a turn knowing the state of its
tree.

## End-to-end verification

Neo's approval was conditional on proving the attributions survive. Run on the real CLI
with exactly what dispatch now writes — the settings file and the composed
`--append-system-prompt` — in a scratch repo, two turns:

```
turn1  {"cache_write": 27993, "cache_read": 107778}
turn2  {"cache_write": 1845,  "cache_read": 56832}     <- warm

commit message:  Add 'second' line to note.txt
                 Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

PR body:         ...
                 🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

The worker also branched rather than committing to `main`, which is the line that replaced
upstream's. All three properties hold at once: the trailer, the footer, and a warm resume.

## What this does not change

Everything else in the 2026-08-10 write-up stands: the 1.25×/2× write multipliers and the
5-minute TTL decision, the double-cold-write mechanism (the tools array changing mid-turn),
the MCP usage census, and the compaction checkpoint. `--autocompact` also remains worth
having and is now *more* valuable, not less: a warm boundary still re-reads the whole
conversation at 0.1×, so context size continues to set the running cost of every call.

The change is to one sentence of that document — that the anti-pattern has no exit — and to
the headline of kn-625e79f1, which said Jarvis cannot fix this and should report it
upstream instead of looking for a config fix. There was a config fix.

## Method

Behaviour read out of the binary per kn-f6f418a9 (`strings` plus `python re` over the
bundle) — that is how `GOn` and its call sites were enumerated, and how the id→name table
was copied rather than guessed. Token figures come from the CLI's own
`--output-format json` usage block on a purpose-built two-arm harness, not from transcript
arithmetic, because the arms had to differ in exactly one variable.
