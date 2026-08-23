# Agent concision: what the OS ships to make its sessions terse

Work order wo-322cd6f8. The complaint was that every Claude session Jarvis creates is too
verbose, in three specific places: source comments that document instead of pointing at
documentation, PR bodies that explain the feature instead of hinting to the reviewer, and
work-order prose that repeats itself across the description, the questions and the
messages.

This spec is the design behind the three changes that answer it. Code cites it by section
number (`-- SS4`) rather than restating it, which is the same rule this spec asks agents
to follow.

## S1. The three surfaces, and why one lever cannot cover them

| Surface | Read by | Governed by |
|---|---|---|
| Chat text (WO messages, Neo questions, the final answer) | user, Neo | output style |
| Source comments | reviewers of the PR | the worker brief + kn-f861a2f6 |
| PR body | the reviewer | the worker brief |

An output style shapes what the assistant *says*. It has no opinion about what the
assistant *writes into a file*, so it cannot fix comments or PR bodies on its own. The
brief covers those two; the output style covers the first. Both ship, and the skill in S4
carries the shared style rules that neither mechanism enforces by itself.

## S2. What the delivery mechanisms can actually do

Probed live against Claude Code 2.1.241 on 2026-08-22, each with a control.

1. `"outputStyle": "<name>"` in a `--settings` file **works**, for the four built-in
   styles only (`Concise`, `Proactive`, `Explanatory`, `Learning`). Control: the same
   invocation with no settings file reported no active output style.
2. `--add-dir X` does **not** load `X/.claude/output-styles/`. It loads
   `X/.claude/skills/` and `X/.claude/agents/` (kn-02cb9c88) and nothing else. Control:
   a custom style planted there, whose only rule was to prefix every reply with a
   sentinel token, produced no sentinel.
3. Therefore a *custom* output style cannot reach a worker without a new delivery
   mechanism (writing `<project>/.claude/output-styles/` from bootstrap, or a plugin),
   and only one output style is ever active — so a custom one and `Concise` are
   mutually exclusive.

## S3. The output style: `Concise`, in the base settings

`assets/settings.base.json` gains `"outputStyle": "Concise"`. That one line covers both
audiences because both settings files are built from it:

* `bootstrap.inject_settings` writes `<project>/.claude/settings.json` — the sessions the
  user opens by hand.
* `dispatch._write_worker_settings` writes `<project>/.jarvis/worker-settings/<wo>.json`
  and passes it as `--settings` — every session Jarvis spawns, including follow-up turns
  (kn-c282473c).

A project that wants a different style overrides it in the catalog's
`settings_overrides`, which `build_settings` deep-merges on top. Nothing else is needed:
`Concise` is built in, so there is no file to install and no path to get wrong.

## S4. The two skills

Both ship under `assets/skills/`, so both reach every worker through
`bootstrap.install_agent_assets` and `--add-dir` — the path every OS skill already uses
(kn-c9281024). They are complementary, not redundant: caveman's own `## Boundaries`
section excludes everything persisted outside the chat (code, comments, commits, docs, PR
and issue text), so it compresses what a session *says*; `i-have-adhd` and S6 cover what a
session *writes*.

### S4.1 `caveman`, verbatim

<https://github.com/JuliusBrussee/caveman>, `skills/caveman/`, MIT (the repo is split
MIT/BSL-1.1 and `skills/` is on the MIT side per its `LICENSING.md`). It ships unmodified:
upstream declares no `disable-model-invocation` and its description already auto-triggers
on a request for token efficiency, so a headless worker can load it as-is. Provenance and
the pinned commit: `assets/skills/caveman/README.md`.

### S4.2 `i-have-adhd`, adapted so a headless worker can load it

The user asked for <https://github.com/ayghri/i-have-adhd> to ship to every session. Two
facts made a verbatim copy useless for that:

1. Upstream is an *output style* that declares `disable-model-invocation: true` and says
   it is invoked with `/i-have-adhd`. A headless `-p` worker cannot type a slash command,
   so a verbatim copy dropped into the skills tree would be inert.
2. Only one output style is active at a time (S2.3), so it cannot be installed *as* a
   style without displacing `Concise`.

So the rules ship as a normal OS worker skill: `assets/skills/i-have-adhd/`, model
invocation enabled, with a description that names the moments this work order actually
complained about — writing a comment, a PR body, or a work-order message. The MIT licence
ships beside it.

The body is trimmed and re-pointed at an agent audience: upstream's rules about restating
state across turns and giving time estimates address an interactive human collaborator and
do not survive the move, while the rules about leading with the action, capping lists,
suppressing tangents and the pre-send delete list do.

## S5. The vendored digest copy stays exactly where it is

`assets/digest/i-have-adhd.SKILL.md` is a byte-verbatim upstream copy, read at run time by
`jarvis.digest` as the system prompt that shortens an over-long Neo question. It is
deliberately outside every tree bootstrap copies, and `tests/test_digest.py` pins that.

That pin is now expressed as *the verbatim bytes reach no project*, rather than *no file
whose name contains "adhd" reaches a project*. The narrower name test would have failed on
the S4 skill for the wrong reason: the invariant was never about the name. Keeping both
copies is intentional — the digest's is a diffable snapshot of someone else's file
(`assets/digest/README.md`), the skill's is ours to edit.

## S6. The brief: comments, PR bodies and repeated prose

`worker_brief` gains a `concision` section and one core bullet pointing at it. The bullet
carries the three rules in their shortest actionable form, because a worker that never
fetches the section still has to obey them; the section carries the reasoning.

The rules:

1. **Comments point, they do not document.** If an explanation runs past a couple of
   lines it belongs in `docs/superpowers/specs/YYYY-MM-DD-<name>.md` with numbered
   sections, cited from a one-line comment. This is kn-f861a2f6, promoted from a
   knowledge-base entry a worker had to go looking for into the contract it is judged
   against.
2. **A PR body hints, it does not explain.** What to look at, what is risky, what was
   decided and where the reasoning lives. The reviewer reads the diff for the rest.
3. **Say each thing once.** The description, the questions, the messages and the finish
   summary are read together; text repeated across them is read twice and adds nothing
   the second time. Refer to what is already on the record instead of restating it.

The bullet is in the core rather than behind the fetch because of what a worker has to
already suspect before it fetches. A worker in doubt fetches; a verbose worker is not in
doubt, so `jarvis brief concision` would be read by everyone except the population it is
aimed at — and the damage it prevents (the over-commented diff, the essay PR body) is done
before anyone could tell it to look.

That cost `CORE_BUDGET_CHARS`, raised 2500 to 2750. The 2500 figure was set against a
2080-char core and PR 124's `--evidence` bullet spent the whole of that headroom while
this work was in flight, so the merged core did not fit. The bullet was compressed from
381 to 246 chars first; the remainder is the raise. The test for adding a bullet here is
the paragraph above, not available headroom.

## S7. Re-probing S2

The probe is three headless `claude -p --model haiku` calls with `--tools ''` and a `--`
fence before the prompt (the fence matters: `--tools` is variadic and swallows a bare
positional — `claude_cli` documents the same trap). Two of them ask the model to name its
active output style; the third plants a custom style under `--add-dir` and checks whether
its sentinel appears. Re-run it against a new CLI version before assuming S2 still holds:
`outputStyle` is not documented as a settings key anywhere the OS controls.
