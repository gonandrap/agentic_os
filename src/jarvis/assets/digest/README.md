# Vendored: the `i-have-adhd` output style

`i-have-adhd.SKILL.md` is a verbatim copy of `skills/i-have-adhd/SKILL.md` from
<https://github.com/ayghri/i-have-adhd> at commit `2d19ad2`, MIT licensed — the licence
sits beside it in `i-have-adhd.LICENSE`. It is read at run time by `jarvis.digest` and
used, unmodified, as the system prompt of the call that shortens an over-long Neo
question for the dashboard.

**It is vendored rather than installed, and it is not under `assets/agents/` or
`assets/skills/`.** Those two trees are `shutil.copytree`d wholesale by
`bootstrap._rebuild` into every project's `.claude/agents/` and `.jarvis/agent-skills/`,
so a file dropped in either becomes a subagent every planner can invoke or a skill every
worker can load. This is neither: it is one prompt, for one internal call, and nothing
outside `jarvis.digest` should see it. `tests/test_digest.py` pins that.

Refresh it by copying the upstream file over `i-have-adhd.SKILL.md` and updating the
commit above. Do not edit it in place — the point of a verbatim copy is that a later
reader can diff it against upstream.
