"""Privileged-action gates: a worker may attempt to ship, under independent review.

The behaviour under test is a security boundary with an unusual failure mode — it is
*supposed* to block, so a broken gate that blocks everything looks fine, and a broken
gate that allows everything also looks fine until something ships that shouldn't. So
these tests pin both directions: what must be gated, and what must never be.
"""

from __future__ import annotations

import json

import pytest

from jarvis import gates
from jarvis.hooks import preflight_decision
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

ALL_GATES = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))


def _decision(result):
    """The permissionDecision a hook result carries, or None for 'no opinion'."""
    if result is None:
        return None
    return result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


# -- classification ------------------------------------------------------------------


@pytest.mark.parametrize("command,kind", [
    ("gh pr merge 31 --squash --delete-branch", "pr_merge"),
    ("gh pr merge --auto", "pr_merge"),
    ("gh api --method PUT repos/o/r/pulls/31/merge", "pr_merge"),
    ("./scripts/shipit.sh", "release"),
    ("bash scripts/shipit.sh --dry-run", "release"),
    ("gh release create jarvis-1.2.3", "release"),
    ("npm publish", "release"),
    ("uv publish", "release"),
    ("git push --follow-tags", "release"),
    ("systemctl --user restart jarvisd", "service_restart"),
    ("sudo systemctl stop jarvis-ui", "service_restart"),
    ("git push origin main", "push_protected"),
    ("git push --force origin master", "push_protected"),
    ("git push origin HEAD:refs/heads/main", "push_protected"),
])
def test_privileged_commands_are_recognised(command, kind):
    action = gates.classify(command, ALL_GATES)
    assert action is not None, f"{command!r} should have tripped the {kind} gate"
    assert action.kind == kind


@pytest.mark.parametrize("command", [
    # Ordinary work must never need a review. Every one of these is something a worker
    # does many times per work order; gating any of them would make the OS unusable.
    "git push -u origin wo-abc123",
    "git push --force-with-lease origin my-feature",
    "gh pr create --draft --title x",
    "gh pr view 31",
    "gh pr checks 31",
    "npm start",
    "npm run build",
    "uv run pytest tests/",
    "git commit -m 'main entry point'",
    "git merge --abort",
    "grep -rn 'systemctl' docs/",
    "cat scripts/README.md",
])
def test_ordinary_work_is_not_gated(command):
    assert gates.classify(command, ALL_GATES) is None, f"{command!r} must not be gated"


@pytest.mark.parametrize("command", [
    # Naming a privileged action is not performing one. Every one of these was gated
    # before `scannable()` existed, and each cost a Neo review and stalled the worker:
    # two reached the user as escalations from wo-6e7caf6c.
    'jarvis learn add "turn 1 was told never to run shipit" --project jarvis_os',
    'grep -n "shipit\\|def classify" -B3 -A 20 src/jarvis/gates.py | head -60',
    'git commit -m "document systemctl restart in the runbook"',
    'gh pr create --title "stop flagging gh pr merge when it appears in prose"',
    'echo "cat x | gh pr merge 31"',
    "jarvis wo send wo-1 'do not git push origin main'",
])
def test_merely_naming_a_privileged_action_is_not_gated(command):
    assert gates.classify(command, ALL_GATES) is None, f"{command!r} must not be gated"


@pytest.mark.parametrize("command", [
    # …but a shell that re-parses its quoted payload turns that data back into code.
    'sh -c "bash scripts/shipit.sh"',
    'bash -c "gh pr merge 31"',
    'bash -lc "gh pr merge 31"',
    'eval "systemctl restart jarvis"',
    'echo "gh pr merge 31" | xargs -I{} sh -c "{}"',
])
def test_a_quoted_payload_handed_to_a_shell_is_still_gated(command):
    assert gates.classify(command, ALL_GATES) is not None, f"{command!r} must be gated"


def test_gated_action_hidden_in_a_pipeline_is_still_caught():
    """A classifier that only reads well-formed simple commands has a bypass."""
    for command in (
        "cd /tmp && ./scripts/shipit.sh",
        "echo yes | gh pr merge 31",
        "(gh pr merge 31)",
        "true; gh pr merge 31",
        "if [ -f x ]; then gh pr merge 31; fi",
    ):
        assert gates.classify(command, ALL_GATES) is not None, command


def test_classification_is_off_when_no_gate_is_enabled():
    """Opt-in per project: an unconfigured project behaves exactly as before."""
    assert gates.classify("gh pr merge 31", gates.GateConfig()) is None
    only_release = gates.GateConfig(enabled=frozenset({"release"}))
    assert gates.classify("gh pr merge 31", only_release) is None
    assert gates.classify("./scripts/shipit.sh", only_release) is not None


# -- config ---------------------------------------------------------------------------


def test_gate_config_parse_forms():
    assert not gates.GateConfig.parse(None)
    assert not gates.GateConfig.parse(False)
    assert gates.GateConfig.parse(True).enabled == frozenset(gates.KIND_NAMES)
    assert gates.GateConfig.parse(["release"]).enabled == frozenset({"release"})
    cfg = gates.GateConfig.parse({"enabled": ["release"],
                                  "patterns": {"release": [r"deploy\.sh"]}})
    assert cfg.extra_patterns["release"] == (r"deploy\.sh",)
    assert gates.classify("./deploy.sh", cfg) is not None


def test_gate_config_rejects_unknown_and_malformed():
    """A typo'd gate name must fail loudly: silently ignoring it leaves the gate open
    while the catalog claims otherwise."""
    with pytest.raises(ValueError, match="unknown gate"):
        gates.GateConfig.parse(["pr_merge", "relase"])
    with pytest.raises(ValueError, match="unknown gate"):
        gates.GateConfig.parse({"patterns": {"nope": ["x"]}})
    with pytest.raises(ValueError, match="bad regex"):
        gates.GateConfig.parse({"enabled": ["release"], "patterns": {"release": ["(["]}})


def test_gate_config_survives_the_env_round_trip():
    """The config reaches the hook as a JSON env var, so that trip must be lossless."""
    cfg = gates.GateConfig.parse({"enabled": ["release", "pr_merge"],
                                  "patterns": {"release": ["custom"]}})
    assert gates.GateConfig.from_json(cfg.to_json()) == cfg
    assert not gates.GateConfig.from_json(None)
    assert not gates.GateConfig.from_json("not json at all")


# -- the deny-rule conflict, which fails silently -------------------------------------


def test_deny_conflicts_flags_rules_that_shadow_a_gate():
    conflicts = gates.deny_conflicts(ALL_GATES, [
        "Bash(*shipit.sh*)",
        "Bash(systemctl *)",
        "Read(//home/x/secrets/**)",     # not Bash — cannot shadow a command gate
        "Bash(rm -rf *)",                # unrelated
    ])
    by_gate = {kind: rule for kind, rule in conflicts}
    assert by_gate["release"] == "Bash(*shipit.sh*)"
    assert by_gate["service_restart"] == "Bash(systemctl *)"
    assert "Bash(rm -rf *)" not in dict(conflicts).values()


def test_deny_conflicts_only_reports_enabled_gates():
    rules = ["Bash(*shipit.sh*)"]
    assert gates.deny_conflicts(gates.GateConfig(), rules) == []
    only_merge = gates.GateConfig(enabled=frozenset({"pr_merge"}))
    assert gates.deny_conflicts(only_merge, rules) == []


def test_doctor_reports_a_gate_that_can_never_open(tmp_path, monkeypatch):
    """INV-GATE-DENY-CONFLICT: the one misconfiguration where every surface reports
    success and the command still never runs."""
    from jarvis import ops
    from jarvis.testing import make_git_project

    proj = make_git_project(tmp_path, "proj_g")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"notifications": {"sinks": ["log"]}},
        "projects": [{
            "name": "proj_g", "path": str(proj),
            "gates": ["release"],
            "settings_overrides": {"permissions": {"deny": ["Bash(*shipit.sh*)"]}},
        }],
    }))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))

    result = ops.run_doctor(catalog_path=str(catalog))

    violations = [v for p in result["projects"] for v in p["violations"]]
    conflict = [v for v in violations if v["invariant"] == "INV-GATE-DENY-CONFLICT"]
    assert len(conflict) == 1
    # The report has to name the rule to delete — "something is wrong" is not actionable.
    assert "Bash(*shipit.sh*)" in conflict[0]["detail"]
    assert conflict[0]["repaired"] is False  # the user's catalog is theirs to edit


def test_doctor_is_quiet_when_the_gate_replaces_the_deny(tmp_path, monkeypatch):
    from jarvis import ops
    from jarvis.testing import make_git_project

    proj = make_git_project(tmp_path, "proj_h")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"notifications": {"sinks": ["log"]}},
        "projects": [{
            "name": "proj_h", "path": str(proj), "gates": ["release"],
            # The deny that WOULD shadow it has been removed; unrelated ones remain.
            "settings_overrides": {"permissions": {
                "deny": ["Write(//home/x/production/**)"]}},
        }],
    }))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))

    result = ops.run_doctor(catalog_path=str(catalog))

    violations = [v for p in result["projects"] for v in p["violations"]]
    assert [v for v in violations if v["invariant"] == "INV-GATE-DENY-CONFLICT"] == []


# -- the hook: attempt -> request -> approval -> retry --------------------------------


@pytest.fixture()
def gated(jarvis_home, project):
    """A dispatched work order in a project with every gate live."""
    store = ProjectStore(project)
    wo = store.create_work_order("ship the thing", description="cut a release")
    store.set_status(wo["id"], "running")
    env = {
        "JARVIS_WO_ID": wo["id"],
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
        "JARVIS_GATES": ALL_GATES.to_json(),
    }

    class Handle:
        def __init__(self):
            self.store = store
            self.wo = wo
            self.env = env
            self.project = project

        def attempt(self, command):
            return preflight_decision(
                {"tool_name": "Bash", "tool_input": {"command": command},
                 "cwd": str(project)}, env)

    yield Handle()
    store.close()


def test_first_attempt_is_blocked_and_files_a_request(gated):
    result = gated.attempt("gh pr merge 31 --squash")

    assert _decision(result) == "deny"
    approvals = gated.store.list_approvals(gated.wo["id"])
    assert len(approvals) == 1
    assert approvals[0]["kind"] == "pr_merge"
    assert approvals[0]["status"] == "pending"
    assert approvals[0]["command"] == "gh pr merge 31 --squash"
    # The worker must be told to stop, or it burns its turn retrying.
    assert "END YOUR TURN" in _reason(result)
    # ...and told how to make a real case next time.
    assert "jarvis gate request" in _reason(result)

    # The request is queued for Neo, as an approval rather than an open question.
    neo = NeoStore()
    try:
        questions = neo.list_questions()
        assert len(questions) == 1
        assert questions[0]["kind"] == "approval"
        assert questions[0]["wo_id"] == gated.wo["id"]
        assert "gh pr merge 31 --squash" in questions[0]["question"]
    finally:
        neo.close()


def test_worker_parked_on_a_gate_is_marked_waiting_not_running(gated):
    gated.attempt("./scripts/shipit.sh")
    assert gated.store.get_work_order(gated.wo["id"])["status"] == "waiting_input"


def test_retrying_while_under_review_does_not_file_a_second_request(gated):
    """A worker that loops on the command must not flood Neo's queue."""
    gated.attempt("gh pr merge 31")
    second = gated.attempt("gh pr merge 31")

    assert _decision(second) == "deny"
    assert "already under review" in _reason(second)
    assert len(gated.store.list_approvals(gated.wo["id"])) == 1
    neo = NeoStore()
    try:
        assert len(neo.list_questions()) == 1
    finally:
        neo.close()


def test_approved_command_goes_through(gated):
    gated.attempt("gh pr merge 31")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="approved",
                         reason="tests pass, PR reviewed", decided_by="neo")

    result = gated.attempt("gh pr merge 31")

    assert _decision(result) == "allow"
    assert "approved by neo" in _reason(result)
    assert gated.store.get_approval(approval["id"])["uses"] == 1


def test_approval_authorises_only_the_command_it_was_given(gated):
    """The blast radius of one "yes". Approving a merge must not approve a release, nor
    a differently-worded merge."""
    gated.attempt("gh pr merge 31")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="approved", reason="ok",
                         decided_by="neo")

    assert _decision(gated.attempt("gh pr merge 31")) == "allow"
    # A different PR, a different flag, and a different gate entirely.
    assert _decision(gated.attempt("gh pr merge 32")) == "deny"
    assert _decision(gated.attempt("gh pr merge 31 --admin")) == "deny"
    assert _decision(gated.attempt("./scripts/shipit.sh")) == "deny"


def test_grant_expires_and_is_refiled(gated, monkeypatch):
    gated.attempt("./scripts/shipit.sh")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="approved", reason="ok",
                         decided_by="user")
    assert _decision(gated.attempt("./scripts/shipit.sh")) == "allow"

    # Wind the clock past the window: the grant stops working on its own, without
    # anything having to sweep the table.
    from jarvis import db
    monkeypatch.setattr(db, "now", lambda: approval["ts"] + gates.GRANT_TTL_SECONDS + 1)

    result = gated.attempt("./scripts/shipit.sh")
    assert _decision(result) == "deny"
    # An expired window is not a refusal — a fresh request is filed.
    assert len(gated.store.list_approvals(gated.wo["id"])) == 2


def test_grant_is_use_limited(gated):
    gated.attempt("gh pr merge 31")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="approved", reason="ok",
                         decided_by="neo")

    allowed = sum(_decision(gated.attempt("gh pr merge 31")) == "allow"
                  for _ in range(gates.GRANT_MAX_USES + 2))

    assert allowed == gates.GRANT_MAX_USES
    # Once spent, the next attempt files a new request rather than silently failing.
    assert len(gated.store.list_approvals(gated.wo["id"])) == 2


def test_denied_command_stays_denied_with_the_reason(gated):
    gated.attempt("git push origin main")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="denied",
                         reason="open a PR instead", decided_by="neo")

    result = gated.attempt("git push origin main")

    assert _decision(result) == "deny"
    assert "open a PR instead" in _reason(result)
    # No new request: a denied worker must not be able to re-roll the dice by retrying.
    assert len(gated.store.list_approvals(gated.wo["id"])) == 1


def test_ungated_commands_are_untouched_by_the_gate(gated):
    """The gate returns "no opinion" for ordinary commands so the existing rules apply."""
    assert gated.attempt("uv run pytest tests/") is None
    assert _decision(gated.attempt("jarvis wo list")) == "allow"  # contract command
    assert gated.store.list_approvals(gated.wo["id"]) == []


def test_interactive_sessions_are_never_gated(project, jarvis_home):
    """No JARVIS_WO_ID means the user is driving; gates govern dispatched workers."""
    result = preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": "./scripts/shipit.sh"},
         "cwd": str(project)},
        {"JARVIS_PROJECT_PATH": str(project), "JARVIS_GATES": ALL_GATES.to_json()},
    )
    assert result is None


def test_project_without_gates_is_unaffected(project, jarvis_home):
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("x")
        result = preflight_decision(
            {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 31"},
             "cwd": str(project)},
            {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
        )
        assert result is None
        assert store.list_approvals(wo["id"]) == []
    finally:
        store.close()


def test_gate_fails_closed_when_the_machinery_breaks(gated, monkeypatch):
    """A broken gate must not become an open door. The whole point of the feature is
    that these commands do not run unreviewed, so an error is a deny."""
    import jarvis.hooks as hooks_mod

    def boom(*a, **k):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(hooks_mod, "_resolve_gate", boom)

    result = gated.attempt("./scripts/shipit.sh")

    assert _decision(result) == "deny"
    assert "could not verify approval" in _reason(result)


def test_gate_denies_when_the_project_db_is_unreachable(project, jarvis_home, tmp_path):
    """Same principle at the other end: no place to record the request means no run."""
    stray = tmp_path / "not-a-project"
    stray.mkdir()
    result = preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": "./scripts/shipit.sh"},
         "cwd": str(stray)},
        {"JARVIS_WO_ID": "wo-nope", "JARVIS_PROJECT_PATH": str(stray),
         "JARVIS_GATES": ALL_GATES.to_json()},
    )
    assert _decision(result) == "deny"


# -- decisions -----------------------------------------------------------------------


def test_apply_decision_tells_the_worker_what_to_do_next(gated):
    gated.attempt("gh pr merge 31")
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="approved",
                         reason="checks green", decided_by="neo")

    messages = gated.store.queued_messages(gated.wo["id"])
    assert len(messages) == 1
    body = messages[0]["content"]
    assert "APPROVED" in body
    assert "checks green" in body
    # The exact command matters: the grant is scoped to that string.
    assert "gh pr merge 31" in body
    kinds = [e["kind"] for e in gated.store.list_events(gated.wo["id"])]
    assert "gate_decided" in kinds


def test_denial_message_tells_the_worker_not_to_retry(gated):
    gated.attempt("gh pr merge 31")
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="denied",
                         reason="tests are failing", decided_by="neo")

    body = gated.store.queued_messages(gated.wo["id"])[0]["content"]
    assert "DENIED" in body
    assert "tests are failing" in body
    assert "Do not retry it as-is" in body


# -- who pays attention --------------------------------------------------------------


def test_a_gate_with_neo_costs_the_user_no_attention(gated):
    """Routing approvals to Neo is the entire point; a request that flags the user
    anyway is the bottleneck this feature was built to remove."""
    from jarvis.invariants import check_project, true_blockers

    gated.attempt("./scripts/shipit.sh")
    wo = gated.store.get_work_order(gated.wo["id"])
    assert wo["status"] == "waiting_input"
    assert true_blockers(gated.store, wo) == []

    # The reconciler's invariants run every tick and must not put the flag back.
    check_project(gated.store, repair=True)
    assert gated.store.get_work_order(gated.wo["id"])["needs_attention"] == 0


def test_an_escalated_gate_does_ask_for_the_user(gated):
    from jarvis.invariants import true_blockers

    gated.attempt("./scripts/shipit.sh")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gated.store.mark_approval_escalated(approval["id"], "release touches production")

    wo = gated.store.get_work_order(gated.wo["id"])
    blockers = true_blockers(gated.store, wo)
    assert any("gate approval needed" in b for b in blockers)
    assert str(approval["id"]) in blockers[0]


def test_escalated_gate_stays_pending_so_the_user_can_still_open_it(gated):
    """Neo declining must not close the request — the user needs a row to act on."""
    gated.attempt("./scripts/shipit.sh")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gated.store.mark_approval_escalated(approval["id"], "nope")

    fresh = gated.store.get_approval(approval["id"])
    assert fresh["status"] == "pending"
    assert fresh["escalated"] == 1
    assert gated.store.escalated_approvals(gated.wo["id"])[0]["id"] == approval["id"]


# -- Neo's side ----------------------------------------------------------------------


def test_approval_requests_get_the_reviewer_persona(jarvis_home):
    """Neo's general persona escalates anything production-touching, which would send
    every release to the user and defeat the gate."""
    from jarvis import neo as neo_mod

    store = NeoStore()
    try:
        question = neo_mod.build_system_prompt(store, "proj_a", kind="question")
        approval = neo_mod.build_system_prompt(store, "proj_a", kind="approval")
    finally:
        store.close()

    assert "PRIVILEGED ACTION REQUEST" in approval
    assert "APPROVE when all of these hold" in approval
    assert "PRIVILEGED ACTION REQUEST" not in question
    assert question != approval


def test_verdict_without_an_explicit_approve_does_not_open_the_gate():
    from jarvis import neo as neo_mod

    assert neo_mod.parse_verdict('{"escalate": false, "reason": "fine"}')["approve"] is False
    assert neo_mod.parse_verdict("garbage")["approve"] is False
    assert neo_mod.parse_verdict("garbage")["escalate"] is True
    approved = neo_mod.parse_verdict('{"escalate": false, "approve": true, "reason": "y"}')
    assert approved["approve"] is True


def test_request_text_carries_the_case_by_itself(gated):
    """Whoever decides sees only this text — never the worker's session."""
    action = gates.classify("./scripts/shipit.sh", ALL_GATES)
    assert action is not None
    text = gates.build_request_question(
        action, gated.wo, justification="all 264 tests pass",
        evidence="PR #42, checks green",
    )
    assert "./scripts/shipit.sh" in text
    assert gated.wo["id"] in text
    assert "ship the thing" in text          # the work order title
    assert "cut a release" in text           # ...and its description
    assert "all 264 tests pass" in text
    assert "PR #42, checks green" in text


def test_missing_justification_is_flagged_to_the_reviewer(gated):
    action = gates.classify("./scripts/shipit.sh", ALL_GATES)
    assert action is not None
    text = gates.build_request_question(action, gated.wo, justification="")
    assert "red flag" in text


# -- the fourth verdict: "this was never a gated action" ------------------------------
#
# The three original verdicts all answer "should this privileged action proceed". When
# the recogniser fires on a command that performs no privileged action, that question
# has not arisen, and every available answer records something false: approve writes an
# authorisation for an act nobody performed, deny accuses the worker of a bad request
# and tells it not to retry, escalate spends the user's attention on an OS bug. The
# evidence that this is not theoretical: the identical command was denied once and
# approved once. These tests pin the honest fourth answer.


def test_dismissed_command_goes_through_without_recording_an_approval(gated):
    """The whole point: unblock the command, authorise nothing."""
    gated.attempt("grep -rn shipit.sh src/jarvis/gates.py")
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="a grep pattern, not a release", decided_by="neo")

    result = gated.attempt("grep -rn shipit.sh src/jarvis/gates.py")
    assert _decision(result) == "allow"
    # The allow REASON is the audit record of why the command ran. "approved" here
    # would be the exact false entry this verdict exists to keep out of the log.
    assert "approved by" not in _reason(result)
    assert "false positive" in _reason(result)
    assert "nothing was authorised" in _reason(result)
    assert gated.store.get_approval(approval["id"])["status"] == "dismissed"


def test_a_dismissal_never_expires(gated, monkeypatch):
    """A dismissal asserts a fact about the command, not a permission with a clock.

    The contrast with `test_grant_expires_and_is_refiled` is the point: an approval
    lapses because it bounds the gap between "yes" and the act; there is no such gap to
    bound when nothing was permitted, and re-reviewing a known classifier bug an hour
    later is pure waste.
    """
    from jarvis import db

    gated.attempt("grep -rn shipit.sh src/")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="not a release", decided_by="neo")
    assert gated.store.get_approval(approval["id"])["expires_at"] is None

    monkeypatch.setattr(db, "now", lambda: approval["ts"] + 400 * 86400)

    assert _decision(gated.attempt("grep -rn shipit.sh src/")) == "allow"
    # ...and no second request was filed, because nothing needed re-deciding.
    assert len(gated.store.list_approvals(gated.wo["id"])) == 1


def test_a_dismissal_is_not_use_limited(gated):
    """An approval is spent after GRANT_MAX_USES. A false positive is not a budget."""
    gated.attempt("grep -rn shipit.sh src/")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="not a release", decided_by="neo")

    allowed = sum(_decision(gated.attempt("grep -rn shipit.sh src/")) == "allow"
                  for _ in range(gates.GRANT_MAX_USES + 3))

    assert allowed == gates.GRANT_MAX_USES + 3
    assert len(gated.store.list_approvals(gated.wo["id"])) == 1


def test_the_expiry_sweep_leaves_dismissed_rows_alone(gated):
    """`expire_approvals` must not rewrite a dismissal to `expired`.

    Two things break if it does. The command needs re-deciding for no reason, and — the
    reason this is a dedicated test — the false-positive COUNT silently drains away as
    rows migrate out of `dismissed`, which is the one number that says whether the
    recognisers are improving.
    """
    gated.attempt("grep -rn shipit.sh src/")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="not a release", decided_by="neo")
    # Spend it well past what would exhaust an approval.
    for _ in range(gates.GRANT_MAX_USES + 2):
        gated.attempt("grep -rn shipit.sh src/")

    gated.store.expire_approvals()

    assert gated.store.get_approval(approval["id"])["status"] == "dismissed"
    assert gated.store.dismissed_count() == 1
    assert gated.store.dismissed_count(gated.wo["id"]) == 1


def test_a_dismissal_does_not_widen_beyond_the_exact_command(gated):
    """Scope is what keeps an unbounded dismissal safe, so it is pinned hard.

    Without a clock, the exact-string match on one work order IS the containment. A
    dismissal that leaked to a prefix would turn "that grep is harmless" into standing
    permission for anything starting with the same words.
    """
    gated.attempt("grep -rn shipit.sh src/")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="not a release", decided_by="neo")

    assert _decision(gated.attempt("grep -rn shipit.sh src/")) == "allow"
    # Extra arguments, a different target, and a genuinely privileged command that
    # happens to share the dismissed one's opening words: all re-gated.
    assert _decision(gated.attempt("grep -rn shipit.sh src/ && ./scripts/shipit.sh")) == "deny"
    assert _decision(gated.attempt("grep -rn shipit.sh docs/")) == "deny"
    assert _decision(gated.attempt("./scripts/shipit.sh")) == "deny"


def test_dismissal_message_says_nothing_was_authorised_and_nothing_refused(gated):
    """A worker told only "proceed" learns the gate is a formality; one told "denied"
    learns to avoid a command that was always fine. It must be told neither."""
    gated.attempt("grep -rn shipit.sh src/")
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="the literal appears inside a search pattern",
                         decided_by="neo")

    body = gated.store.queued_messages(gated.wo["id"])[0]["content"]
    assert "DISMISSED" in body
    assert "the literal appears inside a search pattern" in body
    assert "nothing was authorised and nothing was refused" in body
    assert "grep -rn shipit.sh src/" in body
    assert "Do not retry it as-is" not in body


def test_a_dismissal_is_not_recorded_as_a_gate_decision(gated):
    """`gate_decided` is the record of a privileged action being ruled on. Folding false
    positives into it inflates exactly the audit trail this verdict protects."""
    gated.attempt("grep -rn shipit.sh src/")
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="not a release", decided_by="neo")

    kinds = [e["kind"] for e in gated.store.list_events(gated.wo["id"])]
    assert "gate_dismissed" in kinds
    assert "gate_decided" not in kinds


def test_the_timeline_does_not_call_a_dismissed_run_an_approved_command(gated):
    from jarvis import timeline

    gated.attempt("grep -rn shipit.sh src/")
    approval = gated.store.list_approvals(gated.wo["id"])[0]
    gates.apply_decision(gated.store, approval["id"], verdict="dismissed",
                         reason="not a release", decided_by="neo")
    gated.attempt("grep -rn shipit.sh src/")

    rendered = timeline.build_timeline(
        gated.store.get_work_order(gated.wo["id"]),
        gated.store.list_events(gated.wo["id"]), [],
    )
    labels = " | ".join(row["label"] for row in rendered)
    assert "matched by mistake" in labels
    assert "Ran the approved" not in labels
    # And the dismissal itself is a signal-level entry, not buried as plumbing: it is
    # the only place a reader learns the OS misfired.
    assert timeline.event_level("gate_dismissed") == "signal"


def test_apply_decision_refuses_a_verdict_it_does_not_understand(gated):
    gated.attempt("gh pr merge 31")
    approval = gated.store.list_approvals(gated.wo["id"])[0]

    with pytest.raises(ValueError):
        gates.apply_decision(gated.store, approval["id"], verdict="probably",
                             reason="hmm", decided_by="neo")

    assert gated.store.get_approval(approval["id"])["status"] == "pending"


# -- the reviewer's mandate -----------------------------------------------------------


def test_the_persona_asks_whether_it_was_a_gated_action_before_asking_whether_to_allow_it():
    """Position is the fix, not the wording.

    The previous persona OPENED by asserting "a worker tried to run a command that ships
    code", then required of every approval that work "landed on a branch, in a pull
    request, with checks passing" — which a misclassified `grep` can never satisfy. The
    closest-fitting clause left was DENY, so the persona structurally forced the wrong
    answer on false positives. "Was this even a gated action" is a question about the
    classifier, and it has to be settled before any question about the worker.
    """
    persona = gates.REVIEWER_PERSONA
    premise = persona.index("PREMISE CHECK")
    dismiss = persona.index("DISMISS when the command performs no privileged action")
    approve = persona.index("APPROVE when all of these hold")
    deny = persona.index("DENY when the request")
    escalate = persona.index("ESCALATE to the user")

    assert premise < dismiss < approve < deny < escalate


def test_the_persona_states_the_hard_limit_on_dismissing():
    """The one thing that keeps a mis-dismissal from being an open door: actually
    performing the action is never dismissible, however routine it looks."""
    # Collapsed, because the persona is hard-wrapped prose and the sentences under test
    # straddle line breaks.
    persona = " ".join(gates.REVIEWER_PERSONA.split())
    assert "HARD LIMIT" in persona
    for phrase in ("ACTUALLY invokes the deploy", "ACTUALLY merges a pull request",
                   "ACTUALLY restarts or stops a service"):
        assert phrase in persona
    # Ambiguity resolves towards review, not towards dismissal.
    assert "it runs it — assume the privileged reading" in persona
    # And deny is explicitly fenced off from false positives, which is the instruction
    # whose absence produced the contradictory rulings.
    assert "never use it for a command the recogniser matched by mistake" in persona


def test_the_persona_offers_dismissal_as_a_json_verdict():
    """A branch the reviewer cannot express is a branch it cannot take."""
    assert '"verdict": "dismiss"' in gates.REVIEWER_PERSONA
    assert '"verdict": "approve"' in gates.REVIEWER_PERSONA
    assert '"verdict": "deny"' in gates.REVIEWER_PERSONA


def test_the_request_shows_the_reviewer_which_recogniser_fired(gated):
    """The premise check is unanswerable without it: 'does this command really do that'
    needs to know what 'that' was matched on."""
    action = gates.classify("grep -rn shipit.sh src/", ALL_GATES)
    assert action is not None
    text = gates.build_request_question(action, gated.wo, justification="read-only")
    assert action.matched in text
    assert "dismiss it if the command performs no privileged action" in text


# -- Neo's JSON contract, across a release ---------------------------------------------


def test_neo_verdicts_parse_into_the_three_rulings():
    from jarvis import neo as neo_mod

    def ruling(raw):
        return neo_mod.parse_verdict(raw)["verdict"]

    assert ruling('{"escalate": false, "verdict": "approve", "reason": "r"}') == "approved"
    assert ruling('{"escalate": false, "verdict": "deny", "reason": "r"}') == "denied"
    assert ruling('{"escalate": false, "verdict": "dismiss", "reason": "r"}') == "dismissed"
    # Past participles too: the persona asks for the verb, the database stores the
    # participle, and a model that writes the other one is not making a real mistake.
    assert ruling('{"escalate": false, "verdict": "dismissed", "reason": "r"}') == "dismissed"
    assert ruling('{"escalate": false, "verdict": "approved", "reason": "r"}') == "approved"


def test_a_dismissal_is_not_an_approval_to_anything_reading_the_old_field():
    """`approve` stays False on a dismissal. Code that conflates the two would put the
    authorisation straight back into the audit trail."""
    from jarvis import neo as neo_mod

    v = neo_mod.parse_verdict('{"escalate": false, "verdict": "dismiss", "reason": "r"}')
    assert v["verdict"] == "dismissed"
    assert v["approve"] is False


def test_an_older_neo_emitting_the_boolean_still_works():
    """Required during a release, not politeness. The persona ships in the code but
    Neo's learnings live in the production state directory, so the two can disagree in
    either direction across an upgrade."""
    from jarvis import neo as neo_mod

    old_yes = neo_mod.parse_verdict('{"escalate": false, "approve": true, "reason": "r"}')
    assert old_yes["verdict"] == "approved"
    old_no = neo_mod.parse_verdict('{"escalate": false, "approve": false, "reason": "r"}')
    assert old_no["verdict"] == "denied"


def test_an_unrecognised_verdict_does_not_open_a_gate():
    """Fail closed: a `verdict` this build has never heard of must not be read as yes."""
    from jarvis import neo as neo_mod

    v = neo_mod.parse_verdict('{"escalate": false, "verdict": "probably", "reason": "r"}')
    assert v["verdict"] == "denied"
    assert v["approve"] is False
