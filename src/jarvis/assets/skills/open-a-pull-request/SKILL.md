---
name: open-a-pull-request
description: Use when opening a pull request for a work order — before running `gh pr create`. Fills the repository's PR template with the summary, implementation notes, Neo questions, learnings and test evidence a reviewer needs, and keeps GitHub from turning a work-order item number into a link to someone else's PR.
---

# Opening a pull request

The pull request is the only artifact of your work order that a reviewer reads beside
the diff, and the only one that outlives the OS's records. It is also the one your
operating contract tells you to keep terse — and terse is not the same as thin. The
brief's "a PR body hints, it does not explain" deletes **narration of the diff**. It
does not delete the five things below, which the diff cannot tell anyone.

A `gh pr create` whose body is missing a section, or which contains a bare `#N`, is
**denied by a hook** with the fix named. Filling the template correctly the first time
is faster than being sent back.

## 1. Get the template

In this repository, in order:

```bash
cat .github/pull_request_template.md 2>/dev/null \
  || cat .github/PULL_REQUEST_TEMPLATE.md 2>/dev/null \
  || cat docs/pull_request_template.md 2>/dev/null
```

If the repository has none, use this skill's bundled copy —
`pull_request_template.md`, beside this file. Its five `##` headings are what the hook
requires, so do not rename or drop any of them.

## 2. Fill every section

Write the body to a file and pass `--body-file`; a body this shape does not survive
being typed as a shell argument.

**Summary** — what changed and why, a few sentences. Longer rationale is a spec:
write it under `docs/superpowers/specs/YYYY-MM-DD-<name>.md` and link it.

**Implementation notes** — bullets, each one a thing the reviewer would otherwise have
to reverse-engineer: a decision you took, an alternative you rejected and why, the
risky part, where to start reading. Not a file-by-file walk of the diff.

**Questions asked to Neo** — one bullet per question you asked, with its link. You get
the id back from `jarvis wo ask` (`question_id`), and `jarvis wo show <wo-id>` lists
them again in the timeline as `question_asked` events. The link is the dashboard's
question page:

```
http://localhost:8787/neo/question/<id>
```

Use the host and port from your own catalog if the OS is not on the default port.
If you asked none, write `None.` — an empty section reads as a section you forgot.

**Learnings** — one bullet per knowledge-base entry this work wrote, with its `kn-` id
and its headline. Write these with `jarvis learn add` *before* you open the PR, so the
ids exist to cite. `None.` if you wrote none.

**Test evidence** — the command you ran and what it actually reported. Not "tests
pass": the numbers.

```
| Unit / integration | `uv run pytest tests/ evals/` | 1965 passed, 70 skipped |
```

Keep all four rows. A row that does not apply says so **and says why** — "n/a, no UI
change" and "n/a, did not run the UI tests" are different facts, and only the reviewer
gets to decide whether the second one is acceptable. If your change touches a prompt,
a contract or a heuristic, the A/B row is the one that matters: see `kn-fe226ab1`,
where prose that every free test approved changed worker behaviour 0/5.

## 3. Never write a bare `#N`

GitHub turns `#2` into a link to pull request 2, whoever's it is. Work orders number
their own items, so "as in #2" in a work order description becomes a link to a
stranger's PR when copied into a PR body. This has already happened once.

| You mean | Write |
|---|---|
| item 2 of the work order | `item 2 of the work order` |
| a real GitHub issue | `issue #133` |
| a real GitHub pull request | `PR #143` |
| a literal string, e.g. a colour | put it in backticks: `` `#1a2b3c` `` |

The hook allows `#N` when `issue`, `issues`, `PR`, `PRs`, `pull request` or
`pull requests` immediately precedes it, and inside code spans and fenced blocks. Every
other bare `#N` is denied.

## 4. Title and footer

Your operating contract already fixes both: the title starts with `[<wo-id>] `, and the
body ends with the Claude Code attribution line from your git briefing. The hook checks
the title separately, so getting the body right does not exempt you from the prefix.
