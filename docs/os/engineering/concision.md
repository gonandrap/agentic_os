---
type: Jarvis Engineering Guide
title: Concision
description: What a code comment, a PR body and a message to the record may contain, and how the OS enforces it.
resource: ../../../.serena/memories/concision-enforcement.md
tags: [concision, house style]
---
# Concision

- Worker rules: `jarvis brief concision`; house style: the `caveman` and `ponytail` skills.
- Reviewers (validation seats, Neo's assumption review): `concision.REVIEW_SCOPE`, ponytail's scope rules.
- CI: `scripts/trim_comments.py --check` fails on a comment or docstring past one line, and also on a trailing period it would strip. It names each file as `OVERLONG <path>`.
- A worker's `git push` is denied before that CI round when the check fails: `hooks.overlong_comment_decision`.
- Enforcement: Serena `read_memory concision-enforcement`.
