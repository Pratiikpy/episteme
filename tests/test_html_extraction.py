"""A chrome hint on the root element must not delete the document.

Every Wikipedia page came back from the live `url.to_markdown` service as `char_count: 0`,
`markdown: ""`, `ok: true`, and no warning of any kind — while example.com and python.org were fine.
The cause was in the response all along: `boilerplate_by_tag: {"html": 1}`. Wikipedia carries its
theme state on the root element, `<html class="… -main-menu-disabled … -toc-pinned-…">`, and the
chrome regex matched `menu` and `toc` there, so the whole page was skipped as navigation.

Measured before and after on the same fetched bytes: 0 characters to 23,985, with example.com and
python.org unchanged to the character.
"""
from __future__ import annotations

from nodes.web_nodes import _html_to_markdown

WIKIPEDIA_ROOT_CLASS = (
    "client-nojs vector-feature-language-in-header-enabled "
    "vector-feature-language-in-main-menu-disabled vector-feature-toc-pinned-clientpref-1 "
    "vector-feature-main-menu-pinned-disabled skin-theme-clientpref-day"
)


def test_theme_classes_on_the_root_element_do_not_delete_the_page():
    html = (f'<html class="{WIKIPEDIA_ROOT_CLASS}"><head><title>An Article</title></head>'
            "<body><main><p>Retrieval-augmented generation grounds a model in retrieved "
            "documents.</p><p>The retrieved passages are placed in the prompt.</p></main>"
            "</body></html>")
    title, md, meta = _html_to_markdown(html)
    assert title == "An Article"
    assert "Retrieval-augmented generation grounds a model" in md
    assert "retrieved passages" in md
    assert "html" not in meta["boilerplate_by_tag"], "the root element was dropped as chrome"


def test_the_same_hint_on_a_real_nav_still_removes_it():
    """The fix must not turn the chrome filter off — only stop it eating the document."""
    html = ('<html><body><main>'
            '<nav class="site-nav menu"><a href="/a">Home</a><a href="/b">About</a></nav>'
            "<p>The actual article text lives here and should survive.</p>"
            '<div class="newsletter-signup">Subscribe to our newsletter</div>'
            "</main></body></html>")
    _, md, meta = _html_to_markdown(html)
    assert "actual article text" in md
    assert "Subscribe to our newsletter" not in md
    assert "Home" not in md
    assert sum(meta["boilerplate_by_tag"].values()) >= 2


def test_body_carrying_a_state_class_is_also_safe():
    """Not a Wikipedia quirk — `menu-open` and `has-sidebar` on <body> are ordinary theme state."""
    html = ('<html><body class="menu-open has-sidebar">'
            "<p>Content that must not vanish because a menu happens to be open.</p>"
            "</body></html>")
    _, md, _ = _html_to_markdown(html)
    assert "must not vanish" in md


def test_an_empty_extraction_explains_itself():
    """`char_count: 0` with `ok: true` and no warning is indistinguishable from a page that
    genuinely has nothing on it. The buyer paid either way and is owed the difference."""
    shell = "<html><body>" + ('<div class="advert"></div>' * 60) + "</body></html>"
    _, md, meta = _html_to_markdown(shell)
    assert md == ""
    assert meta.get("extraction_note"), "an empty result was returned with no explanation"
    assert "nothing to convert" in meta["extraction_note"]


def test_the_salvage_pass_recovers_text_and_says_so():
    """If the filters ever eat a document again, the buyer gets the text and is told how."""
    html = ('<html><body><div class="navigation">'
            + "Real words that only exist inside a block the filter would normally drop. " * 3
            + "</div></body></html>")
    _, md, meta = _html_to_markdown(html)
    assert "Real words that only exist" in md
    assert "boilerplate removal off" in meta.get("extraction_note", "")
