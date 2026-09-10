---
name: open-a-pull-request
description: Use when opening a pull request for a work order — before running `gh pr create`. Fills the repository's PR template with the summary, implementation notes, Neo questions, alarms raised, learnings, test evidence and screenshots a reviewer needs, and keeps GitHub from turning a work-order item number into a link to someone else's PR or a screenshot into a broken-image icon.
---

# Opening a pull request

The pull request is the only artifact of your work order that a reviewer reads beside
the diff, and the only one that outlives the OS's records. It is also the one your
operating contract tells you to keep terse — and terse is not the same as thin. The
brief's "a PR body hints, it does not explain" deletes **narration of the diff**. It
does not delete the seven things below, which the diff cannot tell anyone.

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
`pull_request_template.md`, beside this file. Its seven `##` headings are what the hook
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

**Alarms raised** — one bullet per cost alarm the OS raised against this work
order while it was running, each with its `al-` id and the link to its page:

```bash
jarvis alarms --wo $JARVIS_WO_ID
```

```
http://localhost:8787/alarms/<project>/<al-id>
```

Same host and port rule as above. Write `None.` if none was raised — an empty section
reads as a section you forgot, and "nothing burned here" is a fact the reviewer wants
stated rather than inferred.

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

**Screenshots** — one image per thing the change claims to do, for any PR that touches
a rendered surface. A UI change without a screenshot is unreviewed (`kn-c531a831`): the
UI row above says the tests passed, and server-rendered assertions see strings, never
layout — 13 of them passed on the page that wrapped every table row onto three lines.
`scripts/screenshot_config_console.py` is the shape to copy; playwright and chromium are
already installed. Write `None — no rendered surface changed.` when none applies.

## 3. Link every image by raw URL at the commit SHA

`gh` cannot upload an image, so the PNGs are committed (`docs/screenshots/`) and linked.
GitHub resolves a repo-relative path for a **link** and not for an **image**, so
`![x](docs/screenshots/x.png)` is a broken icon in review while `[x](docs/…)` beside it
works — which is why this survives being written and fails in front of the reviewer
(`kn-72cec521`, PR 173). **The hook denies any image target that is not an absolute
`http(s)` URL.**

```
https://raw.githubusercontent.com/<owner>/<repo>/<COMMIT-SHA>/docs/screenshots/<name>.png
```

Pin the SHA, not the branch: a branch URL 404s the moment the branch is deleted after
merge, and the merged record is the one anybody reads later.

Push the branch first — raw.githubusercontent serves from the remote — then verify each
URL before you post the body:

```bash
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' <url>   # 200 image/png
```

## 4. Never write a bare `#N`

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

## 5. Title and footer

Your operating contract already fixes both: the title starts with `[<wo-id>] `, and the
body ends with the Claude Code attribution line from your git briefing. The hook checks
the title separately, so getting the body right does not exempt you from the prefix.
