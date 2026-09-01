"""`/alarms`, the per-alarm page and the review loop — §5 of
docs/superpowers/specs/2026-08-31-the-supervisor.md.

THE SUPERVISOR IS NEVER RUN HERE, and that is the design, not a shortcut. Every column
it writes is fillable with `ProjectStore.update_alarm`, so a fixture states the verdict
under test outright instead of paying a model call to be told one — and the page is
proved to work with `supervisor.py` absent, which is how it ships.

The fixture strings are DISTINCTIVE on purpose. Jinja renders a missing key as the empty
string, so `assert note in page` is trivially true when the note is `""`; every
assertion below names a string that appears nowhere else, and `NOWHERE` is the negative
control that proves the page is not simply echoing everything it is handed.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

WHY = "eleven minutes of a design review is the shape of the work, not a stall"
NOTE = "the design doc is long on purpose"
FIRED = "turn 1 has been running 2h and is still being billed"
NOWHERE = "no fixture anywhere in this module says this sentence"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _alarm(title="a very slow design", *, live=True, decided=False,
           status="raised", verdict=None, qid=None):
    """One work order carrying one alarm, in whatever state the test needs.

    Returns (wo_id, alarm_id). `live` is the ORDER's attention flag, which is what the
    page's first half is keyed on — never the row's own status.
    """
    wo = ops.create_work_order("proj_a", title)
    path = ops.find_work_order(wo["id"])[1]
    store = ProjectStore(path)
    try:
        alarm = store.add_alarm(wo["id"], "long-turn", 1, FIRED)
        if live:
            store.flag_attention(wo["id"], "a turn is still being billed")
        if decided:
            store.update_alarm(alarm["id"], status=status, verdict=verdict or "ack",
                               verdict_reason=WHY, note=NOTE, decided_at=alarm["ts"],
                               neo_question_id=qid)
    finally:
        store.close()
    return wo["id"], alarm["id"]


def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def _review_block(page: str, alarm_id: str) -> str:
    """The review control's markup for one alarm, whichever page it came off."""
    start = page.index(f'id="review-{alarm_id}"')
    start = page.rindex("<div", 0, start)
    return page[start:page.index("</div>", page.index("</form>", start))]


def _row(alarm_id, wo_id):
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        return store.get_alarm(alarm_id)
    finally:
        store.close()


# -- the anchor: one alarm, at a URL -----------------------------------------------


def test_the_per_alarm_page_carries_the_whole_decision(started):
    """Two siblings emit links at this exact URL shape and neither can prove they
    resolve — the timeline's `ref` and a Neo escalation's inbox line. This is that
    proof, and the reason `/alarms` alone was never enough: a list has no anchor, so a
    'review it →' into one opens on whichever row sorts first."""
    wo_id, alarm_id = _alarm(decided=True, status="acked")

    page = _client().get(f"/alarms/proj_a/{alarm_id}")

    assert page.status_code == 200
    assert FIRED in page.text
    assert WHY in page.text, "the supervisor's argument"
    assert NOTE in page.text, "and what it wrote to the user — different sentences"
    assert wo_id in page.text
    # The negative control. Without it every assertion above is also passed by a page
    # that prints its whole context dict.
    assert NOWHERE not in page.text


def test_an_alarm_that_is_not_there_is_a_404_and_not_a_traceback(started):
    page = _client().get("/alarms/proj_a/al-nosuchthing")

    assert page.status_code == 404
    assert "al-nosuchthing" in page.text


def test_the_page_still_reads_when_no_supervisor_has_ever_looked(started):
    """With the supervisor off — which is how it ships — every column it writes is
    NULL, and the page must say so rather than render three empty rows."""
    _, alarm_id = _alarm()

    page = _client().get(f"/alarms/proj_a/{alarm_id}").text

    assert "the supervisor has not decided this alarm" in page
    assert 'name="decision"' not in page, "nothing to approve yet"


# -- the list, and the half that must not appear when it is empty ------------------


def test_the_alarms_page_degrades_to_exactly_the_page_that_exists_today(started):
    """`status_code == 200` proves nothing about 'unchanged'. These four strings are
    the ones §1 froze, asserted verbatim, plus the new heading's ABSENCE — a permanent
    empty panel would teach the reader to skip the region the asks appear in."""
    wo_id, _ = _alarm()

    page = _client().get("/alarms").text

    assert "a very slow design" in page
    assert FIRED in page
    assert f'action="/wo/proj_a/{wo_id}/ack"' in page
    assert 'name="back" value="alarms"' in page
    assert 'alarms<span class="nav-badge">1</span>' in page.replace(" <span", "<span")

    assert "Addressed by the supervisor" not in page, \
        "no acked+unreviewed row, so no second half at all"


def test_the_second_half_appears_only_for_an_acked_unreviewed_alarm(started):
    _, alarm_id = _alarm(decided=True, status="acked")

    page = _client().get("/alarms").text

    assert "Addressed by the supervisor, awaiting your feedback" in page
    assert WHY in page and NOTE in page
    assert f'action="/alarms/proj_a/{alarm_id}/review"' in page

    ops.review_alarm(alarm_id, approved=True, project_name="proj_a")

    after = _client().get("/alarms").text
    assert "Addressed by the supervisor, awaiting your feedback" not in after
    assert alarm_id in after, "but it is on the record, with the verdict"


def test_an_escalated_alarm_is_not_asked_about_twice(started):
    """It is still with Neo and the attention flag is already asking about it. In the
    feedback queue as well, the user would be asked for one decision in two words."""
    _alarm(decided=True, status="escalated", verdict="escalate")

    assert [a["id"] for a in ops.alarm_review_queue()] == []


# -- the control is one macro, and this is the only observable form of that ---------


def test_the_review_control_is_byte_identical_on_both_surfaces(started):
    """'It is a macro' is not testable; this is. The property `_question.html` exists
    to guarantee for a Neo question, held for an alarm."""
    _, alarm_id = _alarm(decided=True, status="acked")
    client = _client()

    on_list = _review_block(client.get("/alarms").text, alarm_id)
    on_page = _review_block(client.get(f"/alarms/proj_a/{alarm_id}").text, alarm_id)

    # `next` is the one field that is deliberately per-surface — the reader returns to
    # the page they decided from. Only that VALUE is normalised, and both are pinned
    # below, so nothing else can differ without this failing.
    assert (on_list.replace('value="/alarms"', 'value="«back»"')
            == on_page.replace(f'value="/alarms/proj_a/{alarm_id}"', 'value="«back»"'))
    assert 'name="next" value="/alarms"' in on_list
    assert f'name="next" value="/alarms/proj_a/{alarm_id}"' in on_page


# -- the review itself -------------------------------------------------------------


def test_correcting_from_the_list_returns_to_the_list_and_records_it(started):
    wo_id, alarm_id = _alarm(decided=True, status="acked")

    posted = _client().post(f"/alarms/proj_a/{alarm_id}/review",
                            data={"decision": "correct", "next": "/alarms",
                                  "feedback": NOWHERE})

    assert posted.status_code == 303
    assert posted.headers["location"] == "/alarms"
    row = _row(alarm_id, wo_id)
    assert row["review_status"] == "corrected"
    assert row["review_feedback"] == NOWHERE
    assert row["reviewed_at"] is not None


@pytest.mark.parametrize("next_, lands", [
    ("/alarms", "/alarms"),
    ("//evil.example", None),
    ("https://evil.example", None),
    ("", None),
])
def test_the_redirect_honours_same_site_paths_only(started, next_, lands):
    """A form field is attacker-settable, so one happy case grades nothing."""
    _, alarm_id = _alarm(decided=True, status="acked")
    expected = lands or f"/alarms/proj_a/{alarm_id}"

    posted = _client().post(f"/alarms/proj_a/{alarm_id}/review",
                            data={"decision": "approve", "next": next_})

    assert posted.headers["location"] == expected


def test_a_refused_review_flashes_the_error_through_the_same_redirect(started):
    """`base.html` renders `?error=` on every page, so the failure has to ride the
    redirect rather than 500 — and it must land where the reader was."""
    _, alarm_id = _alarm(decided=True, status="acked")

    posted = _client().post(f"/alarms/proj_a/{alarm_id}/review",
                            data={"decision": "correct", "next": "/alarms",
                                  "feedback": "   "})

    assert posted.status_code == 303
    assert posted.headers["location"].startswith("/alarms?error=")
    assert "what should the supervisor" in unquote(posted.headers["location"])


def test_a_correction_with_no_feedback_leaves_the_row_untouched(started):
    """Every refusal ahead of the first write. `review_status` alone would also pass
    for a function that wrote and rolled back; the pair with `reviewed_at` is what
    says nothing happened at all."""
    wo_id, alarm_id = _alarm(decided=True, status="acked")

    with pytest.raises(ops.OpsError, match="what should the supervisor"):
        ops.review_alarm(alarm_id, approved=False, project_name="proj_a")

    row = _row(alarm_id, wo_id)
    assert row["review_status"] == "unreviewed"
    assert row["reviewed_at"] is None


def test_an_undecided_alarm_cannot_be_reviewed_and_the_error_names_its_status(started):
    wo_id, alarm_id = _alarm()

    with pytest.raises(ops.OpsError, match="is raised"):
        ops.review_alarm(alarm_id, approved=True, project_name="proj_a")

    assert _row(alarm_id, wo_id)["reviewed_at"] is None


def test_reviewing_an_escalated_alarm_closes_the_neo_question(started):
    """The close site nobody else owns: the pointer lives on the alarm, and a question
    nobody closes goes on asking the user for a ruling they have already given."""
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        q = neo.ask("proj_a", "wo-x", "should this turn be stopped?")
    finally:
        neo.close()
    _, alarm_id = _alarm(decided=True, status="escalated", verdict="escalate",
                         qid=q["id"])

    res = ops.review_alarm(alarm_id, approved=False, feedback=NOWHERE,
                           project_name="proj_a")

    assert res["neo_question_closed"] is True
    neo = NeoStore()
    try:
        closed = neo.get(q["id"])
    finally:
        neo.close()
    assert closed is not None
    assert closed["status"] == "answered"
    assert closed["answered_by"] == "os", "decided elsewhere, not by Neo"
    assert NOWHERE in closed["answer"]


# -- the CLI is the OS -------------------------------------------------------------


def test_both_spellings_of_the_listing_survive_the_new_subcommands(started, capsys):
    """`alarms` took a bare positional, so a subparser group is a real change to how it
    parses. Neither existing spelling may notice."""
    from jarvis import cli

    wo_id, alarm_id = _alarm()

    assert cli.main(["alarms"]) == 0
    every = capsys.readouterr().out
    assert "1 asking for you" in every and wo_id in every
    assert alarm_id not in every, "the bare listing is unchanged — §1 froze it"

    assert cli.main(["alarms", "proj_a"]) == 0
    assert wo_id in capsys.readouterr().out
    assert cli.main(["alarms", "--wo", wo_id]) == 0
    assert alarm_id in capsys.readouterr().out


def test_the_cli_shows_one_alarm_and_reviews_it(started, capsys):
    from jarvis import cli

    wo_id, alarm_id = _alarm(decided=True, status="acked")

    assert cli.main(["alarms", "show", alarm_id]) == 0
    out = capsys.readouterr().out
    assert FIRED in out and WHY in out and NOTE in out

    assert cli.main(["alarms", "review", alarm_id, "--reject",
                     "--feedback", NOWHERE]) == 0
    assert _row(alarm_id, wo_id)["review_feedback"] == NOWHERE

    assert cli.main(["alarms", "show", alarm_id]) == 0
    assert "you corrected this" in capsys.readouterr().out


def test_the_cli_refuses_a_correction_with_no_feedback(started, capsys):
    _, alarm_id = _alarm(decided=True, status="acked")
    from jarvis import cli

    assert cli.main(["alarms", "review", alarm_id, "--reject"]) == 1
    assert "what should the supervisor" in capsys.readouterr().err


# -- the link section 4 emits, followed --------------------------------------------


def test_the_link_the_work_order_timeline_renders_actually_resolves(started):
    """§4 renders `href="/alarms/<project>/<al-id>"` and says outright that proving it
    RESOLVES is this section's job. So: scrape the href off the work-order page and
    follow it, rather than re-asserting a URL both halves compose from the same parts.
    That is the assertion that would catch the two halves not meeting."""
    import re

    wo_id, alarm_id = _alarm(decided=True, status="acked")
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        store.add_event(wo_id, "cost_alarm",
                        {"kind": "long-turn", "seq": 1, "reason": FIRED,
                         "alarm_id": alarm_id})
    finally:
        store.close()
    client = _client()

    page = client.get(f"/wo/proj_a/{wo_id}").text
    hrefs = re.findall(r'href="(/alarms/[^"]+)"', page)
    assert hrefs, "the timeline rendered no link to the alarm"

    followed = client.get(hrefs[0])

    assert alarm_id in hrefs[0]
    assert followed.status_code == 200
    assert FIRED in followed.text
