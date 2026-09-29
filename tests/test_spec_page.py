"""The stdlib markdown renderer behind the spec page — escape-first, subset only.

§2 of docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md. The security
property under test is the ORDER: the whole document is escaped before a single rule
runs, so the only markup in the output is markup this module emitted.
"""

from __future__ import annotations

from jarvis import sections
from jarvis.ui import markdown


def test_escape_first_leaves_no_live_markup():
    """Test 2 of the spec's Tests section: planner prose is inert."""
    doc = (
        "## Threats\n\n"
        "<script>alert(1)</script>\n\n"
        "<img src=x onerror=alert(1)>\n\n"
        "[x](javascript:alert(1))\n"
    )
    out = markdown.render(doc)

    # Spec test 2, with its one assertion stated the only way escape-first can satisfy
    # it: `onerror=` survives as TEXT (that is what escaping does), so what must not
    # exist is a tag carrying it. No `<script`, no `<img`, no `javascript:` in an href.
    assert "<script" not in out
    assert "<img" not in out
    assert "onerror" not in out.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert 'href="javascript' not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_autolinks_only_http_and_https():
    out = markdown.render("See https://example.com/a?b=1 and ftp://example.com/x")

    assert '<a href="https://example.com/a?b=1"' in out
    assert "ftp://example.com/x" in out
    assert '<a href="ftp' not in out


def test_an_autolink_stops_at_an_escaped_quote_and_keeps_a_real_ampersand():
    """The pattern runs over ESCAPED text, so `'` is `&#x27;` by then: without the
    distinction, `https://x.com's` puts the entity inside the href."""
    out = markdown.render("see https://x.com/a's page and https://x.com/q?a=1&b=2 too")

    assert '<a href="https://x.com/a">' in out
    assert "&#x27;" not in out.split("</a>")[0]
    assert '<a href="https://x.com/q?a=1&amp;b=2">' in out


def test_fenced_code_is_verbatim_with_no_inline_pass():
    out = markdown.render("```sh\nrm *.md **bold** `x`\n```\n")

    assert "<pre><code" in out
    assert "rm *.md **bold** `x`" in out
    assert "<strong>" not in out


def test_an_unclosed_fence_runs_to_the_end_of_the_document():
    out = markdown.render("```\nstill code\nand this too\n")

    assert "still code" in out and "and this too" in out
    assert "<p>still code" not in out


def test_headings_are_clamped_to_h2_h6_and_carry_their_slug():
    out = markdown.render("# Title\n\n###### Deep\n")

    assert '<h2 id="title">Title</h2>' in out
    assert '<h6 id="deep">Deep</h6>' in out
    assert "<h1" not in out


def test_inline_code_wins_over_emphasis():
    out = markdown.render("`**not bold**` and **bold** and *em*")

    assert "<code>**not bold**</code>" in out
    assert "<strong>bold</strong>" in out
    assert "<em>em</em>" in out


def test_lists_render_one_level_and_nested_items_flatten():
    out = markdown.render("- one\n  - nested\n\n1. first\n")

    assert out.count("<ul>") == 1
    assert "<li>one</li>" in out and "<li>nested</li>" in out
    assert "<ol>" in out and "<li>first</li>" in out


def test_slug_folds_accents_and_section_marks():
    assert markdown.slug("§5. Résumé of the Plan ##") == "5-resume-of-the-plan"
    assert markdown.slug("###") == "section"
    assert markdown.slug("Data model") == "data-model"


def test_slug_collisions_first_wins_bare():
    """Test 5: GitHub's rule, and `render` and `anchor_for` must agree on it."""
    doc = "## Data model\n\nfirst\n\n## Data model\n\nsecond\n"

    assert [s for _t, s in markdown.anchors(doc)] == ["data-model", "data-model-1"]
    out = markdown.render(doc)
    assert 'id="data-model"' in out and 'id="data-model-1"' in out
    assert markdown.anchor_for(doc, "data model") == "data-model"


def test_a_hash_comment_inside_a_fence_does_not_shift_every_later_id():
    """A `#` line in fenced bash is a HEADING_RE match `render` never emits.

    `anchors()` must keep counting it — `sections.find_heading` indexes into the same
    list — so `render` looks its slug up BY LINE instead of consuming an iterator.
    Without that, one shell comment sends every `#anchor` deep link below it to the
    wrong section, and our own specs are full of fenced bash.
    """
    doc = "## Intro\n\n```bash\n# install things\n```\n\n## Data model\n\ntext\n"
    out = markdown.render(doc)

    assert '<h2 id="data-model">Data model</h2>' in out
    assert markdown.anchor_for(doc, "Data model") == "data-model"
    assert 'id="install-things"' not in out
    assert "# install things" in out and "<pre><code>" in out


def test_anchor_for_uses_the_one_matcher():
    doc = "## 1. Shape\n\na\n\n## 2. Data model\n\nb\n"

    assert markdown.anchor_for(doc, "2") == "2-data-model"
    assert markdown.anchor_for(doc, "data model") == "2-data-model"
    assert markdown.anchor_for(doc, "no such thing") is None
    # And it is literally `sections.find_heading`, never a second rule.
    assert sections.find_heading(doc, "2") == 1


def test_everything_outside_the_subset_degrades_to_a_paragraph():
    out = markdown.render("| a | b |\n|---|---|\n\n> quoted\n")

    assert "<table" not in out and "<blockquote" not in out
    assert "quoted" in out
