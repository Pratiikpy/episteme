"""Web pack (2): page.links, page.extract (bs4). Offline via {html}; {url} fetches."""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError
from nodes.web_nodes import _ssrf_guard, safe_get


def _html(ctx: NodeContext) -> tuple[str, str | None]:
    html = ctx.input.get("html")
    base = ctx.input.get("base_url") or ctx.input.get("url")
    if html is not None:
        return str(html), base
    url = str(ctx.input.get("url", "")).strip()
    if not url:
        raise NodeError(ErrorCode.INVALID_INPUT, "provide 'html' or 'url'")
    try:
        r = safe_get(url, timeout=20, headers={"User-Agent": "EpistemeBot/1.0"})
        return r.text, str(r.url)
    except NodeError:
        # A policy refusal is not a network failure. Rewrapping it as FETCH_FAILED told the caller
        # the site was unreachable when in fact we declined to go there, hiding a blocked redirect
        # behind what reads as a transient error worth retrying.
        raise
    except Exception as e:
        raise NodeError(ErrorCode.FETCH_FAILED, f"fetch failed: {e}")


class PageLinksNode(Node):
    name = "page.links"
    price_usdt = 0.005
    deterministic = True
    engine = "beautifulsoup4"

    def engine_available(self) -> bool:
        try:
            import bs4  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        from bs4 import BeautifulSoup
        html, base = _html(ctx)
        soup = BeautifulSoup(html, "html.parser")
        links, domains = [], set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            absolute = urljoin(base, href) if base else href
            dom = urlparse(absolute).netloc
            if dom:
                domains.add(dom)
            links.append({"href": href, "absolute": absolute,
                          "text": a.get_text(strip=True)[:200]})
        return {"link_count": len(links), "links": links[:2000],
                "unique_domains": sorted(domains)}

    def validate(self, result, ctx):
        return [ValidationCheck(name="counted_links", passed="link_count" in result)]


class PageExtractNode(Node):
    name = "page.extract"
    price_usdt = 0.02
    deterministic = True
    engine = "beautifulsoup4"

    def engine_available(self) -> bool:
        try:
            import bs4  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        from bs4 import BeautifulSoup
        html, _ = _html(ctx)
        selectors = ctx.input.get("selectors")
        if not isinstance(selectors, dict) or not selectors:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'selectors' {name: cssSelector}")
        soup = BeautifulSoup(html, "html.parser")
        extracted = {}
        for name, sel in selectors.items():
            try:
                nodes = soup.select(str(sel))
            except Exception as e:
                raise NodeError(ErrorCode.INVALID_INPUT, f"bad selector for '{name}': {e}")
            extracted[name] = [n.get_text(strip=True) for n in nodes][:200]
        return {"extracted": extracted,
                "fields": list(selectors.keys()),
                "matched_fields": [k for k, v in extracted.items() if v]}

    def validate(self, result, ctx):
        return [ValidationCheck(name="has_extraction", passed="extracted" in result)]
