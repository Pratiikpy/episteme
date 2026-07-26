"""Tests for web(2) extraction nodes: page.links, page.extract (offline html)."""
from contract import ArtifactRequest


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


def test_page_links(runtime):
    html = "<a href='/a'>A</a><a href='https://ex.com/p'>Ext</a><a href='/a'>dup</a>"
    env = runtime.execute(_req("page.links", html=html, base_url="https://site.test"))
    assert env.ok
    assert env.result["link_count"] == 3
    abs_urls = [l["absolute"] for l in env.result["links"]]
    assert "https://site.test/a" in abs_urls
    assert "ex.com" in env.result["unique_domains"]


def test_page_extract(runtime):
    html = "<html><body><h1>Title Here</h1><p class='body'>Para one</p><p class='body'>Para two</p></body></html>"
    env = runtime.execute(_req("page.extract", html=html,
                               selectors={"title": "h1", "paras": ".body"}))
    assert env.ok
    assert env.result["extracted"]["title"] == ["Title Here"]
    assert env.result["extracted"]["paras"] == ["Para one", "Para two"]
    assert "title" in env.result["matched_fields"]


def test_page_extract_bad_selectors(runtime):
    env = runtime.execute(_req("page.extract", html="<h1>x</h1>"))
    assert env.ok is False
