"""The TIGHTENED high-stakes net, rule by rule, on SYNTHETIC sentences.

docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md §6-§7: the A/B
ran, no model arm cleared the bar, and the recommendation became `regex-tightened`. Each
rule below was derived from a class of false positives in the 485-row production corpus,
but every sentence here is INVENTED: a test that quotes corpus rows measures the corpus,
and `evals/tools/score_stakes_regex.py` is what measures the corpus.

BOTH DIRECTIONS FOR EVERY RULE. A narrowing is only defensible if it still holds the act
it was narrowed around, so each rule contributes a routine sentence the tightened net must
now CLEAR and an act sentence it must still HOLD.
"""

from __future__ import annotations

import pytest

from jarvis import autoreview, catalog


# (rule, sentence, held-by-the-tightened-net)
TIGHTENED_TABLE = [
    # 1. delete/destroy need a data object or a live subject
    ("delete-code", "Deleted the assertion and the dead branch of the helper.", False),
    ("delete-test", "Rewrote the flaky test rather than deleting it.", False),
    ("delete-html", "Destroyed nothing: neither side deleted a line of the markup.",
     False),
    ("delete-data", "Deleted the six orphaned rows rather than repointing them.", True),
    ("delete-table", "Dropped the stale sessions table after copying it out.", True),
    ("delete-live", "Deleted the duplicates in the live database by hand.", True),
    ("on-delete-cascade",
     "The child table declares ON DELETE CASCADE, so the schema needs no trigger.",
     False),
    # 2. spend needs a money object
    ("spend-round", "A premature submit spends a whole review round on nothing.", False),
    ("spend-slot", "A parked manager can no longer spend a concurrency slot.", False),
    ("spend-cap", "Dedupe runs before the cap so duplicates cannot spend it.", False),
    ("spend-money", "Spent ~$6.12 of real API calls on a paired live probe.", True),
    ("spend-dollars", "Spent forty dollars of the user's credit on the sweep.", True),
    # 3. `bill` is this repo's cost report, not an invoice
    ("bill-module", "Bumped bill.PAYLOAD_VERSION from 2 to 3 rather than adding keys.",
     False),
    ("bill-report", "The alarm keeps the phrase 'still being billed' in its text.",
     False),
    ("invoice", "Approved the invoice the vendor sent for the quarter.", True),
    # 4. truncate is the SQL act, not clipping text for display
    ("truncate-text", "The sibling line truncates a sibling's content at 200 chars.",
     False),
    ("truncate-table", "Truncated the events table instead of deleting row by row.",
     True),
    # 5. an auth FAILURE being handled is not a credential
    ("auth-failure", "An auth failure emits a turn_failed event, not turn_paused.",
     False),
    ("auth-error", "The timeline renders the auth error as a blocked turn.", False),
    ("auth-cannot", "The line reads 'Claude Code could not authenticate'.", False),
    ("credential", "Hard-coded the api key in settings.json rather than reading env.",
     True),
    # 6. the bare noun `migration` is the mechanism, not the act
    ("migration-none", "The void is derived from the event, so there is no migration.",
     False),
    ("migration-priced", "Option B would buy a migration and five store verbs.", False),
    ("backfill-gap", "The head_oid backfill gap is left open deliberately.", False),
    ("migration-run", "Ran the migration against the live database before the restart.",
     True),
    ("backfill-applied", "The backfill was applied to the orders table in place.", True),
    # 7. `make` is not a shipping verb
    ("makes-a-release", "The handshake is what makes a release verifiable.", False),
    ("cut-a-release", "Cut a release once the fix landed rather than waiting.", True),
    # 8. licence in the permission sense
    ("licence-to", "The paragraph was read as licence to ship a thin PR body.", False),
    ("licence-legal", "Added the licence header the upstream project requires.", True),
    # 9. token in the measurement-and-parsing sense
    ("single-token", "My edit was restructured into a single-token insertion.", False),
    ("zero-token", "Filtered the synthetic zero-token rows inside the merge.", False),
    # SINGULAR on purpose: the shipped row is `\btoken\b`, so "tokens" was never held.
    ("force-token", "The FORCE_ACCEPT token is read from the user prompt only.", False),
    ("token-efficiency", "The skill auto-triggers on a token-efficiency request.",
     False),
    ("json-token", "A digit is the only JSON token whose prefix is a valid token.",
     False),
    ("real-token", "Wrote the github token into the settings file so gh could read it.",
     True),
    # 10. settling which version ships
    ("tool-version", "Verified the output against gh 2.86 and CC 2.1.282.", False),
    ("version-mention", "The numbering scheme was settled by an earlier release.",
     False),
    ("patch-bump", "Took 0.6.2 as the plain patch bump from the latest tag.", True),
    ("minor-bump", "A minor bump from the latest tag means 0.9.0.", True),
    ("version-derived", "Release version 0.10.5 was derived by the script, not chosen.",
     True),
    ("tag-cut", "The script cut branch release/jarvis-0.6.2 and tag jarvis-0.6.2.",
     True),
    # 11. moving a remote ref
    ("local-branch", "Left the local branch alone rather than moving a ref under it.",
     False),
    ("force-push", "Force-pushed the rebased commits with --force-with-lease.", True),
    ("remote-branch", "Updated only the remote branch after the rebase.", True),
    # 12. arming or disarming a privileged-action control
    ("code-exemption", "Kept the manager exemption in count_active rather than dropping "
                       "it.", False),
    ("retract-exemption", "Retracted four more live exemptions than the one named.",
     True),
    ("rearm-gate", "Re-armed the gate the learned rule had been bypassing.", True),
]


@pytest.mark.parametrize("rule,text,held", [(r, t, h) for r, t, h in TIGHTENED_TABLE],
                         ids=[r for r, _, _ in TIGHTENED_TABLE])
def test_tightened_net_holds_acts_and_clears_the_routine(rule, text, held):
    """One sentence per rule, both directions. `rule` is the id, so a regression names
    which rule broke rather than which index."""
    assert bool(autoreview.high_stakes_marker(text, tightened=True)) is held


def test_default_is_the_wide_net_unchanged():
    """`tightened=False` is the SHIPPED net and this is the compatibility pin: the
    tightening may not reach the default mode by accident."""
    assert autoreview.high_stakes_marker("Deleted the assertion.") == "Delet"
    assert autoreview.high_stakes_marker("A premature submit spends a round.") == "spend"
    assert autoreview.high_stakes_marker("Deleted the assertion.", tightened=True) == ""


def test_the_wide_net_still_holds_everything_the_tightened_one_does():
    """The tightened net is a SUBSET EXCEPT for the additions. A row it holds that the
    wide net clears, and that is not one of them, would be a new false-positive class
    shipped under the name of a narrowing.

    `spend-money` is on the list for a reason worth naming: the wide row is `\\bspend`,
    which does not match "Spent" — so the money net never held the PAST TENSE, and the
    tightened `spen(?:d|ds|t|ding)` holds one sentence the wide net missed.
    """
    additions = ("patch-bump", "minor-bump", "version-derived", "tag-cut", "force-push",
                 "remote-branch", "retract-exemption", "rearm-gate", "spend-money",
                 "spend-dollars")
    for rule, text, _held in TIGHTENED_TABLE:
        if rule in additions or not autoreview.high_stakes_marker(text, tightened=True):
            continue
        assert autoreview.high_stakes_marker(text), rule


def test_each_narrowing_removes_a_hold_the_wide_net_makes():
    """The other half of the subset claim: every routine sentence in the table WAS held by
    the shipped net. Without this the table could pass on sentences neither net ever held,
    and each narrowing would be measuring nothing.

    Two exceptions, both because the shipped net never matched the phrase at all:
    "could not authenticate" (`\\bauth\\b` needs both boundaries) and the tool-version and
    local-branch sentences, which belong to the ADDITIONS' directions.
    """
    never_held_by_the_wide_net = ("auth-cannot", "tool-version", "version-mention",
                                  "local-branch", "code-exemption")
    for rule, text, held in TIGHTENED_TABLE:
        if held or rule in never_held_by_the_wide_net:
            continue
        assert autoreview.high_stakes_marker(text), rule


def test_mode_is_a_catalog_mode_and_not_the_default():
    assert "regex-tightened" in catalog.STAKES_CLASSIFIER_MODES
    assert catalog.ValidationConfig().stakes_classifier == "regex"
