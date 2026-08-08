"""LLM-graded knowledge-retrieval evals: can a worker actually CASH IN the index?

Worker prompts no longer carry the knowledge base; they carry a bounded index of it
(headline + id) and four commands for fetching the rest. That trade only pays if a
model handed the index reliably (a) notices the entry it needs exists and (b) aims a
retrieval at the right one. Nothing else in the suite tests that: the unit tests prove
the index is built and bounded, and a well-formed index nobody reads is exactly the
failure this design was supposed to remove, just cheaper.

Three batteries:

  * knowledge/retrieve  — the answer sits behind an index headline. The worker must
                          reach for the knowledge base rather than decide for itself
                          (>= 6/7).
  * knowledge/precision — when it names an id, it must name the RIGHT one. The index
                          is deliberately full of adjacent decoys inside the same
                          topic, because "there is something about deploys" is not
                          findability; "it is THIS one" is (>= 5/6 of the replies
                          that name an id).
  * knowledge/no-phantom — the area is genuinely absent from the index. Searching is
                          fine, inventing an id is not: a model that answers every
                          situation with `learn show kn-something` would ace the first
                          two batteries while being useless (>= 3/4).

Retrieval is graded against a REAL store, not by string matching. When the reply
searches instead of showing, the term it chose is run through
`CentralStore.search_knowledge` and the target entry has to actually come back — so a
plausible-looking `jarvis learn search "deployment"` that retrieves nothing is scored
as the miss it is. That is also the early-warning system for substring search: the day
this battery starts failing on synonyms is the day the search backend needs to stop
being `LIKE`.

Opt-in (spends tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_knowledge_retrieval_judgment.py -q
    JARVIS_EVALS_MODEL=opus  # optional, default sonnet
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from jarvis import claude_cli
from jarvis.catalog import ProjectSpec
from jarvis.central_store import CentralStore
from jarvis.dispatch import build_worker_prompt

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

WO = {
    "id": "wo-eval42",
    "title": "Add a nightly export job for the billing reports",
    "description": ("Finance wants last night's billing report waiting for them each "
                    "morning. Add a scheduled job that renders the report and drops "
                    "it somewhere they can pick it up."),
}

# The fixture knowledge base. Every body is long enough that its headline is a genuine
# truncation — if these were one-liners the index would be the payload and the eval
# would prove nothing. `key` is the eval's handle on the row; the worker never sees it.
#
# Topics carry DECOYS on purpose: two deploy entries, three billing entries. An index
# that only lets a model conclude "there is something about deploys in here" has not
# made anything findable.
KB: list[tuple[str, str, str, str]] = [
    ("migrations-during-deploy", "deploy",
     "Schema migrations never run inside the deploy step. The deploy is blue/green, so "
     "for the minutes between the two slots swapping there are two application versions "
     "live against one database, and a migration that drops or renames a column takes "
     "the old slot down with it. Land the additive migration in its own release first, "
     "let both versions run against the widened schema, and only then ship the code "
     "that stops writing the old column. The destructive half goes out a release later "
     "again. This cost us the 14 March outage, when a rename shipped with the code that "
     "used it and every request on the old slot 500'd for nine minutes.",
     ""),
    ("restart-services", "deploy",
     "Never restart the application services by hand on the box. systemd is configured "
     "with a socket-activated pre-start check that rewrites the environment file from "
     "the release manifest, and a manual `systemctl restart` skips it, leaving the "
     "process running against whatever environment the previous release wrote. Use the "
     "release script; it does the restart as its last step.",
     ""),
    ("billing-rounding", "billing",
     "Billing figures are stored in integer cents and rounded for display ONLY at the "
     "presentation layer. Anything that renders, exports or emails a billing number "
     "must go through `billing.format_amount`, never through its own float arithmetic — "
     "we have had two rounding-drift incidents where an export and the invoice it was "
     "reconciled against disagreed by a few cents per line and nobody could tell which "
     "one was authoritative.",
     ""),
    ("billing-timezone", "billing",
     "Every billing period boundary is computed in UTC, not in the tenant's local "
     "timezone, and any job that says 'yesterday' has to mean 'yesterday in UTC' or it "
     "will double-count or drop the rows that fall in the offset window. This bit the "
     "monthly rollup twice before it was written down.",
     ""),
    ("billing-pii", "billing",
     "Billing exports must never include the raw payment-instrument fields, even "
     "truncated. The reports schema exposes a `payment_ref` surrogate for exactly this "
     "reason and the compliance review treats anything else as a reportable incident.",
     ""),
    ("scheduled-jobs", "jobs",
     "Scheduled jobs are registered in the job catalog with an explicit idempotency "
     "key, never as a bare cron entry. The scheduler guarantees at-least-once delivery, "
     "so a job without a key will happily run twice on a retry — which for anything that "
     "writes a file or sends an email means duplicates in production.",
     ""),
    ("job-failures", "jobs",
     "A scheduled job that fails must exit non-zero AND emit a notification; the "
     "scheduler only retries on the exit code, and a job that logs an error and exits 0 "
     "is invisible for as long as it keeps failing. We lost a week of the reconciliation "
     "feed that way.",
     ""),
    ("artifact-storage", "storage",
     "Generated artifacts go to the reports bucket under a dated prefix, never to the "
     "application's own filesystem: the app boxes are replaced on every release, so "
     "anything written locally survives until the next deploy and not one minute longer.",
     ""),
    ("test-fixtures", "testing",
     "Integration tests share one Postgres instance, so a test that writes must clean up "
     "in a fixture teardown rather than relying on transaction rollback; the connection "
     "pool hands the same session to unrelated tests and a leaked row surfaces as a "
     "failure somewhere else entirely.",
     ""),
    ("pr-conventions", "process",
     "Pull requests are squash-merged with the PR title as the commit subject, so the "
     "title has to read as a commit message rather than as a ticket name.",
     ""),
    ("secrets", "process",
     "Secrets are read from the environment at process start and never from a file "
     "checked into the repo, including in tests, where the fixtures inject fakes.",
     ""),
]

INSTRUCTION = (
    "\n\n# This turn\n"
    "You have just hit the situation below, at the very start of this work order. "
    "Reply with ONLY the single next command you would run — one line, no prose, no "
    "code fences. If you would not run a command, reply NONE.\n\n"
    "Situation: "
)

# The answer sits behind an index headline. Reaching for the knowledge base — by id or
# by search — is the pass; deciding it alone is the miss.
MUST_RETRIEVE = [
    ("rounding", "I am about to write the code that renders the billing amounts into "
     "the export file. I was going to format them with a plain two-decimal float "
     "format.", "billing-rounding"),
    ("timezone", "The job needs to select 'last night's' rows. I was going to use the "
     "server's local midnight as the boundary.", "billing-timezone"),
    ("idempotency", "I need to register this as a nightly scheduled job and was about "
     "to add a cron entry for it.", "scheduled-jobs"),
    ("storage", "The rendered report has to land somewhere finance can pick it up. I "
     "was going to write it to a directory on the app server.", "artifact-storage"),
    ("failure-handling", "I need to decide what the job does when the report query "
     "fails. I was going to log the error and exit cleanly so the scheduler does not "
     "spam.", "job-failures"),
    ("pii", "The billing report has a column with the customer's card details in it. I "
     "was going to include it truncated to the last four digits.", "billing-pii"),
    ("migration", "This needs a new column on the reports table, and I was going to "
     "put the migration in the same release as the job that reads it.",
     "migrations-during-deploy"),
]

# The index says nothing about these. Searching is legitimate; asserting a specific
# entry holds the answer is a hallucination, and a model that always reaches for
# `learn show` would otherwise sweep the batteries above.
MUST_NOT_INVENT = [
    ("absent-csv-dialect", "I need to pick the CSV dialect for the export file — "
     "comma or semicolon separated, and whether to emit a header row."),
    ("absent-naming", "I need a name for the module that holds the rendering code."),
    ("absent-retry-window", "I need to choose how many minutes to wait before the "
     "job's first retry."),
    ("absent-log-level", "I need to decide whether the job's progress messages are "
     "logged at INFO or DEBUG."),
]


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Run the subject outside this repo, so its CLAUDE.md is not loaded on top of the
    worker contract we are actually testing (same reason as the persona evals)."""
    return tmp_path_factory.mktemp("jarvis-knowledge-eval")


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> CentralStore:
    """A real central store holding KB. The eval grades retrieval by running the
    worker's chosen search term through it, so this cannot be a stub.

    Given an explicit db path rather than `$JARVIS_HOME`: the autouse `jarvis_home`
    fixture is function-scoped and would repoint the env under this module-scoped
    store between tests.
    """
    home = tmp_path_factory.mktemp("jarvis-knowledge-home")
    central = CentralStore(path=home / "os.db")
    for key, topic, content, tags in KB:
        row = central.add_knowledge(content, project="billing_app", topic=topic,
                                    tags=tags)
        IDS[key] = row["id"]
    return central


# key -> kn-id, filled by the `store` fixture. The worker never sees the keys; they are
# how the scenarios below name the entry each one should have retrieved.
IDS: dict[str, str] = {}


@pytest.fixture(scope="module")
def contract(store, tmp_path_factory) -> str:
    """The REAL dispatch prompt, with the REAL index block. If the renderer changes,
    this eval changes with it, which is the point."""
    spec = ProjectSpec(name="billing_app",
                       path=tmp_path_factory.mktemp("billing_app"))
    brief = store.knowledge_brief("billing_app")
    prompt = build_worker_prompt(WO, spec, brief)

    # Guard the eval's own premise. Every entry has to be reachable — its id in the
    # index — and every entry a scenario TARGETS has to be there as a headline with its
    # body withheld. Without this the suite would silently degrade into reading answers
    # straight out of the prompt the day someone raises the injection default.
    #
    # Only the targets, not all of KB: `pr-conventions` and `secrets` are shorter than
    # the headline budget, so their headline is legitimately the whole entry. That is
    # what a real knowledge base looks like, and they are here as index filler, never
    # as the answer to a scenario.
    assert "# Knowledge base" in prompt
    bodies = {key: content for key, _topic, content, _tags in KB}
    for key in bodies:
        assert IDS[key] in prompt, f"{key} missing from the index"
    for _name, _situation, key in MUST_RETRIEVE:
        assert bodies[key] not in prompt, f"{key} was pasted in full — index bypassed"
    return prompt


@pytest.fixture(scope="module")
def replies(contract, neutral_cwd) -> dict[str, str]:
    out = {}
    cases = ([(n, s) for n, s, _ in MUST_RETRIEVE] + list(MUST_NOT_INVENT))
    for name, situation in cases:
        out[name] = claude_cli.run_headless(
            INSTRUCTION + situation, system_prompt=contract, model=MODEL,
            cwd=neutral_cwd, tools="", timeout=180).strip()
    return out


SEARCH_TERM = re.compile(r"jarvis\s+learn\s+(?:search|list)\s+(?:--\S+\s+\S+\s+)*"
                         r"[\"']?([^\"'\n]+?)[\"']?\s*(?:--|$)")
KN_ID = re.compile(r"kn-[0-9a-f]+")


def retrieved(reply: str, target_id: str, store: CentralStore) -> tuple[bool, str]:
    """Did this reply actually fetch the target entry? Returns (hit, how).

    `show <id>` is checked against the id. A search is checked by RUNNING it: the term
    the worker picked has to bring the target back out of the real store, so a
    reasonable-sounding term that retrieves nothing counts as a miss rather than a pass.
    """
    if "jarvis learn show" in reply:
        ids = KN_ID.findall(reply)
        return (target_id in ids), f"show {ids}"
    m = SEARCH_TERM.search(reply)
    if m:
        term = m.group(1).strip()
        hits = store.search_knowledge(term, limit=50, project="billing_app")
        return any(h["id"] == target_id for h in hits), f"search {term!r}"
    if "jarvis learn" in reply:
        return False, "learn command, no term parsed"
    return False, "no retrieval"


@scenario("knowledge/retrieve", "the worker cashes in an index headline it needs")
def test_worker_retrieves_what_the_index_advertises(replies, store):
    misses = []
    for name, _situation, key in MUST_RETRIEVE:
        hit, how = retrieved(replies[name], IDS[key], store)
        if not hit:
            misses.append(f"{name}: {how} — {replies[name][:120]!r}")
    scored = len(MUST_RETRIEVE) - len(misses)
    assert scored >= 6, (
        f"only {scored}/{len(MUST_RETRIEVE)} situations reached the knowledge base:\n"
        + "\n".join(misses))


@scenario("knowledge/precision", "an index headline points at the RIGHT entry")
def test_named_ids_are_the_right_ones(replies, store):
    """Among replies that name an id at all, how many name the correct one?

    This is the findability claim. The index carries adjacent decoys in the same topic
    (two deploy entries, three billing ones), so picking the right id means the
    headline carried enough to discriminate — not merely enough to notice the topic.
    """
    named, correct, wrong = 0, 0, []
    for name, _situation, key in MUST_RETRIEVE:
        ids = KN_ID.findall(replies[name])
        if not ids:
            continue
        named += 1
        if IDS[key] in ids:
            correct += 1
        else:
            back = {v: k for k, v in IDS.items()}
            wrong.append(f"{name}: wanted {key}, got {[back.get(i, i) for i in ids]}")
    if named == 0:
        pytest.skip("no reply named an id — nothing to score for precision")
    assert correct >= min(5, named), \
        f"{correct}/{named} ids were right:\n" + "\n".join(wrong)


@scenario("knowledge/no-phantom", "an absent area does not summon an invented entry")
def test_absent_knowledge_is_not_invented(replies):
    """Searching an area the index does not cover is fine — claiming a specific entry
    holds the answer is not. Without this, a model that answers everything with
    `jarvis learn show kn-whatever` would sweep the other two batteries."""
    known = set(IDS.values())
    bad = []
    for name, _situation in MUST_NOT_INVENT:
        reply = replies[name]
        ids = KN_ID.findall(reply)
        # citing an id for an area the index is silent on is the failure, whether the
        # id is fabricated or a real entry about something else entirely
        if ids:
            bad.append(f"{name}: cited {ids} "
                       f"({'fabricated' if set(ids) - known else 'unrelated real entry'})"
                       f" — {reply[:120]!r}")
    scored = len(MUST_NOT_INVENT) - len(bad)
    assert scored >= 3, (
        f"only {scored}/{len(MUST_NOT_INVENT)} absent areas stayed honest:\n"
        + "\n".join(bad))
