"""The TIGHTENED high-stakes net, rule by rule, on SYNTHETIC sentences (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md §6-)"""

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
    # issue 1532: a participle naming a record STATE (backlog status `dropped`)
    ("dropped-state",
     "Duplicates folded into an item are computed in ops.show_backlog as a new "
     "duplicates key (dropped rows of the same project whose duplicate_of is this "
     "id), not in the UI route.", False),
    ("deleted-state", "The sweep skips deleted records and archived ones.", False),
    ("drop-column", "Drop the column once the readers are gone.", True),
    ("delete-rows", "Delete the rows the backfill duplicated.", True),
    ("dropping-rows", "Dropping rows older than a week keeps the table small.", True),
    ("on-delete-cascade",
     "The child table declares ON DELETE CASCADE, so the schema needs no trigger.",
     False),
    # 2. spend needs a money object
    ("spend-round", "A premature submit spends a whole review round on nothing.", False),
    ("spend-slot", "A parked manager can no longer spend a concurrency slot.", False),
    ("spend-cap", "Dedupe runs before the cap so duplicates cannot spend it.", False),
    ("spend-money", "Spent ~$6.12 of real API calls on a paired live probe.", True),
    ("spend-dollars", "Spent forty dollars of the user's credit on the sweep.", True),
    # 3
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
    # 7
    ("makes-a-release", "The handshake is what makes a release verifiable.", False),
    ("cut-a-release", "Cut a release once the fix landed rather than waiting.", True),
    # 8. licence in the permission sense
    ("licence-to", "The paragraph was read as licence to ship a thin PR body.", False),
    ("licence-legal", "Added the licence header the upstream project requires.", True),
    # 9. token in the measurement-and-parsing sense
    ("single-token", "My edit was restructured into a single-token insertion.", False),
    ("zero-token", "Filtered the synthetic zero-token rows inside the merge.", False),
    # SINGULAR on purpose: the shipped row is `\btoken\b`, so "tokens" was never held
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
    # 13. production as an act on it, never the bare noun: issue 1517
    ("prod-caller", "ops.mo_create is the only production caller (CLI and /mo POST both "
                    "use it).", False),
    ("prod-checkout", "The refusal string exists nowhere in the production checkout.",
     False),
    ("prod-path", "The corpus lives in a path under production, never in the repo.",
     False),
    ("prod-name", "Read the version from production jarvis_os read-only.", False),
    ("prod-code", "It is not production code: no module imports it.", False),
    ("prod-runs-config", "Prod runs self_dispatch=true, so reported bugs start at once.",
     False),
    ("deploy-to-prod", "Deploy to production once the round passes.", True),
    ("restart-prod-service", "Restart the production service after the tag lands.", True),
    ("prod-credentials", "Read the production credentials from the vault.", True),
    ("prod-database", "_migrate() runs over live production databases on every call.",
     True),
    ("writes-to-prod", "The release script writes to production as a subprocess.", True),
    ("downgrade-prod", "Unwinding would mean downgrading production to the old tag.", True),
    ("prod-runs-version", "No release cut: production runs 0.10.45, which carries it.",
     True),
    ("prod-checked-out", "Production at /srv/app is already checked out at that tag.", True),
    ("already-in-prod", "No gate request: the payload is already in production.", True),
    ("rows-backfilled", "Historical rows are backfilled, not just new ones.", True),
    # 14. token/secret/auth only as a credential act or object: issue 1522
    ("token-free", "Dedupe stays token-free: fuzzy duplicates are QUARANTINED for Neo, "
                   "never auto-folded.", False),
    ("token-cost", "The token cost of the sweep is one haiku call per tick.", False),
    ("token-budget", "Each tick gets a token budget the steward can refuse.", False),
    ("the-json-token", "The parser stops at the json token after the brace.", False),
    ("auth-paused", "Auth failures paused the fleet for an hour.", False),
    ("bare-token", "A token in the grammar is one word of the command.", False),
    ("bare-secret", "The ranking is no secret: it is printed on the card.", False),
    ("bare-auth", "The auth module is left alone.", False),
    ("reused-api-key", "Reused the production API key rather than minting one.", True),
    ("minted-committed", "Minted a new auth token and committed it.", True),
    ("prod-token", "Read the production token from the env file.", True),
    ("api-token-for", "Created an API token for the bot account.", True),
    ("rotate-token", "Rotated the token after the leak.", True),
    ("logged-secret", "Logged the client secret at debug level.", True),
    ("service-credentials", "Uses the service account credentials in the vault.", True),
    ("bypass-auth", "Bypassed auth on the admin endpoint for the probe.", True),
    # 15. price/invoice/charge/bill only as the money act or object: issue 1610
    ("price-held-sentence",
     "Per Q3665 and user ruling on assumption 1: bare aliases price at family newest; an "
     "id not exactly in usage.PRICES (after stripping claude-, date, [1m]) keeps a "
     "fallback price and is named with its dollars in totals.unpriced_note on jarvis cost "
     "and /cost. Rejected zero-pricing: it breaks the ledger identity with the meter.",
     False),
    ("price-rate-table", "The table prices sonnet at $2 per million input tokens.", False),
    ("price-report", "The report prints the list price of each model id.", False),
    ("pricing-tiers", "Rates come from the pricing table and its tiers.", False),
    ("charged-rate", "Cache writes are charged at $3.75 per million tokens.", False),
    ("charged-on-tokens", "The cache-write price is charged on the prompt tokens.", False),
    ("charges-attention", "An escalation charges the user's attention, nothing else.", False),
    ("in-charge", "The manager in charge of the sweep reads the ledger.", False),
    ("billed-at-rate", "Haiku rows are billed at the newest tier.", False),
    ("invoice-noun", "The invoice column on the report stays blank.", False),
    ("price-set-in-table", "Set the sonnet price to $2 in the rate table.", False),
    ("price-updated", "Updated the price to $2 per million tokens in the table.", False),
    ("billed-passive-no-payer", "The rows are billed at list rates.", False),
    ("invoice-customer", "Invoice the customer for $900.", True),
    ("invoiced-client", "Invoiced the client.", True),
    ("charged-passive", "The customer was charged twice.", True),
    ("billed-passive", "Subscribers will be billed from next month.", True),
    ("raise-price", "Raise the price to $50.", True),
    ("charge-customer", "Charge the customer once the trial ends.", True),
    ("charged-card", "Charged the user's card when the retry passed.", True),
    ("charge-amount", "The retry charged $40 before anyone noticed.", True),
    ("charged-to-account", "The overage is charged to the team's account.", True),
    ("charge-of-amount", "A charge of $40 hit the sandbox.", True),
    ("amount-charge", "A $40 charge hit the sandbox.", True),
    ("bill-customers", "Bill customers for the overage from next month.", True),
    ("billed-account", "Billed the account twice for the same seat.", True),
    ("billed-amount", "The sweep billed 12 dollars to the sandbox.", True),
    ("invoice-amount", "The vendor sent an invoice for $900.", True),
    ("invoice-issued", "Issued an invoice to the reseller.", True),
    ("price-customers-pay", "Raised the price that customers pay next quarter.", True),
    ("subscription-price", "Raised the subscription price before the renewal.", True),
    ("spend-budget", "Spending $200 on the paired probe is the plan.", True),
]


@pytest.mark.parametrize("rule,text,held", [(r, t, h) for r, t, h in TIGHTENED_TABLE],
                         ids=[r for r, _, _ in TIGHTENED_TABLE])
def test_tightened_net_holds_acts_and_clears_the_routine(rule, text, held):
    """One sentence per rule, both directions"""
    assert bool(autoreview.high_stakes_marker(text, tightened=True)) is held


def test_default_is_the_wide_net_unchanged():
    """`tightened=False` is the SHIPPED net and this is the compatibility pin: the tightening may not..."""
    assert autoreview.high_stakes_marker("Deleted the assertion.") == "Delet"
    assert autoreview.high_stakes_marker("A premature submit spends a round.") == "spend"
    assert autoreview.high_stakes_marker("Deleted the assertion.", tightened=True) == ""


def test_the_wide_net_still_holds_everything_the_tightened_one_does():
    """The tightened net is a SUBSET EXCEPT for the additions"""
    additions = ("patch-bump", "minor-bump", "version-derived", "tag-cut", "force-push",
                 "remote-branch", "retract-exemption", "rearm-gate", "spend-money",
                 "spend-dollars")
    for rule, text, _held in TIGHTENED_TABLE:
        if rule in additions or not autoreview.high_stakes_marker(text, tightened=True):
            continue
        assert autoreview.high_stakes_marker(text), rule


def test_each_narrowing_removes_a_hold_the_wide_net_makes():
    """The other half of the subset claim: every routine sentence in the table WAS held by the shipped net"""
    never_held_by_the_wide_net = ("auth-cannot", "tool-version", "version-mention",
                                  "local-branch", "code-exemption", "token-cost",
                                  "token-budget")
    for rule, text, held in TIGHTENED_TABLE:
        if held or rule in never_held_by_the_wide_net:
            continue
        assert autoreview.high_stakes_marker(text), rule


def test_mode_is_a_catalog_mode_and_the_default():
    assert "regex-tightened" in catalog.STAKES_CLASSIFIER_MODES
    assert catalog.ValidationConfig().stakes_classifier == "regex-tightened"
