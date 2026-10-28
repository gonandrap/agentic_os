# Assumptions — issue #1014 port (2026-10-23)

1. **The orphan fix is still wanted.** Commit 344292a5 on `worktree-wo-b289fae8` never reached `main`. Its 33 new tests fail against current `main`, so the defect is still live. I ported the commit rather than redesigning it.
2. **Comments match main's current style.** Main compacted docstrings and comments to one line plus one spec pointer after the fix was written. The ported code and tests were compacted the same way; the spec file keeps the full reasoning.
3. **Only code-landing kinds get the pushed-branch park.** Main added kind contracts (`lands_code`) after the fix. An analyst or investigator with a pushed commit is still refused, because it has no pull-request route. Covered by a new test.
4. **`plan_submit` stays a non-declaring source.** Main added `pr_url_recorded {source: "plan_submit"}` after the fix. `DECLARING_SOURCES` stays `("user", "auto")`, so planner-recorded URLs keep not routing the merge queue.
5. **Main's artifact filter on `dirty` is kept.** `landing.authored` now ignores a kind's own artifact files. `pushed` is computed after that filter.
6. **CLAUDE.md gets one crib-sheet line** for `jarvis wo pr`, not the long block the original commit added, because main's crib sheet is one line per command now.
7. **The auto-attach is pinned to our own branch and SHA.** An automated security review noted that `gh pr list --head` also matches fork pull requests with the same branch name. The daemon now attaches only a same-repository pull request whose head equals the SHA recorded at park time, and only when exactly one matches. Parks written before this change carry no SHA, so they are never auto-attached; `jarvis wo pr` still works for them.
