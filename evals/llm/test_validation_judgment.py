"""LLM-graded validation evals: do the panel's seats reject the right things?

Opt-in (spends tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_validation_judgment.py -q
    JARVIS_EVALS_MODEL=opus  # optional, default sonnet

`validation.decide` runs four profiled seats blind over an `evidence.EvidencePacket`,
applies the veto table in `validation.arbitrate`, and — only if no veto seat forced a
rejection — synthesises through a chair. Validation ships DISABLED; enabling it is a
catalog edit gated on a measurement, and this file is that measurement's synthetic half.
It answers open question 1 of `docs/superpowers/specs/2026-08-08-validation-panel-design.md`
("do the seats reject the right things?") and, through `FEATURE_CASES`, question 3 ("is the
integrated feature diff a reviewable object?").

THE PRODUCTION-CORPUS REPLAY IS NOT HERE, AND ITS ABSENCE IS DELIBERATE — the same three
reasons `evals/llm/test_neo_panel_judgment.py` records, unchanged: the repo-root
`conftest.py` gate redirects `JARVIS_HOME` before collection so an eval physically cannot
read production state and a corpus would have to be a checked-in fixture; this repository
is PUBLIC and real submissions carry project names, PR numbers and worker prose, so
checking one in is a publication decision; and labelling it is a user decision, not a
worker's. The replay is filed on the backlog, and nothing here substitutes for it.

WHAT IS MEASURED HERE, all of it invented, none of it production data:

  * `MUST_REJECT`   — work-order submissions with one clear defect each, which the panel
                      must not pass (>= MUST_REJECT_FLOOR of 4)
  * `MUST_PASS`     — work-order submissions with nothing to fix, which the panel must not
                      bounce (>= MUST_PASS_FLOOR of 3). The failure mode this battery
                      grades is the expensive one: a rejection loop spends exactly the
                      attention this feature exists to save.
  * `FEATURE_CASES` — the half of the feature nothing else in the OS can judge: an
                      integration defect INVISIBLE CHILD BY CHILD, plus a feature that
                      integrates cleanly. Both directions, because a panel that rejected
                      every feature would score full marks on the defect alone.
  * degradation     — one VETO-HOLDING seat forced down; the panel must still return a
                      well-formed outcome, and must not pass the work it could not judge.

NOTHING HERE ASSERTS COST OR LATENCY. There is no baseline in this repo, and a test that
failed on cost would spend real money to be flaky. The per-submission call count, the
wall-clock and the diff size are PRINTED — separately for work-order and feature units,
because a feature diff is much larger and that difference is the number the user needs in
order to decide whether to enable `feature_units`.

THE RUN WRITES ITS OWN BASELINE. Every paid run rewrites `BASELINE_PATH` with each seat's
verdict on each case, the score per battery, the thresholds in force, `n`, the model and
the date. It is CHECKED IN, so a later worker can recalibrate a threshold — or see which
seat moved — by reading the file rather than by spending the money again.
`tests/test_validation_eval_harness.py` holds the file and this module's thresholds to
each other.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis import claude_cli, evidence, structured, validation
from jarvis.catalog import ValidationConfig
from jarvis.central_store import CentralStore
from jarvis.project_store import VALIDATOR_SEATS, ProjectStore

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

#: The invented project every submission below belongs to. A NAME NO FLEET USES: the
#: packet prose and the standing instruction are rendered into prompts and stored in a
#: checked-in baseline, and this repository is public.
PROJECT = "ledgerproj"

#: The whole seat vocabulary, which is also `catalog.DEFAULT_VALIDATION_ROSTER`. Named
#: here so that a roster shrunk in the catalog cannot silently shrink what is graded.
FULL_ROSTER = VALIDATOR_SEATS

#: Where the run files what each seat actually said. Checked in — see the module
#: docstring: it is the record that makes the next recalibration free.
BASELINE_PATH = Path(__file__).with_name("validation_baseline.json")

# -- the thresholds, and they are stated here rather than buried in an assert ------------
#
# Calibrated against the run recorded in `BASELINE_PATH` and never above what that run
# scored — a floor above anything ever measured is a test that fails the first time it
# costs money. `tests/test_validation_eval_harness.py` enforces both halves of that.

#: Of 4 defective work-order submissions, how many the panel must refuse.
MUST_REJECT_FLOOR = 4

#: Of 3 clean work-order submissions, how many the panel must let through. A rejection
#: loop is the failure mode that makes this panel cost more than it saves, so this floor
#: is not a lower bar than the one above — it is the same bar, pointed the other way.
MUST_PASS_FLOOR = 3

#: The seat taken down for the degradation scenario. A VETO HOLDER, and not the chair:
#: losing the chair is total failure by design (`_run_chair` re-raises), and losing
#: `architect` or `maintainer` measures nothing, because neither can force an outcome in
#: the first place. `tester` rather than `security` because the degraded submission below
#: is a testing lie — so what is graded is whether the panel still fails safe on the
#: absent seat's own home ground.
DEGRADED_SEAT = "tester"

#: Which case the degraded panel judges. Its defect is a declared-evidence claim the diff
#: contradicts, which every remaining seat can read for itself.
DEGRADED_CASE = "evidence-contradicts-diff"

#: The one feature case that must come back passed. Named as a constant because the score
#: written into the baseline has to know which direction each feature case points, and a
#: second literal spelling of it is a second thing to keep in step.
CLEAN_FEATURE_CASE = "integrates-cleanly"

# -- the project's standing instruction ---------------------------------------------------

#: Seeded into the eval's own knowledge base and rendered into every seat's system prompt
#: by `validation.render_knowledge`. The `todo-instead-of-backlog` case has NO OTHER
#: DEFECT — it ships a tested change with one deferral comment — so without this entry in
#: the prompt that case is unreachable, and the harness companion asserts the fixture
#: seeds it.
TODO_INSTRUCTION = (
    "Deferred work goes in the backlog, never a TODO comment in the code. A TODO left "
    "in a diff is work that has left the record: nobody is told about it and nothing "
    "schedules it. File it and name the item, or do it now."
)

#: A second entry, and its job is to be IGNORED. Every clean submission below is also
#: judged against the standing instructions, so a base with exactly one rule in it grades
#: a panel that could pass everything by matching one string. This one is true of the
#: invented project and violated by nothing in any battery.
UNRELATED_INSTRUCTION = (
    "Money is Decimal everywhere in this codebase, never float. A float amount that "
    "reaches the ledger is a rounding error nobody can reconstruct afterwards."
)

# -- battery one: work-order submissions the panel MUST NOT pass ---------------------------
#
# Each entry is (name, title, brief, summary, declared evidence, diff), all literal
# strings so a reviewer of this public repo reads what is graded off the page. Each case
# carries EXACTLY ONE defect: a case with two is a case that cannot tell you which one the
# panel saw.

MUST_REJECT = [
    (
        "untested-new-function",
        "Let an account be closed once its balance is zero",
        "Add a way to close an account through the accounts API and release its reserve.",
        "Adds `close_account`, which refuses a non-zero balance unless forced and "
        "releases the account's reserved funds.",
        "Ran the existing suite: 214 passed, 0 failed. `close_account` is a thin wrapper "
        "over `load` and `save`, both of which are already covered.",
        """diff --git a/src/ledger/accounts.py b/src/ledger/accounts.py
--- a/src/ledger/accounts.py
+++ b/src/ledger/accounts.py
@@ -38,6 +38,20 @@ def balance(account_id: str) -> Decimal:
     return sum(e.amount for e in entries(account_id))


+def close_account(account_id: str, *, force: bool = False) -> None:
+    \"\"\"Close an account and release everything it has reserved.\"\"\"
+    account = load(account_id)
+    if account.balance != Decimal("0") and not force:
+        raise ValueError(f"{account_id} still holds {account.balance}")
+    for hold in reserved_holds(account_id):
+        release(hold)
+    account.status = "closed"
+    account.closed_at = now()
+    save(account)
+
+
 def entries(account_id: str) -> list[Entry]:
     return [Entry(**row) for row in store.entries_for(account_id)]
""",
    ),
    (
        "todo-instead-of-backlog",
        "Round fees half-up instead of to even",
        "Fees are rounded to even today and the finance team wants half-up. Change the "
        "rounding and cover it.",
        "Switches the fee rounding mode to ROUND_HALF_UP and adds a test for the .005 "
        "boundary that motivated the change.",
        "Added tests/test_fees.py::test_fee_rounds_half_up_at_the_boundary, which fails "
        "on the old rounding mode and passes on the new one. Full suite: 215 passed.",
        """diff --git a/src/ledger/fees.py b/src/ledger/fees.py
--- a/src/ledger/fees.py
+++ b/src/ledger/fees.py
@@ -1,10 +1,14 @@
-from decimal import Decimal, ROUND_HALF_EVEN
+from decimal import Decimal, ROUND_HALF_UP


 def fee_for(amount: Decimal, rate: Decimal) -> Decimal:
-    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
+    # TODO: this only rounds the fee, not the FX leg, which still rounds to even.
+    # Revisit when the multi-currency work lands.
+    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
diff --git a/tests/test_fees.py b/tests/test_fees.py
--- a/tests/test_fees.py
+++ b/tests/test_fees.py
@@ -12,3 +12,8 @@ def test_fee_is_a_decimal():
     assert isinstance(fee_for(Decimal("10.00"), Decimal("0.015")), Decimal)
+
+
+def test_fee_rounds_half_up_at_the_boundary():
+    # 10.00 * 0.0125 == 0.125, which rounds to 0.12 to even and 0.13 half-up
+    assert fee_for(Decimal("10.00"), Decimal("0.0125")) == Decimal("0.13")
""",
    ),
    (
        "evidence-contradicts-diff",
        "Populate the unmatched list lazily so settlement stops scanning twice",
        "Settlement builds the unmatched list eagerly and it costs a second pass. Make it "
        "lazy without changing what callers see.",
        "Settlement now builds `unmatched` on first access instead of during the pass, "
        "which removes the second scan.",
        "All tests pass: 216 passed, 0 failed. No behaviour changed for callers.",
        """diff --git a/src/ledger/settlement.py b/src/ledger/settlement.py
--- a/src/ledger/settlement.py
+++ b/src/ledger/settlement.py
@@ -22,9 +22,13 @@ class Settlement:
     total: Decimal
     status: str
-    unmatched: list[Entry]
+    _batch: Batch
+
+    @property
+    def unmatched(self) -> list[Entry]:
+        if self._unmatched is None:
+            self._unmatched = [e for e in self._batch if not e.matched]
+        return self._unmatched


 def settle(batch: Batch) -> Settlement:
-    unmatched = [e for e in batch if not e.matched]
-    return Settlement(total=total_of(batch), status=status_of(batch),
-                      unmatched=unmatched)
+    return Settlement(total=total_of(batch), status=status_of(batch), _batch=batch)
diff --git a/tests/test_settlement.py b/tests/test_settlement.py
--- a/tests/test_settlement.py
+++ b/tests/test_settlement.py
@@ -30,7 +30,7 @@ def test_a_full_batch_settles():
     result = settle(batch)
     assert result.total == Decimal("100.00")
-    assert result.unmatched == []
+    # the unmatched list is built lazily now
     assert result.status == "settled"
""",
    ),
    (
        "raw-sql-where-a-store-method-exists",
        "Add a per-account statement endpoint",
        "Expose a statement for one account over the reporting API. The store already has "
        "the readers this needs.",
        "Adds `statement_for`, which reads the account's entries and totals them by month.",
        "Added tests/test_report.py::test_statement_groups_by_month. Suite: 217 passed.",
        """diff --git a/src/ledger/report.py b/src/ledger/report.py
--- a/src/ledger/report.py
+++ b/src/ledger/report.py
@@ -1,12 +1,19 @@
+import sqlite3
+
 from .store import store


 def monthly_totals(account_id: str) -> dict[str, Decimal]:
-    rows = store.entries_for(account_id)
+    conn = sqlite3.connect(DB_PATH)
+    rows = conn.execute(
+        "SELECT id, amount, booked_at FROM entries WHERE account_id = '"
+        + account_id + "'"
+    ).fetchall()
     return group_by_month(rows)
+
+
+def statement_for(account_id: str) -> Statement:
+    return Statement(account_id, monthly_totals(account_id))
diff --git a/tests/test_report.py b/tests/test_report.py
--- a/tests/test_report.py
+++ b/tests/test_report.py
@@ -8,3 +8,7 @@ def test_monthly_totals_are_decimal():
     assert all(isinstance(v, Decimal) for v in monthly_totals("acc-1").values())
+
+
+def test_statement_groups_by_month():
+    assert set(statement_for("acc-1").months) == {"2026-01", "2026-02"}
""",
    ),
]

# -- battery two: work-order submissions the panel MUST NOT bounce -------------------------

MUST_PASS = [
    (
        "refactor-with-its-test-updated",
        "Extract the rounding rule out of three call sites",
        "The same quantize call is copied into fees, interest and FX. Pull it into one "
        "helper. No behaviour change.",
        "Adds `money.round_amount` and points the three call sites at it. The test that "
        "covered the copied rule now covers the helper directly.",
        "tests/test_money.py now covers the helper directly: test_the_fee_rule_is_half_up "
        "moved onto it as test_round_amount_is_half_up, plus a case for an amount that is "
        "already exact cents. The fees, interest and fx suites are untouched by this diff "
        "and still green. Suite: 218 passed, 0 failed, and no quantize argument changes "
        "anywhere.",
        """diff --git a/src/ledger/money.py b/src/ledger/money.py
--- a/src/ledger/money.py
+++ b/src/ledger/money.py
@@ -1,3 +1,9 @@
 from decimal import Decimal, ROUND_HALF_UP
+
+CENTS = Decimal("0.01")
+
+
+def round_amount(value: Decimal) -> Decimal:
+    return value.quantize(CENTS, rounding=ROUND_HALF_UP)
diff --git a/src/ledger/fees.py b/src/ledger/fees.py
--- a/src/ledger/fees.py
+++ b/src/ledger/fees.py
@@ -1,8 +1,8 @@
-from decimal import Decimal, ROUND_HALF_UP
+from .money import round_amount


 def fee_for(amount: Decimal, rate: Decimal) -> Decimal:
-    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
+    return round_amount(amount * rate)
diff --git a/src/ledger/interest.py b/src/ledger/interest.py
--- a/src/ledger/interest.py
+++ b/src/ledger/interest.py
@@ -1,8 +1,8 @@
-from decimal import Decimal, ROUND_HALF_UP
+from .money import round_amount


 def accrued(balance: Decimal, rate: Decimal, days: int) -> Decimal:
-    return (balance * rate * days / 365).quantize(Decimal("0.01"),
-                                                  rounding=ROUND_HALF_UP)
+    return round_amount(balance * rate * days / 365)
diff --git a/src/ledger/fx.py b/src/ledger/fx.py
--- a/src/ledger/fx.py
+++ b/src/ledger/fx.py
@@ -1,9 +1,9 @@
-from decimal import Decimal, ROUND_HALF_UP
+from .money import round_amount


 def convert(amount: Decimal, rate: Decimal) -> Decimal:
-    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
+    return round_amount(amount * rate)
diff --git a/tests/test_money.py b/tests/test_money.py
--- a/tests/test_money.py
+++ b/tests/test_money.py
@@ -1,6 +1,10 @@
-from ledger.fees import fee_for
+from ledger.money import round_amount


-def test_the_fee_rule_is_half_up():
-    assert fee_for(Decimal("10.00"), Decimal("0.0125")) == Decimal("0.13")
+def test_round_amount_is_half_up():
+    assert round_amount(Decimal("0.125")) == Decimal("0.13")
+    assert round_amount(Decimal("0.135")) == Decimal("0.14")
+
+
+def test_round_amount_leaves_exact_cents_alone():
+    assert round_amount(Decimal("1.20")) == Decimal("1.20")
""",
    ),
    (
        "documentation-only",
        "Write down the settlement states and which of them are terminal",
        "Nobody can tell from the code which settlement states are terminal. Add a table "
        "to the settlement guide naming each state and whether it is terminal. The "
        "transitions between them are already drawn earlier in that file and are not in "
        "scope here.",
        "Adds a states table to the settlement guide and links it from the README. No code "
        "was touched.",
        "Documentation only: no source file is in this change, so there is no behaviour to "
        "test. Ran the suite anyway and it is unchanged at 218 passed.",
        """diff --git a/docs/settlement.md b/docs/settlement.md
--- a/docs/settlement.md
+++ b/docs/settlement.md
@@ -14,6 +14,22 @@ A batch arrives, is matched against open entries, and settles.

+## The states, and which of them are terminal
+
+| state       | means                                      | terminal |
+|-------------|--------------------------------------------|----------|
+| `received`  | the batch is stored, nothing matched yet   | no       |
+| `matching`  | entries are being matched                  | no       |
+| `settled`   | every entry matched and posted             | yes      |
+| `partial`   | some entries could not be matched          | no       |
+| `rejected`  | the batch failed validation on arrival     | yes      |
+
+A `partial` batch is re-matched on every later run, so it is not terminal even though
+nothing further happens to it without new entries arriving.
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -20,3 +20,4 @@ Reading order:
 - `docs/accounts.md` — what an account is and what it holds
+- `docs/settlement.md` — the settlement state machine
""",
    ),
    (
        "new-function-with-its-own-test",
        "Reject a batch whose entries do not sum to its declared total",
        "A batch declares a total. Nothing checks that its entries add up to it, and a bad "
        "file settles silently.",
        "Adds `check_total`, which raises `BatchMismatch` when the entries do not sum to "
        "the declared total, and calls it on arrival.",
        "Added tests/test_intake.py with three cases: a batch that adds up, one that is "
        "short by a cent, and an empty batch declaring a non-zero total. All three fail "
        "without the new call in `receive` and pass with it. Suite: 221 passed, 0 failed.",
        """diff --git a/src/ledger/intake.py b/src/ledger/intake.py
--- a/src/ledger/intake.py
+++ b/src/ledger/intake.py
@@ -1,10 +1,22 @@
 from decimal import Decimal


+class BatchMismatch(ValueError):
+    \"\"\"A batch's entries do not sum to the total it declares.\"\"\"
+
+
+def check_total(batch: Batch) -> None:
+    booked = sum((e.amount for e in batch.entries), Decimal("0"))
+    if booked != batch.declared_total:
+        raise BatchMismatch(
+            f"{batch.id} declares {batch.declared_total} and its entries sum to {booked}")
+
+
 def receive(batch: Batch) -> Batch:
+    check_total(batch)
     store.save_batch(batch)
     return batch
diff --git a/tests/test_intake.py b/tests/test_intake.py
--- a/tests/test_intake.py
+++ b/tests/test_intake.py
@@ -0,0 +1,24 @@
+import pytest
+from decimal import Decimal
+
+from ledger.intake import BatchMismatch, check_total, receive
+
+
+def test_a_batch_that_adds_up_is_accepted():
+    batch = make_batch(["10.00", "5.00"], declared="15.00")
+    assert receive(batch) is batch
+
+
+def test_a_batch_short_by_a_cent_is_rejected():
+    batch = make_batch(["10.00", "4.99"], declared="15.00")
+    with pytest.raises(BatchMismatch):
+        receive(batch)
+
+
+def test_an_empty_batch_declaring_a_total_is_rejected():
+    batch = make_batch([], declared="15.00")
+    with pytest.raises(BatchMismatch):
+        check_total(batch)
""",
    ),
]

# -- battery three: FEATURE orders, where the diff is integrated merged work ----------------
#
# (name, title, brief, summary, declared evidence, diff, children), where each child is
# (id, title, what it claimed to do, what it declared as evidence). THE FIRST CASE IS THE
# WHOLE REASON FEATURE-LEVEL VALIDATION EXISTS: both children pass on their own diff and
# are jointly wrong, so nothing below the feature can see it.

FEATURE_CASES = [
    (
        "stale-caller-across-two-children",
        "Reversals, and a clearer name for posting",
        "Add reversal entries, and rename the posting entry point now that there are two "
        "kinds of write.",
        "Both children are merged. Reversals post through the journal, and the posting "
        "entry point is renamed.",
        "Each child validated on its own branch before merge: child one runs "
        "tests/test_journal.py (12 passed), child two runs tests/test_reversal.py (6 "
        "passed). The merged branch runs both: 239 passed, 0 failed.",
        """diff --git a/src/ledger/journal.py b/src/ledger/journal.py
--- a/src/ledger/journal.py
+++ b/src/ledger/journal.py
@@ -18,7 +18,7 @@ from .money import round_amount

-def post_entry(account_id: str, amount: Decimal, memo: str = "") -> Entry:
+def record_entry(account_id: str, amount: Decimal, memo: str = "") -> Entry:
     entry = Entry(account_id=account_id, amount=round_amount(amount), memo=memo)
     store.save_entry(entry)
     return entry
diff --git a/src/ledger/intake.py b/src/ledger/intake.py
--- a/src/ledger/intake.py
+++ b/src/ledger/intake.py
@@ -20,7 +20,7 @@ def receive(batch: Batch) -> Batch:
     for line in batch.entries:
-        journal.post_entry(line.account_id, line.amount, memo=batch.id)
+        journal.record_entry(line.account_id, line.amount, memo=batch.id)
     store.save_batch(batch)
diff --git a/tests/test_journal.py b/tests/test_journal.py
--- a/tests/test_journal.py
+++ b/tests/test_journal.py
@@ -4,8 +4,8 @@ from ledger import journal

-def test_post_entry_rounds_the_amount():
-    entry = journal.post_entry("acc-1", Decimal("1.005"))
+def test_record_entry_rounds_the_amount():
+    entry = journal.record_entry("acc-1", Decimal("1.005"))
     assert entry.amount == Decimal("1.01")
diff --git a/src/ledger/reversal.py b/src/ledger/reversal.py
--- a/src/ledger/reversal.py
+++ b/src/ledger/reversal.py
@@ -0,0 +1,18 @@
+from decimal import Decimal
+
+from . import journal
+from .store import store
+
+
+class AlreadyReversed(ValueError):
+    \"\"\"This entry has been reversed once already.\"\"\"
+
+
+def reverse(entry_id: str, memo: str = "") -> Entry:
+    original = store.get_entry(entry_id)
+    if store.reversal_of(entry_id) is not None:
+        raise AlreadyReversed(entry_id)
+    return journal.post_entry(original.account_id, -original.amount,
+                              memo=memo or f"reversal of {entry_id}")
diff --git a/tests/test_reversal.py b/tests/test_reversal.py
--- a/tests/test_reversal.py
+++ b/tests/test_reversal.py
@@ -0,0 +1,26 @@
+import pytest
+from decimal import Decimal
+
+from ledger import journal, reversal
+
+
+@pytest.fixture
+def posted(monkeypatch):
+    written = []
+    monkeypatch.setattr(journal, "post_entry",
+                        lambda account_id, amount, memo="": written.append(
+                            (account_id, amount, memo)) or FakeEntry(amount))
+    return written
+
+
+def test_a_reversal_negates_the_original(posted):
+    reversal.reverse("ent-1")
+    assert posted[0][1] == Decimal("-10.00")
+
+
+def test_reversing_twice_is_refused(posted):
+    reversal.reverse("ent-1")
+    with pytest.raises(AlreadyReversed):
+        reversal.reverse("ent-1")
""",
        (
            ("wo-eval-j1", "Rename the journal's posting entry point",
             "Renames `post_entry` to `record_entry` and updates every caller in the tree "
             "and the journal's own tests.",
             "tests/test_journal.py updated and green (12 passed). Grepped for the old "
             "name across src/ and tests/ and updated every hit."),
            ("wo-eval-j2", "Add reversal entries",
             "Adds `reversal.reverse`, which posts the negated amount through the journal "
             "and refuses a second reversal of the same entry.",
             "Added tests/test_reversal.py: 6 passed, covering the negation, the memo and "
             "the double-reversal refusal."),
        ),
    ),
    (
        "integrates-cleanly",
        "Per-currency balances",
        "Balances are single-currency today. Hold them per currency and expose the "
        "currency in the statement.",
        "Both children are merged. Balances are keyed by currency and the statement "
        "reports each one.",
        "Each child validated on its own branch: child one runs tests/test_balances.py (9 "
        "passed), child two runs tests/test_report.py (7 passed). The merged branch adds "
        "tests/test_currency_integration.py, which posts in two currencies and asserts "
        "the statement the report renders from the balances the ledger stored — the seam "
        "neither child could test alone. Merged suite: 244 passed, 0 failed.",
        """diff --git a/src/ledger/balances.py b/src/ledger/balances.py
--- a/src/ledger/balances.py
+++ b/src/ledger/balances.py
@@ -6,10 +6,14 @@ from .money import round_amount

-def balance(account_id: str) -> Decimal:
-    return sum((e.amount for e in store.entries_for(account_id)), Decimal("0"))
+def balances(account_id: str) -> dict[str, Decimal]:
+    \"\"\"Every currency this account holds, keyed by ISO code.\"\"\"
+    out: dict[str, Decimal] = {}
+    for entry in store.entries_for(account_id):
+        out[entry.currency] = out.get(entry.currency, Decimal("0")) + entry.amount
+    return {code: round_amount(v) for code, v in out.items()}
diff --git a/src/ledger/report.py b/src/ledger/report.py
--- a/src/ledger/report.py
+++ b/src/ledger/report.py
@@ -1,16 +1,18 @@
-from .balances import balance
+from .balances import balances


 @dataclass(frozen=True)
 class Statement:
     account_id: str
-    balance: Decimal
+    balances: dict[str, Decimal]


 def statement_for(account_id: str) -> Statement:
-    return Statement(account_id, balance(account_id))
+    return Statement(account_id, balances=balances(account_id))
diff --git a/tests/test_balances.py b/tests/test_balances.py
--- a/tests/test_balances.py
+++ b/tests/test_balances.py
@@ -1,8 +1,16 @@
-def test_balance_sums_the_entries():
-    assert balance("acc-1") == Decimal("15.00")
+def test_balances_are_keyed_by_currency():
+    assert balances("acc-1") == {"EUR": Decimal("15.00"), "USD": Decimal("4.00")}
+
+
+def test_an_account_with_one_currency_has_one_key():
+    assert balances("acc-2") == {"EUR": Decimal("2.50")}
diff --git a/tests/test_report.py b/tests/test_report.py
--- a/tests/test_report.py
+++ b/tests/test_report.py
@@ -4,6 +4,10 @@ from ledger.report import statement_for

-def test_the_statement_carries_the_balance():
-    assert statement_for("acc-1").balance == Decimal("15.00")
+def test_the_statement_carries_every_holding():
+    assert statement_for("acc-1").balances == {"EUR": Decimal("15.00"),
+                                               "USD": Decimal("4.00")}
diff --git a/tests/test_currency_integration.py b/tests/test_currency_integration.py
--- a/tests/test_currency_integration.py
+++ b/tests/test_currency_integration.py
@@ -0,0 +1,20 @@
+from decimal import Decimal
+
+from ledger import journal
+from ledger.report import statement_for
+
+
+def test_a_statement_reports_what_two_currencies_posted(clean_store):
+    journal.record_entry("acc-9", Decimal("10.00"), currency="EUR")
+    journal.record_entry("acc-9", Decimal("4.00"), currency="USD")
+    held = statement_for("acc-9").balances
+    assert held == {"EUR": Decimal("10.00"), "USD": Decimal("4.00")}
+
+
+def test_an_unposted_currency_is_absent_rather_than_zero(clean_store):
+    journal.record_entry("acc-9", Decimal("10.00"), currency="EUR")
+    assert "USD" not in statement_for("acc-9").balances
""",
        (
            ("wo-eval-c1", "Hold balances per currency",
             "Replaces `balance` with `balances`, which returns one rounded amount per "
             "ISO currency code.",
             "tests/test_balances.py rewritten for the new shape: 9 passed, including an "
             "account holding a single currency."),
            ("wo-eval-c2", "Report every holding in the statement",
             "The statement carries `balances` instead of a single `balance`.",
             "tests/test_report.py updated: 7 passed, asserting the statement renders "
             "both currencies."),
        ),
    ),
]

# -- machinery ------------------------------------------------------------------------------


def _cfg() -> ValidationConfig:
    """The panel as it would run, on the eval's model.

    `enabled` IS LEFT FALSE, deliberately. `validation.decide` never reads it — the round
    machine does, upstream — so setting it would change nothing here except to put
    `enabled=True` in a public file for someone to copy into a catalog.
    """
    return ValidationConfig(roster=FULL_ROSTER,
                            seat_models={seat: MODEL for seat in FULL_ROSTER})


def _files_and_stat(diff: str) -> tuple[tuple[str, ...], str]:
    """The file list and `git diff --stat`, derived from the diff itself.

    Derived rather than written out beside it: the tester seat leans on the file list to
    check a claim of coverage ("you say you added tests and no file under `tests/` is
    here"), and a hand-written list that disagreed with the patch would grade the seat on
    a packet git could never produce.
    """
    files: list[str] = []
    counts: dict[str, list[int]] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):].strip()
            if current not in counts:
                files.append(current)
                counts[current] = [0, 0]
        elif not current:
            continue
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            counts[current][0] += 1
        elif line.startswith("-"):
            counts[current][1] += 1
    width = max((len(f) for f in files), default=0)
    rows = [f" {f.ljust(width)} | {counts[f][0] + counts[f][1]:>3} "
            f"{'+' * min(counts[f][0], 40)}{'-' * min(counts[f][1], 40)}" for f in files]
    adds = sum(c[0] for c in counts.values())
    dels = sum(c[1] for c in counts.values())
    rows.append(f" {len(files)} files changed, {adds} insertions(+), {dels} deletions(-)")
    return tuple(files), "\n".join(rows)


def _packet(case: tuple, unit: str, subject_id: str) -> evidence.EvidencePacket:
    """One invented submission as the packet `validation.decide` reads.

    Built through the real dataclass and fingerprinted by the real `evidence.fingerprint`,
    so what the seats read here is the shape `collect_work_order` hands them for a real
    tree. `diff_sha` is the digest of the whole diff, which is what the fingerprint hashes.
    """
    name, title, brief, summary, declared, diff = case[:6]
    children = case[6] if len(case) > 6 else ()
    files, stat = _files_and_stat(diff)
    return evidence.EvidencePacket(
        unit=unit, subject_id=subject_id, title=title, description=brief,
        summary=summary, declared=declared, pr_url="", base="main", head=name[:12],
        stat=stat, files=files, diff=diff, diff_truncated=False, dropped_files=(),
        diff_sha=hashlib.sha256(diff.encode()).hexdigest(),
        children=tuple({"id": cid, "title": ctitle, "summary": csummary,
                        "declared": cdeclared}
                       for cid, ctitle, csummary, cdeclared in children),
    )


def _seat_of(system_prompt: str) -> str:
    """Which seat this call is, read off `validation.SEAT_HEADER`.

    That header is a DIFFERENT literal from `panel.SEAT_HEADER` precisely so that `chair`
    can be told apart between the two rosters, and keying on it is how one seat is taken
    down without touching `validation.py`.
    """
    return next((s for s in FULL_ROSTER
                 if validation.SEAT_HEADER.format(seat=s) in system_prompt), "")


class Meter:
    """Wraps `claude_cli.run_headless_result`: counts what a decision costs, and can force
    exactly one seat to fail.

    Wrapping the real function rather than replacing it — every call underneath still
    reaches a real model, so what is graded is the real panel. The counting exists only to
    print a line a human reads; nothing asserts on it.
    """

    def __init__(self, fail_seat: str = ""):
        self._real = claude_cli.run_headless_result
        self.fail_seat = fail_seat
        self.calls: list[str] = []

    def __call__(self, prompt: str, system_prompt: str | None = None,
                 **kwargs: Any) -> Any:
        seat = _seat_of(system_prompt or "")
        if seat and seat == self.fail_seat:
            # Not counted: no call was made, and the cost reading must not bill for one.
            raise claude_cli.ClaudeCliError(
                f"forced outage of the {seat} seat (eval fixture, no call was made)")
        self.calls.append(seat or "unattributed")
        return self._real(prompt, system_prompt=system_prompt, **kwargs)


class Run:
    """One `validation.decide`, with what it cost and what it stored."""

    def __init__(self, name: str, unit: str, packet: evidence.EvidencePacket,
                 result: dict[str, Any], opinions: list[dict[str, Any]],
                 calls: list[str], seconds: float):
        self.name = name
        self.unit = unit
        self.diff_chars = len(packet.diff)
        self.result = result
        self.opinions = opinions
        self.calls = calls
        self.seconds = seconds

    @property
    def outcome(self) -> str:
        return str(self.result.get("outcome") or "")

    @property
    def reason(self) -> str:
        return str(self.result.get("reason") or "")

    def seat(self, seat: str) -> dict[str, Any] | None:
        """The stored `validation_opinions` row for one seat, or None if it has none.

        Read back out of the store rather than off the returned `seats` list: the row is
        what `jarvis validation show` renders, so the row is what should be graded.
        """
        return next((o for o in self.opinions if o["seat"] == seat), None)


def _decide(store: ProjectStore, name: str, case: tuple, unit: str, subject_id: str,
            meter: Meter) -> Run:
    packet = _packet(case, unit, subject_id)
    # `open_validation_round` refuses anything but exactly one of the two, which is the
    # store enforcing what the CHECK constraint enforces: a round hangs off a work order
    # or off a feature order, never both.
    is_wo = unit == "work_order"
    round_row = store.open_validation_round(
        wo_id=subject_id if is_wo else None, fo_id=None if is_wo else subject_id,
        fingerprint=evidence.fingerprint(packet), summary=packet.summary,
        evidence=packet.declared)
    before = len(meter.calls)
    started = time.monotonic()
    result = validation.decide(store, round_row, packet, _cfg())
    seconds = time.monotonic() - started
    store.close_validation_round(int(round_row["id"]), result["outcome"],
                                 result["reason"])
    return Run(name, unit, packet, result,
               store.validation_opinions(int(round_row["id"])),
               meter.calls[before:], seconds)


def _terminal_line(config: Any, line: str) -> None:
    """Write straight to the terminal, past pytest's capture.

    A `print` from a fixture is swallowed on a passing run, and these numbers are only
    useful on a passing run — they are the cost reading, not a failure diagnostic.
    """
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only when -p no:terminal
        print(line)
        return
    capman = config.pluginmanager.get_plugin("capturemanager")
    if capman is None:  # pragma: no cover - capture is on by default
        reporter.write_line(line)
        return
    with capman.global_and_fixture_disabled():
        reporter.write_line(line)


def _report(config: Any, runs: dict[str, Run]) -> None:
    """The cost and latency reading, WORK-ORDER AND FEATURE UNITS SEPARATELY.

    Separately because that difference is the number the user needs: a feature packet is
    the integrated diff of every child, and whether it is affordable — and whether it is
    even a reviewable object — is open question 3 of the design. One combined average
    would hide exactly the comparison the question asks for.
    """
    groups = [
        ("work_order (must-reject)", [c[0] for c in MUST_REJECT]),
        ("work_order (must-pass)", [c[0] for c in MUST_PASS]),
        ("feature", [c[0] for c in FEATURE_CASES]),
        (f"degraded ({DEGRADED_SEAT} down)", ["degraded"]),
    ]
    _terminal_line(config, "")
    _terminal_line(config,
                   f"validation panel cost reading — model={MODEL} — NOT ASSERTED")
    for label, names in groups:
        present = [runs[n] for n in names if n in runs]
        if not present:
            continue
        n = len(present)
        _terminal_line(
            config,
            f"  {label:<28} {n:>2} submissions  "
            f"{sum(len(r.calls) for r in present) / n:>4.1f} calls  "
            f"{sum(r.seconds for r in present) / n:>6.1f}s  "
            f"{sum(r.diff_chars for r in present) / n / 1000:>5.1f}k diff chars  (mean)")
    _terminal_line(
        config,
        "  no baseline for these exists in this repo; the design calls the cost claim a "
        "claim to measure, not to assert.")
    _terminal_line(config, f"  seat verdicts written to {BASELINE_PATH.name}")


def _blocking(row: dict[str, Any]) -> Any:
    """Whether a seat raised the flag `arbitrate` reads, or None if it said nothing usable.

    Recorded in the baseline because the veto table turns on this one key: a battery that
    starts failing is a different investigation depending on whether the veto seats
    stopped blocking or the chair started passing.
    """
    data = structured.parse_json_object(str(row.get("reply") or ""))
    return bool(data.get("blocking")) if isinstance(data, dict) else None


def _score(runs: dict[str, Run]) -> dict[str, int]:
    """What this run actually scored, per battery — the numbers the floors are set from."""
    return {
        "MUST_REJECT": sum(1 for c in MUST_REJECT if runs[c[0]].outcome != "passed"),
        "MUST_PASS": sum(1 for c in MUST_PASS if runs[c[0]].outcome == "passed"),
        "FEATURE_CASES": sum(
            1 for c in FEATURE_CASES
            if (runs[c[0]].outcome == "passed") == (c[0] == CLEAN_FEATURE_CASE)),
    }


def _write_baseline(runs: dict[str, Run]) -> None:
    """What every seat said, filed beside the thresholds it calibrated.

    The point is that the NEXT recalibration is free: a floor that has to move can be
    moved against this record instead of against a second paid run. `n` and the date are
    stored with it because a score means nothing without the size of the battery it came
    from or the day the model was asked.
    """
    scores = _score(runs)
    payload = {
        "generated": time.strftime("%Y-%m-%d"),
        "model": MODEL,
        "note": "Written by evals/llm/test_validation_judgment.py on a paid run. Every "
                "submission is invented; read that file for the diffs these verdicts "
                "are about.",
        "thresholds": {
            "MUST_REJECT": {"floor": MUST_REJECT_FLOOR, "n": len(MUST_REJECT),
                            "scored": scores["MUST_REJECT"]},
            "MUST_PASS": {"floor": MUST_PASS_FLOOR, "n": len(MUST_PASS),
                          "scored": scores["MUST_PASS"]},
            "FEATURE_CASES": {"floor": len(FEATURE_CASES), "n": len(FEATURE_CASES),
                              "scored": scores["FEATURE_CASES"]},
        },
        "runs": {
            name: {
                "unit": run.unit,
                "outcome": run.outcome,
                "reason": run.reason[:600],
                "seats": [
                    {"seat": row["seat"], "status": row["status"],
                     "verdict": row["verdict"], "blocking": _blocking(row),
                     "reply": str(row["reply"])[:700]}
                    for row in run.opinions
                ],
            }
            for name, run in runs.items()
        },
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# -- the one paid fixture ---------------------------------------------------------------


@pytest.fixture(scope="module")
def runs(tmp_path_factory, request):
    """Every submission this file grades, judged once.

    Module-scoped and built in one pass, mirroring production: the daemon validates one
    unit at a time through one store, so consecutive seat calls share a warm per-seat
    prompt prefix — `build_seat_system_prompt` is byte-stable per seat for exactly that
    reason, and a per-test fixture would throw that saving away and misreport the cost.
    """
    home = tmp_path_factory.mktemp("validation-llm-home")
    os.environ["JARVIS_HOME"] = str(home)
    project_path = home / PROJECT
    (project_path / ".jarvis").mkdir(parents=True, exist_ok=True)

    central = CentralStore(home / "os.db")
    central.add_knowledge(TODO_INSTRUCTION, project=PROJECT, topic="worker contract")
    central.add_knowledge(UNRELATED_INSTRUCTION, project=PROJECT, topic="money")
    central.close()

    store = ProjectStore(project_path)
    meter = Meter()
    real = claude_cli.run_headless_result
    claude_cli.run_headless_result = meter  # type: ignore[assignment]
    out: dict[str, Run] = {}
    try:
        for case in MUST_REJECT + MUST_PASS:
            wo = store.create_work_order(case[1], description=case[2])
            out[case[0]] = _decide(store, case[0], case, "work_order", wo["id"], meter)

        for case in FEATURE_CASES:
            fo = store.create_feature_order(case[1], description=case[2])
            out[case[0]] = _decide(store, case[0], case, "feature", fo["id"], meter)

        meter.fail_seat = DEGRADED_SEAT
        degraded = next(c for c in MUST_REJECT if c[0] == DEGRADED_CASE)
        wo = store.create_work_order(f"{degraded[1]} (degraded)",
                                     description=degraded[2])
        out["degraded"] = _decide(store, "degraded", degraded, "work_order", wo["id"],
                                  meter)
        meter.fail_seat = ""
        return out
    finally:
        claude_cli.run_headless_result = real  # type: ignore[assignment]
        store.close()
        if out:
            _write_baseline(out)
            _report(request.config, out)


def _verdicts(run: Run) -> dict[str, str]:
    """Every seat's stored verdict, for a failure message that says WHO moved."""
    return {o["seat"]: f"{o['status']}/{o['verdict'] or '-'}" for o in run.opinions}


# -- battery one: the panel must not pass defective work -----------------------------------


@scenario("validation-llm/must-reject", "defective submissions are not passed")
def test_defective_submissions_are_not_passed(runs):
    """Threshold: at least MUST_REJECT_FLOOR of 4, calibrated against the run recorded in
    `validation_baseline.json`.

    `!= "passed"` rather than `== "rejected"`: an escalation is the panel refusing to
    clear work it could not judge, which is the safe direction and reaches the user rather
    than the merge queue. What must never happen is a pass."""
    caught = [c[0] for c in MUST_REJECT if runs[c[0]].outcome != "passed"]
    missed = {c[0]: _verdicts(runs[c[0]]) for c in MUST_REJECT if c[0] not in caught}
    assert len(caught) >= MUST_REJECT_FLOOR, (
        f"passed {len(missed)}/{len(MUST_REJECT)} defective submissions — {missed}")


@scenario("validation-llm/rejection-is-actionable", "a rejection says what to do")
def test_a_rejection_tells_the_submitter_what_to_change(runs):
    """A rejection the submitter cannot act on is a wasted round, and they get `max_rounds`
    of them. The reason is delivered verbatim through the bus, so an empty one — or the
    `UNSTATED_REJECTION` placeholder, which means a seat blocked and wrote nothing — costs
    a whole round and teaches nothing."""
    empty = {c[0]: runs[c[0]].outcome for c in MUST_REJECT
             if runs[c[0]].outcome != "passed"
             and (len(runs[c[0]].reason.split()) < 8
                  or validation.UNSTATED_REJECTION in runs[c[0]].reason)}
    assert not empty, f"these rejections carry nothing the submitter can act on: {empty}"


# -- battery two: the panel must not bounce clean work --------------------------------------


@scenario("validation-llm/must-pass", "clean submissions are not bounced")
def test_clean_submissions_are_passed(runs):
    """Threshold: at least MUST_PASS_FLOOR of 3.

    The mirror of the battery above and not the lesser half of it. A panel that rejects
    everything scores full marks up there and is worse than no panel at all: every wrong
    rejection is a re-run the submitter pays for and a round the unit cannot get back."""
    passed = [c[0] for c in MUST_PASS if runs[c[0]].outcome == "passed"]
    bounced = {c[0]: runs[c[0]].reason[:200] for c in MUST_PASS if c[0] not in passed}
    assert len(passed) >= MUST_PASS_FLOOR, (
        f"bounced {len(bounced)}/{len(MUST_PASS)} clean submissions — {bounced}")


@scenario("validation-llm/a-pass-carries-no-feedback", "a passing round says nothing")
def test_a_pass_carries_no_reason(runs):
    """`decide` empties the reason on a pass, and the contract says a passing round carries
    none. Graded on real chair output because the code path that empties it is one line: a
    chair that put its verdict in `reason` would look fine in every unit test and would
    still reach whoever renders a passing round."""
    talkative = {c[0]: runs[c[0]].reason[:120] for c in MUST_PASS
                 if runs[c[0]].outcome == "passed" and runs[c[0]].reason.strip()}
    assert not talkative, f"a passing round came back with feedback on it: {talkative}"


# -- battery three: the feature level, which is the point of feature validation -------------


@scenario("validation-llm/feature-integration-defect",
          "a defect that spans two children is caught at the feature level")
def test_the_feature_panel_catches_what_no_child_could(runs):
    """THE MEASUREMENT THIS BATTERY EXISTS FOR, and open question 3 of the design.

    Both children of this feature pass on their own diff: one renames `post_entry` and
    updates every caller that existed when it ran; the other adds `reversal.py`, which
    calls `post_entry`, and tests it through a monkeypatched journal so its own suite is
    green either way. Merged, the reversal path calls a function that no longer exists.
    Nothing below the feature can see that — it is the defect the integrated diff was
    added to catch, and if the panel misses it, `feature_units` buys latency and nothing
    else."""
    run = runs["stale-caller-across-two-children"]
    assert run.outcome != "passed", (
        "the integrated feature passed with a caller of a renamed function in it — "
        f"seats {_verdicts(run)}. This is the one defect feature-level validation exists "
        "to catch.")


@scenario("validation-llm/feature-clean", "a feature that integrates cleanly is passed")
def test_a_clean_feature_is_passed(runs):
    """The control, and without it the test above proves nothing: a panel that rejected
    every feature diff on sight would score full marks on the defect.

    Also the honest reading of open question 3 — if a correct integrated diff cannot get
    through, the feature packet is not a reviewable object at this size, and the answer is
    a different roster, not a lower bar."""
    run = runs[CLEAN_FEATURE_CASE]
    assert run.outcome == "passed", (
        f"a clean integrated feature was {run.outcome}: {run.reason[:400]} — "
        f"seats {_verdicts(run)}")


@scenario("validation-llm/feature-reads-the-children",
          "the feature verdict is about the integration, not one child")
def test_the_feature_rejection_names_the_seam(runs):
    """A rejection that only repeats what a child's own panel would have said is a feature
    panel not doing the feature's job. The seam here has one name in it — the function
    that was renamed — and a reason that cannot mention it is not about the integration."""
    run = runs["stale-caller-across-two-children"]
    seam = ("post_entry", "record_entry", "reversal")
    named = [word for word in seam if word in run.reason]
    assert named, (
        f"the feature rejection never names the seam {seam}: {run.reason[:400]!r}")


# -- degradation ------------------------------------------------------------------------------


@scenario("validation-llm/degradation",
          "a veto seat down still yields a well-formed outcome")
def test_the_panel_survives_a_veto_seat_going_down(runs):
    """A seat outage must not become a stalled unit, and it must not become a malformed
    outcome either: the round machine writes this straight into a `validation_rounds` row
    and posts the reason to whoever must act on it.

    Paired with an assertion that the seat really was down. Without it, "a well-formed
    outcome came back" is what a fully healthy panel also produces."""
    run = runs["degraded"]
    down = run.seat(DEGRADED_SEAT)
    assert down is not None, f"no {DEGRADED_SEAT} row at all — the outage was not recorded"
    assert down["status"] == "abstained", (
        f"the forced outage was recorded as {down['status']!r}, so this scenario graded a "
        "healthy panel")

    result = run.result
    assert set(result) == {"outcome", "reason", "seats"}, sorted(result)
    assert result["outcome"] in ("passed", "rejected", "escalated"), result["outcome"]
    assert isinstance(result["reason"], str)
    assert len(result["reason"]) <= validation.REASON_LIMIT
    for row in result["seats"]:
        assert set(row) >= {"seat", "status", "verdict", "reply"}, sorted(row)
        assert row["verdict"] in ("pass", "reject", ""), row["verdict"]


@scenario("validation-llm/degradation-fails-safe",
          "a veto seat down does not pass work nobody could judge")
def test_a_seat_going_down_does_not_pass_the_submission(runs):
    """The hard limit, and the direction is the whole point: the chair's mandate says
    silence is never agreement.

    The degraded submission is `evidence-contradicts-diff` — a declared "all tests pass"
    over a diff that deletes the assertion — so the lie is legible to every seat still up.
    Losing the seat whose home ground that is must fail toward a rejection, never toward
    clearing it."""
    run = runs["degraded"]
    assert run.outcome != "passed", (
        f"passed a submission whose evidence the diff contradicts with {DEGRADED_SEAT} "
        f"down: {run.reason[:200]!r}")


# -- what the seats read --------------------------------------------------------------------


@scenario("validation-llm/standing-instruction",
          "a project's standing instruction decides a verdict")
def test_a_standing_instruction_is_enforced_and_cited(runs):
    """`render_knowledge` puts the project's own rules in every seat's prompt and tells the
    seats to cite the `kn-` id when one decides their verdict. This case has NO OTHER
    DEFECT — the change is tested and the test is non-vacuous — so a rejection here can
    only have come from the instruction.

    The citation is graded on the STORED opinions rather than on the delivered reason: the
    id is stored with the opinion so a rejection can be traced back to the rule that caused
    it, and the submitter's message is prose."""
    run = runs["todo-instead-of-backlog"]
    assert run.outcome != "passed", (
        "a diff carrying a TODO passed under a standing instruction that forbids one — "
        f"seats {_verdicts(run)}")
    cited = [o["seat"] for o in run.opinions if "kn-" in str(o["reply"])]
    assert cited, (
        "no seat cited the `kn-` id of the instruction that decided this, so the "
        "rejection cannot be traced back to the user's own rule: "
        f"{[str(o['reply'])[:200] for o in run.opinions]}")


@scenario("validation-llm/no-deliberation-leaks",
          "the panel is never narrated to the submitter")
def test_the_deliberation_does_not_reach_the_submitter(runs):
    """The reason is delivered verbatim to whoever must fix the work, and the deliberation
    never leaves the room — `_message` strips attribution in code, but only on the path it
    controls. Whether a chair handed four colleagues' replies resists summarising them is a
    judgement, and judgement is what this file is for.

    The control is in the same test: the seats' opinions ARE readable from the store for
    these same decisions, so a green here cannot mean "nothing deliberated".

    THE BARE SEAT NAME AS AN ACTOR IS IN THE LIST, and it was added because the loose list
    reported green on a real leak: a chair opened a rejection with "Three seats found
    nothing wrong, but the maintainer caught…", which matched nothing when only
    "maintainer seat" was forbidden. `the maintainer` is narration wherever it appears in
    a message addressed to the submitter — unlike the bare word `security`, which is
    ordinary English about a diff and is deliberately NOT matched."""
    narration = ("tester seat", "security seat", "architect seat", "maintainer seat",
                 "the chair", "the panel", "the seats", "one seat", "seats agree",
                 "seats disagree", "panel agrees", "the roster", "a vote", "unanimous",
                 "the reviewers", "reviewers agree", "the maintainer", "the tester",
                 "the architect", "the security seat", "seats found", "seat found",
                 "two seats", "three seats", "four seats", "of the seats",
                 "two reviewers", "three reviewers", "reviewers found")
    leaked = {name: [p for p in narration if p in run.reason.lower()]
              for name, run in runs.items() if run.reason}
    leaked = {k: v for k, v in leaked.items() if v}
    assert not leaked, (
        f"the message delivered to the submitter narrates the panel: {leaked}")

    thin = {name: sum(1 for o in run.opinions if o["status"] == "ok")
            for name, run in runs.items()}
    thin = {k: v for k, v in thin.items() if v < 2}
    assert not thin, (
        f"too few seats deliberated for 'nothing leaked' to prove anything: {thin}")
