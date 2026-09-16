"""The gate rule base, and the feedback loop that grows it.

The behaviour under test has two halves that pull against each other, and a test file
that only pins one of them is worse than none. The OS must LEARN — a false positive Neo
has already ruled on must never be re-litigated, in this work order or the next project
— and it must not be able to learn its way into shipping something unreviewed. So every
test that widens the classifier is paired with one that pins what may never widen.

The scenario throughout is the one that produced this work order: issue #104, a `git
commit -F -` whose heredoc body mentions the release script, gated four times on
wo-f49dab38 and dismissed by Neo four times.
"""

from __future__ import annotations

import re
from unittest import mock

import pytest

from jarvis import db, gate_rules, gates
from jarvis.central_store import CentralStore
from jarvis.hooks import preflight_decision
from jarvis.project_store import ProjectStore

ALL_GATES = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))

# The command from issue #104, near enough verbatim: a local commit whose MESSAGE talks
# about the release path. It invokes no release script and ships nothing.
HEREDOC_COMMIT = """git commit -F - <<'EOF'
Reading a release script is not running one

Narrow the recogniser so scripts/shipit.sh is not gated when it is only read.
EOF"""

# The same shape, different words, never reviewed by anybody. This is the one that
# matters: clearing only the exact string reviewed is what the OS already did.
ANOTHER_HEREDOC_COMMIT = """git add -A && git commit -F - <<'EOF'
Document the staged flow in scripts/shipit.sh so the next release is boring
EOF"""

# A heredoc that is NOT prose: the body is a program and `bash` runs it. Structurally
# identical to the case above right up to the pipe, which is the whole point.
HEREDOC_INTO_SHELL = """cat <<'EOF' | bash
scripts/shipit.sh
EOF"""

# A one-line false positive: the release path sits in a quoted argument to a note-taking
# command. Single-line, so it is the only shape a regex exemption may be learned from.
QUOTED_NOTE = 'jarvis learn add "never run scripts/shipit.sh by hand" --topic releases'

# The two defect SHAPES behind the five learned rules that were walkable on 2026-09-12
# (wo-551f5e8c) — a negated class that forgets the newline, and a pattern with no end
# anchor — written out here as fresh patterns over commands nobody ever dismissed.
#
# NOT the rules themselves, and this repository being public is the whole reason. The
# five live patterns and the commands each one cleared are on the work-order record and
# in the retraction reasons (`jarvis gate rules`), which are private state; publishing
# the pair "this rule cleared this command" is the user's call and they have not made it.
# Nothing is lost by the substitution: the floors under `Rule.clears` are indifferent to
# what a pattern says, so a shape-for-shape stand-in exercises the same code identically.
#
# Each entry: the defect, a pattern exhibiting it, the single-line command it was written
# for, and a command it WOULD clear without the fix.
DEFECT_SHAPES = [
    ("a negated class that forgets the newline", "release",
     r"^\s*(?:cat|echo)(?:[^;&|`$]|2>&1)*$",
     "echo hi",
     "echo hi\n./scripts/shipit.sh"),
    ("the same defect on a trailing class after a pipe", "release",
     r"^(?:uv run )?pytest\s+tests/test_x\.py(?:\s*\|\s*(?:head|tail)\b[^;&|]*)?$",
     "uv run pytest tests/test_x.py | tail -20",
     "uv run pytest tests/test_x.py | tail -20\n./scripts/shipit.sh"),
    ("a class excluding parens and the newline alike", "release",
     r"^\s*(cat|head|find)\b[^;&|`$()]*NOTES\.md[^;&|`$()]*$",
     "cat NOTES.md",
     "cat NOTES.md\n./scripts/shipit.sh"),
    ("no end anchor at all, so a plain && walks it", "pr_merge",
     r"^\s*jarvis\s+wo\s+show\s+wo-[0-9a-f]{8}\b",
     "jarvis wo show wo-12345678 --json",
     "jarvis wo show wo-12345678 --json && gh pr merge 210 --squash"),
    ("a whitespace-or-end terminator, unanchored at both ends", "release",
     r"\bls\s+(-[A-Za-z-]+\s+)*docs/notes/?(\s|$)",
     "ls docs/notes",
     "ls docs/notes && ./scripts/shipit.sh"),
]
BYPASS_IDS = [d[0] for d in DEFECT_SHAPES]

# Issue #203, verbatim from `jarvis gate show 95` on prod jarvis-0.9.0: a read-only
# classifier probe, gated as a release and refused 10 minutes later by `sweep_unargued`.
# Everything after `-c` is one double-quoted argument that `python3` reads and no shell
# ever re-parses.
APPROVAL_95 = r'''python3 -c "
import sys; sys.path.insert(0,'src')
from jarvis import gate_rules as g
c=\"git commit -F - <<'EOF'\nmentions scripts/shipit.sh\nEOF\n|| find . -name x\"
for cmd in [c, \"cat <<'EOF' | bash\nscripts/shipit.sh\nEOF\", \"python <<EOF\nrun('scripts/shipit.sh')\nEOF\", 'eval \"bash scripts/shipit.sh\"']:
    s=g.shape_of(cmd,'shipit')
    print(repr(cmd[:40]),'->',s.position, s.owner, sorted(s.names))
"'''


@pytest.fixture()
def central(jarvis_home):
    store = CentralStore()
    yield store
    store.close()


@pytest.fixture()
def rules(central):
    return gate_rules.RuleSet.load(central)


def classify(command, rules, central=None):
    return gates.classify(command, ALL_GATES, rules=rules, central=central)


def learn(central, command, *, exempt_pattern="", kind=None, pattern=None):
    """Do to the rule base what a dismissal does, and return the new rule id."""
    action = classify(command, gate_rules.RuleSet.load(central))
    approval = {"id": 43, "wo_id": "wo-f49dab38", "kind": kind or action.kind,
                "command": command, "matched": pattern or action.matched}
    return gates.learn_from_dismissal(central, approval, "prose, not a release", "neo",
                                      exempt_pattern=exempt_pattern,
                                      project="jarvis_os")


# -- reading a command ----------------------------------------------------------------


def test_heredoc_bodies_are_found_including_the_indented_and_quoted_forms():
    for opener, body in (("<<EOF", "plain"), ("<<-EOF", "dash"), ("<<'EOF'", "quoted"),
                         ('<<"EOF"', "double")):
        command = f"git commit -F - {opener}\n{body} scripts/shipit.sh\nEOF"
        spans = gate_rules.heredoc_spans(command)
        assert len(spans) == 1, command
        start, end, _, terminated = spans[0]
        assert terminated, command
        assert command[start:end].strip() == f"{body} scripts/shipit.sh"


def test_a_here_string_is_not_a_heredoc():
    """`<<<word` has no body. Treating it as one would blank the rest of the command."""
    assert gate_rules.heredoc_spans("bash <<< 'scripts/shipit.sh'") == []


def test_an_unterminated_heredoc_body_runs_to_the_end_and_says_so():
    spans = gate_rules.heredoc_spans("git commit -F - <<EOF\nshipit\n")
    assert len(spans) == 1
    assert spans[0][3] is False


def test_command_names_ignore_heredoc_bodies_and_quoted_text():
    """Every line of a commit message would otherwise read as another command."""
    assert gate_rules.command_names(HEREDOC_COMMIT) == {"git commit"}
    assert gate_rules.command_names(ANOTHER_HEREDOC_COMMIT) == {"git add", "git commit"}
    assert gate_rules.command_names(HEREDOC_INTO_SHELL) == {"cat", "bash"}


def test_list_segments_split_on_lists_and_never_on_a_pipe():
    def parts(command):
        return [command[s:e].strip() for s, e in gate_rules.list_segments(command)]

    assert parts("cat a || find b") == ["cat a", "find b"]
    assert parts("cat a && cat b; cat c & cat d") == ["cat a", "cat b", "cat c", "cat d"]
    assert parts("cat a | bash") == ["cat a | bash"]
    assert parts("cat a 2>&1 | head") == ["cat a 2>&1 | head"]
    # A separator inside a quoted argument or a heredoc body starts no new command — and
    # neither does the opener's own newline. The body belongs to the command that owns
    # it, so a commit message arrives as one span WITH the `git commit` that reads it;
    # splitting there made every line of prose a command of its own (issue #233).
    assert parts("git commit -m 'a && b'") == ["git commit -m 'a && b'"]
    body = "git commit -F - <<'EOF'\nfix a && ship b\nEOF"
    assert parts(body) == [body]
    # The command AFTER the terminator is still a command: the merge is not the message.
    assert parts(body + "\ngh pr merge 1") == [body, "gh pr merge 1"]


def test_shape_reports_where_the_literal_landed():
    assert gate_rules.shape_of(HEREDOC_COMMIT, "shipit").position == gate_rules.HEREDOC
    assert gate_rules.shape_of("./scripts/shipit.sh", "shipit").position == gate_rules.CODE
    quoted = gate_rules.shape_of('eval "echo shipit"', "shipit")
    assert quoted.position == gate_rules.QUOTED


def test_a_literal_in_both_prose_and_code_reads_as_code():
    """The settled convention: if you cannot tell whether the command runs the thing or
    only mentions it, it runs it."""
    command = "git commit -F - <<EOF\nabout scripts/shipit.sh\nEOF\n./scripts/shipit.sh"
    assert gate_rules.shape_of(command, "shipit").position == gate_rules.CODE


# -- the loop closing -----------------------------------------------------------------


def test_the_heredoc_commit_from_issue_104_is_gated_before_anything_is_learned(rules):
    """The bug as filed. This is the state the OS ships in, and it is correct until a
    reviewer has actually ruled on the shape."""
    action = classify(HEREDOC_COMMIT, rules)
    assert action is not None and action.kind == "release"


def test_a_dismissal_clears_the_shape_for_commands_nobody_reviewed(central):
    """The whole work order, in one assertion.

    Before: every commit message mentioning the release path costs a Neo review. After
    one dismissal: none of them do — including this one, which no reviewer ever saw.
    """
    assert classify(ANOTHER_HEREDOC_COMMIT, gate_rules.RuleSet.load(central)) is not None

    learned = learn(central, HEREDOC_COMMIT)

    assert learned["learned"], learned["notes"]
    after = gate_rules.RuleSet.load(central)
    assert classify(HEREDOC_COMMIT, after) is None
    assert classify(ANOTHER_HEREDOC_COMMIT, after) is None


def test_what_was_learned_is_recorded_with_its_provenance(central):
    learn(central, HEREDOC_COMMIT)
    rule = [r for r in central.gate_rules(role="exempt")][0]
    assert rule["source"] == "neo"
    assert rule["approval_id"] == 43
    assert rule["wo_id"] == "wo-f49dab38"
    assert rule["project"] == "jarvis_os"
    assert "prose, not a release" in rule["reason"]


def test_a_learned_rule_crosses_projects(central, tmp_path):
    """Central, not per-project, and this is the reason: the user's complaint was that a
    dismissal settles nothing for the next project."""
    learn(central, HEREDOC_COMMIT)
    other = gate_rules.RuleSet.load(CentralStore())
    assert classify(ANOTHER_HEREDOC_COMMIT, other) is None


def test_an_exemption_that_fires_is_counted(central):
    """A learned rule with no hits generalised nothing. That has to be visible."""
    rule_id = learn(central, HEREDOC_COMMIT)["learned"]
    classify(ANOTHER_HEREDOC_COMMIT, gate_rules.RuleSet.load(central), central=central)
    assert central.get_gate_rule(rule_id)["hits"] == 1


# -- what learning may never do -------------------------------------------------------


def test_learning_the_commit_shape_does_not_clear_a_heredoc_piped_into_a_shell(central):
    """The bypass this design exists to survive.

    `git commit <<EOF … shipit … EOF` and `cat <<EOF | bash … shipit … EOF` differ only
    in what consumes the body. If the exemption learned from the first cleared the
    second, the feedback loop would be a hole in the gate rather than a fix for it.
    """
    learn(central, HEREDOC_COMMIT)
    after = gate_rules.RuleSet.load(central)
    assert classify(HEREDOC_INTO_SHELL, after) is not None
    assert classify("./scripts/shipit.sh", after) is not None
    assert classify("gh pr merge 31", after) is not None


def test_a_literal_in_executable_position_can_never_be_learned(central):
    """No dismissal, however well-argued, generalises a real release into a rule."""
    result = learn(central, "./scripts/shipit.sh")
    assert result["learned"] is None
    assert "executable position" in " ".join(result["notes"])
    assert classify("./scripts/shipit.sh", gate_rules.RuleSet.load(central)) is not None


def test_every_canary_still_gates_after_learning(central):
    learn(central, HEREDOC_COMMIT)
    assert gate_rules.RuleSet.load(central).check_canaries() == []


def test_a_chain_containing_an_executor_is_never_exemptible():
    for command in (HEREDOC_INTO_SHELL,
                    "python <<EOF\nrun('scripts/shipit.sh')\nEOF",
                    'eval "bash scripts/shipit.sh"'):
        shape = gate_rules.shape_of(command, "shipit")
        assert shape is None or not shape.exemptible, command


def test_an_executor_past_a_list_separator_leaves_the_shape_exemptible():
    """Issue #194, in the generalisation logic rather than the match: `find` cannot reach
    a heredoc body it is not in, so a dismissal of this shape still has something to
    teach. Paired with the test above, which is the same sentence about a pipe."""
    guarded = HEREDOC_COMMIT + "\n|| find . -name TODO"
    shape = gate_rules.shape_of(guarded, "shipit")
    assert shape.names == {"git commit"}
    assert shape.exemptible and shape.unlearnable_reason() == ""

    piped = gate_rules.shape_of(HEREDOC_INTO_SHELL, "shipit")
    assert "bash" in piped.names
    assert piped.unlearnable_reason() == "its own command executes via bash"


# -- the reviewer's own generalisation ------------------------------------------------


def test_a_sound_reviewer_pattern_is_used_in_preference_to_the_structural_one(central):
    result = learn(central, QUOTED_NOTE, kind="release", pattern="shipit",
                   exempt_pattern=r'^jarvis learn add "[^"]*" --topic [a-z]+$')
    rule = central.get_gate_rule(result["learned"])
    assert rule["test"] == "regex"
    assert classify(QUOTED_NOTE, gate_rules.RuleSet.load(central)) is None


@pytest.mark.parametrize("pattern,why", [
    (".*", "no literal anchor"),
    ("[a-z]+", "no literal anchor"),
    ("(", "does not compile"),
    ("something else entirely", "does not match the command"),
])
def test_an_unsound_reviewer_pattern_is_refused(pattern, why):
    assert why in gate_rules.validate_pattern(pattern, HEREDOC_COMMIT)


def test_a_reviewer_pattern_that_would_clear_a_real_release_is_refused(central):
    """The backstop. A reviewer that proposes `shipit` as the exemption has described
    every release there is; the OS declines and falls back to the structural rule."""
    result = learn(central, HEREDOC_COMMIT, exempt_pattern="shipit")

    assert "refused" in " ".join(result["notes"])
    rule = central.get_gate_rule(result["learned"])
    assert rule["test"] == "signature"
    assert classify("./scripts/shipit.sh", gate_rules.RuleSet.load(central)) is not None


def test_a_multi_line_command_never_teaches_a_regex(central):
    """Item 4 of wo-551f5e8c. A regex cannot say which line of a script it describes, so
    a multi-line dismissal falls to the structural rule — the path a heredoc was always
    meant to take — however sound the reviewer's pattern looks."""
    result = learn(central, HEREDOC_COMMIT,
                   exempt_pattern=r"git commit -F - <<'?EOF'?[\s\S]*shipit")

    assert central.get_gate_rule(result["learned"])["test"] == "signature"
    assert "more than one line" in " ".join(result["notes"])
    # The dismissal still buys what it was supposed to buy.
    assert classify(HEREDOC_COMMIT, gate_rules.RuleSet.load(central)) is None


def test_an_exemption_describing_only_a_prefix_is_refused():
    """The defect that needed no newline: the pattern described a harmless prefix and
    said nothing about the `&& gh pr merge` chained after it."""
    _, _, pattern, _, walked = DEFECT_SHAPES[3]
    assert "whole command" in gate_rules.validate_pattern(pattern, walked)


@pytest.mark.parametrize("label,kind,pattern,original,walked", DEFECT_SHAPES,
                         ids=BYPASS_IDS)
def test_every_bypass_shape_is_refused_at_learn_time(label, kind, pattern, original,
                                                     walked):
    """Each shape fails validation against the single-line command it was written for,
    so no pattern of that shape can enter the base again."""
    assert gate_rules.validate_pattern(pattern, original) != "", label


@pytest.mark.parametrize("label,kind,pattern,original,walked", DEFECT_SHAPES,
                         ids=BYPASS_IDS)
def test_a_stored_bypass_rule_no_longer_clears_what_it_would_have(label, kind, pattern,
                                                                  original, walked):
    """The regression proper, and it is deliberately blind to validation: the five real
    rules were already IN the base, admitted before any of these checks existed.
    `Rule.clears` is the floor that holds for a stored rule nobody can re-validate."""
    rule = gate_rules.Rule(id="gr-stored", role=gate_rules.EXEMPT,
                           test=gate_rules.REGEX, pattern=pattern, kind=kind,
                           source="neo")
    base = gate_rules.RuleSet.from_seeds().with_rule(rule)

    # It still clears the command it legitimately describes...
    assert base.decide(original, gate_rules.KIND_NAMES) is not None
    # ...and no longer clears the one it had no business clearing.
    assert base.decide(walked, gate_rules.KIND_NAMES).match is not None, label


def test_a_regex_exemption_never_clears_a_command_it_does_not_cover_entirely():
    """The two floors under `Rule.clears`, stated directly."""
    rule = gate_rules.Rule(id="gr-x", role=gate_rules.EXEMPT, test=gate_rules.REGEX,
                           pattern=r"^echo hi$", kind="release", source="neo")

    assert rule.clears("echo hi", "release", "shipit")
    assert not rule.clears("echo hi\n./scripts/shipit.sh", "release", "shipit")
    assert not rule.clears("echo hi && ./scripts/shipit.sh", "release", "shipit")


def test_a_structural_exemption_still_clears_the_multi_line_shape_it_is_for():
    """The newline rule is scoped to regex exemptions on purpose: a heredoc IS multi-line,
    and clearing that shape is what the signature path exists for (kn-0b2fdebb)."""
    rule = gate_rules.Rule(
        id="gr-y", role=gate_rules.EXEMPT, test=gate_rules.SIGNATURE, kind="release",
        pattern='{"kind": "release", "owner": "git commit", "position": "heredoc"}')

    assert rule.clears(HEREDOC_COMMIT, "release", "shipit")


# Refusing a regex for a multi-line command makes the signature the ONLY route by which
# one is ever cleared, so it carries the whole weight and needs the negative half. The
# last two of these were real: both cleared the appended release before wo-551f5e8c
# review round 1, because the delimiter line was not a delimiter line and the body ran
# to the end of the string.
SIGNATURE_MUST_REFUSE = [
    ("a release appended below the terminator", HEREDOC_COMMIT + "\n./scripts/shipit.sh"),
    ("a blank line before it", HEREDOC_COMMIT + "\n\n./scripts/shipit.sh"),
    ("chained onto the terminator line", HEREDOC_COMMIT + " && ./scripts/shipit.sh"),
    ("an unterminated body, so nothing below it is prose",
     "git commit -F - <<'EOF'\nmentions scripts/shipit.sh\n./scripts/shipit.sh"),
    ("a second heredoc piped into a shell",
     HEREDOC_COMMIT + "\ncat <<'EOF2' | bash\nscripts/shipit.sh\nEOF2"),
]


@pytest.mark.parametrize("label,command", SIGNATURE_MUST_REFUSE,
                         ids=[c[0] for c in SIGNATURE_MUST_REFUSE])
def test_a_structural_exemption_refuses_a_release_the_heredoc_does_not_contain(label,
                                                                              command):
    rule = gate_rules.Rule(
        id="gr-y", role=gate_rules.EXEMPT, test=gate_rules.SIGNATURE, kind="release",
        pattern='{"kind": "release", "owner": "git commit", "position": "heredoc"}')

    assert not rule.clears(command, "release", "shipit"), label
    assert gate_rules.RuleSet.from_seeds().with_rule(rule).decide(
        command, gate_rules.KIND_NAMES).match is not None, label


def test_no_rule_in_the_base_clears_a_multi_line_command_with_a_gated_verb_below():
    """Item 2's acceptance test, swept rather than sampled: every reader the classifier
    knows, above every command that must always gate."""
    readers = ["cat README.md", "ls -la", "echo hi", "head -5 README.md",
               "sed -n '1,5p' README.md", "git ls-files", "tail -3 README.md",
               "grep -rn shipit src/", "uv run pytest tests/test_x.py",
               "cat NOTES.md", "ls docs/notes",
               'jarvis wo show wo-12345678 --json']
    base = gate_rules.RuleSet.from_seeds()
    for i, (_, _, pattern, _, _) in enumerate(DEFECT_SHAPES):
        base = base.with_rule(gate_rules.Rule(
            id=f"gr-shape{i}", role=gate_rules.EXEMPT, test=gate_rules.REGEX,
            pattern=pattern, kind="", source="neo"))
    # The signature path belongs in the sweep too: once a regex may not be learned from a
    # multi-line command it is the only way one is ever cleared, so it carries the weight.
    base = base.with_rule(gate_rules.Rule(
        id="gr-sig", role=gate_rules.EXEMPT, test=gate_rules.SIGNATURE, kind="release",
        pattern='{"kind": "release", "owner": "git commit", "position": "heredoc"}'))

    canaries = base.canaries()
    probed = 0
    for reader in readers:
        for canary in canaries:  # every canary, multi-line ones included
            for joiner in ("\n", " && ", "; "):
                command = f"{reader}{joiner}{canary.pattern}"
                assert base.decide(command, gate_rules.KIND_NAMES).match is not None, \
                    command
                probed += 1

    # A heredoc can only be joined with a newline: `&&` after it lands on the TERMINATOR
    # line, which is inside the body rather than below it — see the test below.
    for canary in canaries:
        command = f"{HEREDOC_COMMIT}\n{canary.pattern}"
        assert base.decide(command, gate_rules.KIND_NAMES).match is not None, command
        probed += 1

    assert probed == len(readers) * len(canaries) * 3 + len(canaries)
    assert probed >= 12 * 24 * 3 + 24, f"the sweep shrank to {probed} probes"


def test_delimiter_reuse_re_terminates_the_outer_heredoc_and_the_shell_agrees():
    """The one combination the sweep above must NOT assert on, and why.

    Appending `&& cat <<'EOF' | bash ... EOF` to a heredoc does not chain a second
    command: the `&&` lands on a line that is not the delimiter, and the appended block's
    own `EOF` closes the OUTER heredoc. Everything is one commit message. Verified
    against real bash on 2026-09-12 — the payload never ran — so clearing it is correct,
    and a future change that makes this gate is over-gating, not a fix.
    """
    command = f"{HEREDOC_COMMIT} && cat <<'EOF' | bash\nscripts/shipit.sh\nEOF"
    shape = gate_rules.shape_of(command, "shipit")

    assert shape is not None and shape.position == gate_rules.HEREDOC
    assert shape.owner == "git commit"
    # The `&&` with a SINGLE-line release does chain, and that one must gate: no
    # trailing delimiter, so the body is unterminated and nothing in it is prose.
    assert gate_rules.shape_of(f"{HEREDOC_COMMIT} && ./scripts/shipit.sh",
                               "shipit").position == gate_rules.CODE


def test_an_empty_reviewer_pattern_is_not_a_pattern(central):
    """`exempt_pattern: ""` must never reach the rule base as a regex matching
    everything."""
    result = learn(central, HEREDOC_COMMIT, exempt_pattern="")
    assert central.get_gate_rule(result["learned"])["test"] == "signature"


# -- the safety net itself -------------------------------------------------------------


def test_every_gate_kind_has_a_multi_line_canary():
    """Item 3. Until wo-551f5e8c every canary was one line, so `jarvis gate rules` could
    report `every command that must gate still gates` over an open release gate: the set
    could not express the shape gr-391ba702 cleared."""
    seeds = gate_rules.RuleSet.from_seeds()

    def is_reader_then_gated(command: str) -> bool:
        """Exactly two lines: something that can only read, then something that gates."""
        head, sep, tail = command.partition("\n")
        return bool(sep) and "\n" not in tail and gate_rules.reads_only(head) and \
            seeds.decide(tail, gate_rules.KIND_NAMES).match is not None

    every = [(k, c) for k, c in gate_rules.SEED_CANARIES if "\n" in c]
    added = [(k, c) for k, c in every if is_reader_then_gated(c)]

    # kn-e74988af: the filter must be shown to have EXCLUDED something, or a later change
    # deleting the rows it was written for still passes on whatever else matches. Other
    # multi-line canaries exist and are deliberately NOT of this shape — the heredoc
    # piped into a shell, the `eval`, the issue #203 `python3 -c` probe.
    assert len(added) < len(every), "the other multi-line canaries have gone"
    # `self_heal` is excluded throughout: its command is a rendered intent string rather
    # than a shell command, so it has no recogniser and no canary (kn-832cb8cb).
    #
    # `auto_merge` is excluded for the neighbouring reason and NOT the same one, which is
    # worth spelling out because its command IS a real shell command: the OS builds and
    # runs it. What it is not is a command any WORKER can type — nothing classifies into
    # the kind, by design, so there is no attempt for a recogniser to catch and a canary
    # would be asserting that a string gates into a kind it must never gate into.
    # docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §8.
    assert {k for k, _ in added} == set(gates.KIND_NAMES) - {gate_rules.SELF_HEAL,
                                                             gate_rules.AUTO_MERGE}


def test_the_multi_line_canary_report_fails_on_the_shape_that_walked_the_gate():
    """The canary set earns its keep only if it is falsifiable. With the newline-blind
    shape back in the base and the `Rule.clears` floor removed, the release canary must
    go red — which is what the report could not say before wo-551f5e8c."""
    rule = gate_rules.Rule(id="gr-stored", role=gate_rules.EXEMPT,
                           test=gate_rules.REGEX, pattern=DEFECT_SHAPES[0][2],
                           kind="release", source="neo")
    unguarded = gate_rules.RuleSet.from_seeds().with_rule(rule)

    # `clears` is what the fix hardened. Restore its pre-fix body verbatim — an
    # unanchored `search` over the raw command — to recreate the state of the day.
    def unhardened(self, command, kind, _pattern):
        if self.role != gate_rules.EXEMPT or (self.kind and self.kind != kind):
            return False
        return bool(re.search(self.pattern, command, re.IGNORECASE))

    with mock.patch.object(gate_rules.Rule, "clears", unhardened):
        failures = unguarded.check_canaries()
    assert ["echo hi\n./scripts/shipit.sh"] == [f["command"] for f in failures]

    assert unguarded.check_canaries() == []


def test_the_newline_floor_refuses_to_run_with_nothing_to_probe():
    """A floor that tested nothing would report every pattern sound — this module's own
    green-over-an-open-gate failure, one level down. It must fail loudly instead."""
    with mock.patch.object(gate_rules, "SEED_CANARIES",
                           (("release", "cat <<'EOF' | bash\nscripts/shipit.sh\nEOF"),)):
        with pytest.raises(AssertionError, match="single-line canary"):
            gate_rules.validate_pattern(r"^echo hi$", "echo hi")


def test_a_trailing_newline_does_not_decide_whether_an_exemption_applies():
    """`"\\n" in command` and `fullmatch` must read the same string, or whitespace the
    user never typed silently voids a rule."""
    rule = gate_rules.Rule(id="gr-z", role=gate_rules.EXEMPT, test=gate_rules.REGEX,
                           pattern=r"^echo hi$", kind="release", source="neo")

    for command in ("echo hi", "echo hi\n", "  echo hi  \n\n", "\necho hi"):
        assert rule.clears(command, "release", "shipit"), repr(command)
    assert gate_rules.validate_pattern(r"^echo hi$", "echo hi\n") == ""


def test_no_seeded_pattern_can_reach_across_a_newline():
    """The audit item 2 asks for, over the patterns the OS itself ships. A negated class
    that forgets `\\n` is the whole defect; `[^\\n]*` is the form that does not have it."""
    classes = re.compile(r"\[\^([^\]]*)\]")
    for kind, pattern in gate_rules.SEED_MATCHES:
        for body in classes.findall(pattern):
            assert r"\n" in body, f"{kind}: {pattern}"
        assert not pattern.startswith("^"), f"{kind}: {pattern}"
        assert not pattern.endswith("$"), f"{kind}: {pattern}"


def test_check_canaries_catches_a_retracted_recogniser(central):
    """The other way a gate goes quiet: not a bad exemption, a missing recogniser."""
    shipit = [r for r in central.gate_rules(role="match")
              if r["pattern"] == "shipit"][0]
    central.retract_gate_rule(shipit["id"], "testing")

    failures = gate_rules.RuleSet.load(central).check_canaries()

    assert any("shipit" in f["command"] for f in failures)


def test_the_invariant_reports_a_disarmed_gate(central):
    from jarvis.invariants import check_gate_canaries

    assert list(check_gate_canaries()) == []
    shipit = [r for r in central.gate_rules(role="match")
              if r["pattern"] == "shipit"][0]
    central.retract_gate_rule(shipit["id"], "testing")

    violations = list(check_gate_canaries())

    assert violations and violations[0].invariant == "INV-GATE-CANARY"
    assert "jarvis gate rules" in violations[0].detail


def test_retracting_an_exemption_re_arms_the_gate(central):
    rule_id = learn(central, HEREDOC_COMMIT)["learned"]
    assert classify(HEREDOC_COMMIT, gate_rules.RuleSet.load(central)) is None

    central.retract_gate_rule(rule_id, "the user disagreed")

    assert classify(HEREDOC_COMMIT, gate_rules.RuleSet.load(central)) is not None


def test_seeding_is_idempotent_and_does_not_resurrect_a_retracted_rule(jarvis_home):
    first = CentralStore()
    shipit = [r for r in first.gate_rules(role="match") if r["pattern"] == "shipit"][0]
    first.retract_gate_rule(shipit["id"], "deliberate")
    seeded = len(first.gate_rules(include_retired=True))
    first.close()

    # Re-opening runs the seeder again, as every `jarvis` command does.
    second = CentralStore()
    try:
        assert len(second.gate_rules(include_retired=True)) == seeded
        assert second.get_gate_rule(shipit["id"])["retired_at"] is not None
    finally:
        second.close()


def test_the_seed_fallback_restores_recognisers_and_no_exemptions():
    """What an unreadable `os.db` leaves the hook with. It must err toward gating."""
    seeds = gate_rules.RuleSet.from_seeds()
    assert seeds.matchers()
    assert seeds.exemptions() == ()
    assert classify("./scripts/shipit.sh", seeds) is not None


# -- the invoker test is positional (issue #203) --------------------------------------


def test_a_shell_invoker_named_inside_a_quoted_payload_does_not_disarm_blanking():
    """Approval 95, verbatim: a read-only probe gated as a release.

    Its payload names a shell-invoker keyword at offset 280 and the release script a few
    characters later, both only as Python string literals. Searching the raw string for
    the invoker matched the first, `scannable` handed the command back whole, and the
    second then tripped the release gate on a command that runs nothing.
    """
    assert gate_rules.scannable(APPROVAL_95) != APPROVAL_95
    assert "shipit" not in gate_rules.scannable(APPROVAL_95)
    assert classify(APPROVAL_95, gate_rules.RuleSet.from_seeds()) is None


def test_a_shell_invoker_in_executable_position_still_scans_the_command_whole():
    """The pairing. Positional means the keyword still counts where the shell reaches
    it — quoting the payload of something that re-parses it buys nothing."""
    for command in ('sh -c "scripts/shipit.sh"',
                    'eval "bash scripts/shipit.sh"',
                    'echo x | xargs "./scripts/shipit.sh"'):
        assert gate_rules.scannable(command) == command, command
        assert classify(command, gate_rules.RuleSet.from_seeds()) is not None, command


@pytest.mark.parametrize("command", [
    'ssh prod "bash scripts/shipit.sh"',
    '"bash" -c "gh pr merge 31"',
])
def test_a_wrapper_whose_payload_names_no_invoker_was_never_gated(command):
    """These two look like the positional test lost them. It did not: neither ever
    gated. `_SHELL_INVOKER` wants `sh` then whitespace then a `-…c` flag, so bare
    `bash <path>` never matched it, and in `"bash" -c` a quote sits where the
    whitespace has to be. Pinned so the next reader does not re-file them as a
    regression — the real one is below."""
    assert classify(command, gate_rules.RuleSet.from_seeds()) is None


@pytest.mark.parametrize("command", [
    'echo "$(scripts/shipit.sh)"',
    'echo "$(eval scripts/shipit.sh)"',
    'git commit -m "`scripts/shipit.sh`"',
    'diff <(scripts/shipit.sh) old.txt',
])
def test_a_substitution_gates_however_it_is_quoted(command):
    """`$(…)`, backticks and `<(…)` RUN inside double quotes, so blanking the span they
    sit in would hide code. `scannable` therefore tests `_SUBSTITUTION` before blanking,
    the same way `reads_only` always has — the second of these regressed in review
    round 1, when only `reads_only` made that call.

    The cost is that prose quoting a literal `$(` gates. That is the loud failure and it
    is the one to prefer: it costs a review, where the silent one ships.
    """
    assert classify(command, gate_rules.RuleSet.from_seeds()) is not None


@pytest.mark.parametrize("command", [
    'ssh prod "sh -c \'scripts/shipit.sh\'"',
    'ssh prod "eval scripts/shipit.sh"',
    'docker exec c "bash -c \'scripts/shipit.sh\'"',
])
def test_a_wrapper_that_executes_its_quoted_payload_is_a_known_miss(command):
    """Issue #213, asserted as the miss it is rather than left unexamined.

    `ssh` and `docker exec` run their quoted payload, and neither is a shell invoker by
    this module's definition — so blanking deletes the gated literal and the invoker
    naming it together. Unlike substitution, which needed no new vocabulary and is fixed
    above, this needs a notion of "wrapper that executes its quoted argument".

    The hole predates the positional test: the same wrappers with a plain payload, below,
    never gated either. What changed is that the raw search used to catch the subset
    whose payload happened to spell one of the three keywords.

    **When #213 lands these assertions INVERT — they do not get deleted.** Each of these
    commands must then gate, and this test is the list of what the fix has to catch.
    """
    assert classify(command, gate_rules.RuleSet.from_seeds()) is None


@pytest.mark.parametrize("command", [
    'ssh prod "scripts/shipit.sh"',
    'ssh prod "gh pr merge 31"',
    'docker exec c "scripts/shipit.sh"',
])
def test_the_wrapper_miss_is_older_than_the_positional_test(command):
    """The control for the case above, and the reason it is a pre-existing hole rather
    than one this fix opened: with no keyword in the payload there was nothing for the
    raw search to catch, and these did not gate before the change either.

    **Inverted by #213 too, not deleted** — a remote shell handed the release script is
    the plainest case the fix must catch, and it is the one that never gated at all.
    """
    assert classify(command, gate_rules.RuleSet.from_seeds()) is None


@pytest.mark.parametrize("command", [
    'ssh prod bash scripts/shipit.sh',
    'ssh prod sh -c scripts/shipit.sh',
])
def test_an_unquoted_wrapper_payload_still_gates(command):
    """The boundary of that miss: nothing is blanked, so the literal is in plain sight
    and the gate fires. Only the quoting hides it."""
    assert classify(command, gate_rules.RuleSet.from_seeds()) is not None


def test_a_reader_whose_argument_names_a_shell_invoker_still_only_reads():
    """`reads_only` ran the same raw search, so a note about a shell cost a reader its
    exemption. The exemption cannot widen: an invoker the shell reaches is never a
    reader's argv0, so the test is belt-and-braces either way."""
    assert gate_rules.reads_only('grep -n "eval" src/jarvis/gate_rules.py')
    assert not gate_rules.reads_only('cat notes.md | xargs ./scripts/shipit.sh')


def test_a_heredoc_handed_to_an_interpreter_is_a_canary():
    """Approval 96, the conservative TRUE positive the fix must not turn into a miss:
    a heredoc body is a program to `python3`, so no learned rule may ever clear it.

    Pinned as a canary because `Shape.exemptible` already says so via `_EXECUTORS`, and a
    property nothing tests is a property the next edit can drop.
    """
    command = "python3 - <<'PY'\nscripts/shipit.sh\nPY"
    assert command in [c for _, c in gate_rules.SEED_CANARIES]
    assert classify(command, gate_rules.RuleSet.from_seeds()) is not None
    shape = gate_rules.shape_of(command, "shipit")
    assert shape.position == gate_rules.HEREDOC and not shape.exemptible


def test_learning_the_commit_shape_does_not_clear_an_interpreter_heredoc(central):
    learn(central, HEREDOC_COMMIT)
    after = gate_rules.RuleSet.load(central)
    assert after.check_canaries() == []
    assert classify("python3 - <<'PY'\nscripts/shipit.sh\nPY", after) is not None


# -- the known holes this mechanism now covers ----------------------------------------


def test_the_eval_in_prose_hole_is_closed(central):
    """kn-1ecbbff2: the bare word `eval` anywhere used to turn off quote-blanking, so a
    summary reporting "eval scorecard 36/36" and naming a gated verb was scanned as code
    and gated. It cost a review every time, and no dismissal was needed to settle it —
    the word is inside the quoted argument, where nothing re-parses it (issue #203).
    """
    command = ('jarvis wo finish wo-1 --summary "eval scorecard 36/36; '
               'do not gh pr merge until reviewed"')
    assert classify(command, gate_rules.RuleSet.load(central)) is None
    # …and the same words with nothing quoting them are still a merge.
    assert classify("gh pr merge 31", gate_rules.RuleSet.load(central)) is not None


# -- end to end, through the hook and a verdict ---------------------------------------


@pytest.fixture()
def gated(jarvis_home, project):
    store = ProjectStore(project)
    wo = store.create_work_order("fix the gate", description="issue 104")
    store.set_status(wo["id"], "running")
    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT": "proj_a",
           "JARVIS_PROJECT_PATH": str(project), "JARVIS_GATES": ALL_GATES.to_json()}

    class Handle:
        def __init__(self):
            self.store, self.wo, self.env = store, wo, env

        def attempt(self, command):
            return preflight_decision(
                {"tool_name": "Bash", "tool_input": {"command": command},
                 "cwd": str(project)}, env)

    yield Handle()
    store.close()


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


def test_a_dismissal_through_the_hook_stops_the_next_worker_being_blocked(gated):
    """The four-false-positives-in-one-work-order scenario, played forwards.

    The first commit is blocked and reviewed. The second — different message, same shape,
    the commit that would have been gate 41 — is never blocked at all.
    """
    assert _decision(gated.attempt(HEREDOC_COMMIT)) == "deny"
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="prose in a commit message", decided_by="neo",
                         project="proj_a")

    assert _decision(gated.attempt(ANOTHER_HEREDOC_COMMIT)) is None
    assert len(gated.store.list_approvals(gated.wo["id"])) == 1


def test_the_worker_is_told_what_the_os_learned(gated):
    gated.attempt(HEREDOC_COMMIT)
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="prose", decided_by="neo", project="proj_a")

    message = gated.store.queued_messages(gated.wo["id"])[0]["content"]
    assert "The OS learned from this" in message
    assert "jarvis gate rules" in message


def test_the_timeline_records_what_was_learned(gated):
    gated.attempt(HEREDOC_COMMIT)
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="prose", decided_by="neo", project="proj_a")

    event = [e for e in gated.store.list_events(gated.wo["id"])
             if e["kind"] == "gate_dismissed"][0]
    payload = db.from_json(event["payload"])
    assert payload["learned_rule"].startswith("gr-")


def test_a_dismissal_that_teaches_nothing_says_so(gated):
    """A real release dismissed by mistake must not silently look like a learned rule."""
    gated.attempt("./scripts/shipit.sh")
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="mistaken", decided_by="user", project="proj_a")

    event = [e for e in gated.store.list_events(gated.wo["id"])
             if e["kind"] == "gate_dismissed"][0]
    payload = db.from_json(event["payload"])
    assert payload["learned_rule"] is None
    assert payload["learn_notes"]
    message = gated.store.queued_messages(gated.wo["id"])[0]["content"]
    assert "could not generalise" in message
