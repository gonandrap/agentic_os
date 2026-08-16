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
        start, end, _ = spans[0]
        assert command[start:end].strip() == f"{body} scripts/shipit.sh"


def test_a_here_string_is_not_a_heredoc():
    """`<<<word` has no body. Treating it as one would blank the rest of the command."""
    assert gate_rules.heredoc_spans("bash <<< 'scripts/shipit.sh'") == []


def test_an_unterminated_heredoc_body_runs_to_the_end():
    spans = gate_rules.heredoc_spans("git commit -F - <<EOF\nshipit\n")
    assert len(spans) == 1


def test_command_names_ignore_heredoc_bodies_and_quoted_text():
    """Every line of a commit message would otherwise read as another command."""
    assert gate_rules.command_names(HEREDOC_COMMIT) == {"git commit"}
    assert gate_rules.command_names(ANOTHER_HEREDOC_COMMIT) == {"git add", "git commit"}
    assert gate_rules.command_names(HEREDOC_INTO_SHELL) == {"cat", "bash"}


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


# -- the reviewer's own generalisation ------------------------------------------------


def test_a_sound_reviewer_pattern_is_used_in_preference_to_the_structural_one(central):
    result = learn(central, HEREDOC_COMMIT,
                   exempt_pattern=r"git commit -F - <<'?EOF'?[\s\S]*shipit")
    rule = central.get_gate_rule(result["learned"])
    assert rule["test"] == "regex"
    assert classify(HEREDOC_COMMIT, gate_rules.RuleSet.load(central)) is None


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


def test_an_empty_reviewer_pattern_is_not_a_pattern(central):
    """`exempt_pattern: ""` must never reach the rule base as a regex matching
    everything."""
    result = learn(central, HEREDOC_COMMIT, exempt_pattern="")
    assert central.get_gate_rule(result["learned"])["test"] == "signature"


# -- the safety net itself -------------------------------------------------------------


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


# -- the known holes this mechanism now covers ----------------------------------------


def test_the_eval_in_prose_hole_becomes_learnable(central):
    """kn-1ecbbff2: the bare word `eval` anywhere turns off quote-blanking, so a summary
    reporting "eval scorecard 36/36" and naming a gated verb is scanned as code.

    Still gated on a first encounter — `scannable` cannot tell the two apart — but the
    shape is now something a dismissal can settle for good, which is the difference
    between a defect and a defect that keeps costing reviews.
    """
    command = ('jarvis wo finish wo-1 --summary "eval scorecard 36/36; '
               'do not gh pr merge until reviewed"')
    action = classify(command, gate_rules.RuleSet.load(central))
    assert action is not None  # the hole, unchanged

    learn(central, command)

    assert classify(command, gate_rules.RuleSet.load(central)) is None
    # …and it did not become a licence to merge.
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
