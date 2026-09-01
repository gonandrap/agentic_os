"""The PR body hook, and the three copies of the section list that must not drift.

Design: docs/superpowers/specs/2026-08-24-a-pull-request-a-reviewer-can-read.md
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from jarvis import hooks

REPO = Path(__file__).resolve().parents[1]
REPO_TEMPLATE = REPO / ".github" / "pull_request_template.md"
SKILL = REPO / "src" / "jarvis" / "assets" / "skills" / "open-a-pull-request"
SKILL_TEMPLATE = SKILL / "pull_request_template.md"

WO = {"JARVIS_WO_ID": "wo-1234abcd"}


def _body(**sections: str) -> str:
    """A body with every required section, overriding the ones named."""
    filled = {
        "Summary": "Adds the thing.",
        "Implementation notes": "- chose X over Y because Z",
        "Questions asked to Neo": "- q164 http://localhost:8787/neo/question/164",
        "Alarms raised": "None.",
        "Learnings": "- kn-abc123 what the next order inherits",
        "Test evidence": (
            "| Kind | Command | Result |\n"
            "| --- | --- | --- |\n"
            "| Unit / integration | `uv run pytest` | 1965 passed |\n"
            "| UI | n/a | no UI change |\n"
            "| Eval | n/a | no prompt change |\n"
            "| A/B | n/a | no contract change |\n"),
    }
    filled.update(sections)
    return "\n".join(f"## {k}\n\n{v}\n" for k, v in filled.items())


def _decide(command: str, cwd: str = "", env: dict[str, str] | None = None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    return hooks.preflight_decision(payload, WO if env is None else env)


# -- the three copies of the section list -------------------------------------------


def test_both_templates_are_byte_identical():
    """The skill ships the template to projects that have none of their own; a drifted
    copy would teach a shape the hook rejects."""
    assert SKILL_TEMPLATE.read_bytes() == REPO_TEMPLATE.read_bytes()


def test_the_template_carries_exactly_the_sections_the_hook_requires():
    headings = [ln[3:].strip() for ln in REPO_TEMPLATE.read_text().splitlines()
                if ln.startswith("## ")]
    assert headings == list(hooks.PR_BODY_SECTIONS)


def test_the_skill_names_the_hook_and_the_bare_ref_rule():
    """A rule enforced only by a hook is a trap: the skill has to state it first."""
    skill = (SKILL / "SKILL.md").read_text()

    assert "denied" in skill
    assert "item 2 of the work order" in skill
    for section in hooks.PR_BODY_SECTIONS:
        assert section in skill


def test_the_skill_and_its_template_reach_a_worker(tmp_path):
    """kn-c9281024: a skill delivered by the wrong mechanism is silently never loaded.
    Workers get `--add-dir <state>/agent-skills`, so both files have to land there."""
    from jarvis.bootstrap import install_agent_assets

    root = install_agent_assets(tmp_path)[0] / ".claude" / "skills" / SKILL.name

    assert (root / "SKILL.md").exists()
    assert (root / "pull_request_template.md").read_bytes() \
        == REPO_TEMPLATE.read_bytes()


def test_the_worker_brief_stops_the_reading_that_produced_pr_143():
    """The concision rule is what got cut too far; it has to name its own floor."""
    from jarvis.worker_brief import concision_section

    text = concision_section()
    assert "open-a-pull-request" in text
    assert "test evidence" in text.lower()


# -- the unfilled template is not a body --------------------------------------------


def test_the_bare_template_fails_every_section():
    problems = hooks.pr_body_problems(REPO_TEMPLATE.read_text())

    assert len(problems) == len(hooks.PR_BODY_SECTIONS)
    for section in hooks.PR_BODY_SECTIONS:
        assert any(section in p for p in problems)


def test_an_unfilled_evidence_table_is_empty_despite_its_header_row():
    """The header and the row labels are template text. Only a filled cell past the
    label counts — without that rule the table always looks answered."""
    table = ("| Kind | Command | Result |\n| --- | --- | --- |\n"
             "| Unit / integration | | |\n| UI | | |\n")

    assert hooks.pr_body_problems(_body(**{"Test evidence": table})) == [
        "`## Test evidence` is still the empty template"]


def test_one_filled_cell_answers_the_table():
    table = ("| Kind | Command | Result |\n| --- | --- | --- |\n"
             "| Unit / integration | | 1965 passed |\n| UI | | |\n")

    assert hooks.pr_body_problems(_body(**{"Test evidence": table})) == []


@pytest.mark.parametrize("content", ["", "\n\n", "-", "<!-- a comment -->", "- \n- \n"])
def test_a_section_holding_only_scaffolding_is_empty(content):
    assert hooks.pr_body_problems(_body(Learnings=content)) == [
        "`## Learnings` is still the empty template"]


def test_the_alarms_section_is_required_like_every_other():
    """§7 of docs/superpowers/specs/2026-08-31-the-supervisor.md. The default in
    `_body()` would turn every test above green without one of them proving the
    section is enforced, so this asserts the deny string directly."""
    body = _body().replace("## Alarms raised", "## Alarms")

    assert hooks.pr_body_problems(body) == ["no `## Alarms raised` section"]


def test_an_order_that_raised_no_alarm_still_answers_the_section():
    """`None.` is content; a blank section is the one the hook sends back."""
    assert hooks.pr_body_problems(_body(**{"Alarms raised": "None."})) == []
    assert hooks.pr_body_problems(_body(**{"Alarms raised": "-"})) == [
        "`## Alarms raised` is still the empty template"]


def test_a_missing_section_is_named():
    body = _body().replace("## Learnings", "## Notes")

    assert "no `## Learnings` section" in hooks.pr_body_problems(body)


def test_a_complete_body_passes():
    assert hooks.pr_body_problems(_body()) == []


# -- the bare `#N` ------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "#2 needs no separate fix",           # verbatim, the PR 143 defect
    "as in #2 of the description",
    "see #143 for context",
])
def test_a_bare_ref_is_caught(text):
    problems = hooks.pr_body_problems(_body(Summary=text))

    assert len(problems) == 1
    assert "GitHub links that to issue/PR" in problems[0]
    # all three ways out, so the retry is one edit and not a guess
    assert "of the work order" in problems[0]
    assert "backticks" in problems[0]


@pytest.mark.parametrize("text", [
    "reported as issue #133",             # deliberate, and reads correctly to a human
    "supersedes PR #143",
    "fixes #133",
    "closes #12",
    "resolved #7",
    "a colour like `#123456`",            # code span — GitHub renders no link
    "```\ndiff --git a#2\n```",           # fenced block, same
    "the thing <!-- #2 -->",              # comment, never rendered at all
    "gonandrap/agentic_os#12",            # a cross-repo ref GitHub resolves correctly
    "https://github.com/o/r/pull/143#2",  # a URL fragment, not a ref
])
def test_a_deliberate_or_unrendered_ref_passes(text):
    assert hooks.pr_body_problems(_body(Summary=text)) == []


def test_blanking_a_code_span_does_not_let_its_last_word_vouch_for_a_later_ref():
    """`issue` inside a code span must not read as the word before a `#N` outside it —
    which is what deleting the span rather than blanking it would do."""
    assert hooks.pr_body_problems(_body(Summary="`issue` #2")) != []


# -- the hook, on real commands -----------------------------------------------------


def test_a_thin_body_is_denied_with_the_fix_named(tmp_path):
    f = tmp_path / "b.md"
    f.write_text("Reported against wo-01d30340. #2 needs no separate fix.\n")

    out = _decide(f'gh pr create --title "[wo-1234abcd] x" --body-file {f}')

    assert out is not None
    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert "`## Test evidence`" in hook["permissionDecisionReason"] \
        or "no `## Test evidence` section" in hook["permissionDecisionReason"]
    assert "open-a-pull-request" in hook["permissionDecisionReason"]


def test_a_body_file_is_resolved_against_the_session_cwd(tmp_path):
    (tmp_path / "b.md").write_text(_body())

    assert _decide('gh pr create --title "[wo-1234abcd] x" --body-file b.md',
                   cwd=str(tmp_path)) is None


@pytest.mark.parametrize("command", [
    'gh pr create --title "[wo-1234abcd] x" --body {body}',
    "gh pr create --title='[wo-1234abcd] x' --body={body}",
    'cd /tmp/wt && /snap/bin/gh pr create -t "[wo-1234abcd] x" -b {body}',
])
def test_an_inline_body_is_read_in_every_flag_spelling(command):
    thin = shlex.quote("## Summary\n\nx\n")

    assert _decide(command.format(body=thin)) is not None
    # ...and the same command with a complete body is not the hook's business
    assert _decide(command.format(body=shlex.quote(_body()))) is None


@pytest.mark.parametrize("command", [
    "gh pr create --fill",                     # no body of ours to judge
    "gh pr create --title '[wo-1234abcd] x'",  # an editor prompt
    "gh pr create --body-file -",              # stdin: a hook cannot see it
    "gh pr create --body-file /no/such/file",  # unreadable is not judgeable
    "gh pr list --state open",                 # not a create
    'git commit -m "gh pr create --body x"',   # the words, not the command
])
def test_the_hook_keeps_out_of_what_it_cannot_read(command):
    assert _decide(command) is None


def test_interactive_sessions_are_untouched():
    """No JARVIS_WO_ID: a human in a managed repo answers to the repo's own template."""
    assert _decide('gh pr create --title "x" --body "thin"', env={}) is None


def test_the_title_rule_is_checked_before_the_body_rule():
    """Both wrong: the title deny is the one that must come back, so a worker fixing
    the body does not then discover the title was also wrong."""
    out = _decide('gh pr create --title "x" --body "thin"')

    assert out is not None
    assert "--title" in out["hookSpecificOutput"]["permissionDecisionReason"]
