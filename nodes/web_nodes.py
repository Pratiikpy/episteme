"""Web pack nodes: url.inspect, url.to_markdown.

Offline-testable: pass {"html": "..."} to extract without network; pass
{"url": "https://..."} to fetch (with SSRF guards). Fetching makes the node
non-deterministic, so it caps at L2; HTML input is deterministic.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

_BLOCK_SCHEMES = {"file", "ftp", "gopher", "data"}


def _ssrf_guard(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise NodeError(ErrorCode.POLICY_BLOCKED, f"scheme '{p.scheme}' not allowed")
    host = p.hostname or ""
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        raise NodeError(ErrorCode.POLICY_BLOCKED, "internal host blocked")
    try:
        for res in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(res[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise NodeError(ErrorCode.POLICY_BLOCKED, f"resolves to private ip {ip}")
    except socket.gaierror:
        raise NodeError(ErrorCode.FETCH_FAILED, f"cannot resolve host '{host}'")


MAX_REDIRECTS = 5


def safe_get(url: str, *, timeout: float = 20.0, headers: dict | None = None):
    """GET a URL, re-running the SSRF guard on **every** hop.

    Guarding only the URL the caller handed us and then letting the HTTP client follow redirects is
    not a guard at all: a perfectly public address that answers 302 with
    `Location: http://169.254.169.254/latest/meta-data/iam/security-credentials/` walks straight past
    it and we fetch the host's cloud credentials on the attacker's behalf. The first hop is the only
    one an attacker has to make look innocent.

    Verified against the live service before this existed: a redirect through a public redirector
    reached the metadata address and failed only because that platform happened to refuse the
    connection. That is the hosting environment's configuration, not our defence, and it is not
    something to rely on — the same code runs on hosts where that address answers.

    So redirects are followed by hand, one at a time, with the guard re-run on each new location.
    """
    import httpx                                                     # noqa: PLC0415

    seen = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _ssrf_guard(current)
        seen.append(current)
        with httpx.Client(follow_redirects=False, timeout=timeout) as c:
            r = c.get(current, headers=headers or {})
        if r.status_code not in (301, 302, 303, 307, 308):
            return r
        location = r.headers.get("location")
        if not location:
            return r
        current = str(httpx.URL(current).join(location))
        if current in seen:
            raise NodeError(ErrorCode.FETCH_FAILED, "redirect loop")
    raise NodeError(ErrorCode.FETCH_FAILED,
                    f"more than {MAX_REDIRECTS} redirects starting at {url}")


# Elements that never carry article content. Dropping them is the difference between a page's text
# and a page's navigation: previously a Wikipedia article came back with the entire sidebar, edit
# links and footer inlined, so the buyer paid for markdown and had to clean it themselves.
_DROP_TAGS = frozenset({
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe",
    "button", "select", "template", "dialog", "menu", "figcaption", "picture", "video", "audio",
})
# Class/id substrings that mark chrome even when the tag itself is a neutral <div>. Matched on word
# boundaries so "sidebar" does not also kill "considerable".
_CHROME_HINT = re.compile(
    r"(?i)(?:^|[\s_-])(?:nav|navbar|navigation|menu|sidebar|side-?bar|masthead|breadcrumb|"
    r"cookie|consent|gdpr|banner|promo|advert|advertisement|ads?|sponsor|social|share|"
    r"newsletter|subscribe|signup|related|recommended|comment|disqus|pagination|pager|"
    r"skip-?link|screen-?reader|sr-only|visually-?hidden|toc|table-of-contents|"
    r"site-?header|site-?footer|global-?header|global-?footer)(?:$|[\s_-])"
)
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _Extract(HTMLParser):
    """HTML → Markdown with boilerplate removal and structure preservation.

    The previous version kept only bare text: every link lost its href, every image vanished, code
    blocks were whitespace-collapsed into prose, tables became a run of unaligned words, and all
    navigation chrome was included. For an agent that is the difference between a readable document
    and an unusable one — and the hrefs in particular are the page's citation graph, which is
    precisely what a research agent came for."""

    def __init__(self, content_root: str | None = None):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.chunks: list[str] = []
        self.links: list[dict] = []
        self.images: list[dict] = []
        self.dropped_tags: dict[str, int] = {}

        self._skip_depth = 0          # inside a dropped subtree
        self._skip_tag: str | None = None
        self._in_title = False
        self._pre_depth = 0           # inside <pre>: preserve whitespace verbatim
        self._href: str | None = None
        self._link_text: list[str] = []
        self._list_stack: list[str] = []   # 'ul' | 'ol', for nesting and numbering
        self._ol_counter: list[int] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._table_rows: list[tuple[list[str], bool]] | None = None
        self._row_is_header = False
        self._quote_depth = 0

        # When a page marks its content region, restrict extraction to it. This single heuristic
        # removes most chrome on well-built sites before any class-name guessing is needed.
        self._content_root = content_root
        self._root_depth = 0
        self._root_tag_depth = 0

    # -- helpers ---------------------------------------------------------------------------------
    @property
    def _emitting(self) -> bool:
        if self._skip_depth:
            return False
        return self._content_root is None or self._root_depth > 0

    def _out(self, s: str) -> None:
        if self._cell is not None:
            self._cell.append(s)
        elif self._link_text is not None and self._href is not None:
            self._link_text.append(s)
        else:
            self.chunks.append(s)

    def _newline(self, n: int = 1) -> None:
        if self._cell is not None:
            return  # a line break inside a table cell would destroy the row
        self.chunks.append("\n" * n)

    # -- parser callbacks ------------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        if tag == "title":
            self._in_title = True
            return

        # Track the content root so everything outside it is ignored.
        if self._content_root:
            if tag == self._content_root:
                self._root_depth += 1
            elif self._root_depth > 0:
                self._root_tag_depth += 1

        ident = f"{a.get('class', '')} {a.get('id', '')} {a.get('role', '')}"
        if tag in _DROP_TAGS or (ident.strip() and _CHROME_HINT.search(ident)) or a.get("hidden") is not None \
                or a.get("aria-hidden") == "true":
            self._skip_depth = 1
            self._skip_tag = tag
            self.dropped_tags[tag] = self.dropped_tags.get(tag, 0) + 1
            return
        if not self._emitting:
            return

        if tag in _HEADINGS:
            self._newline(2)
            self._out("#" * _HEADINGS[tag] + " ")
        elif tag == "p":
            self._newline(2)
        elif tag == "br":
            self._newline()
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self._ol_counter.append(0)
            self._newline()
        elif tag == "li":
            depth = max(0, len(self._list_stack) - 1)
            self._newline()
            if self._list_stack and self._list_stack[-1] == "ol":
                self._ol_counter[-1] += 1
                self._out("  " * depth + f"{self._ol_counter[-1]}. ")
            else:
                self._out("  " * depth + "- ")
        elif tag == "a":
            href = a.get("href")
            if href and not href.startswith(("javascript:", "#")):
                self._href = href
                self._link_text = []
        elif tag == "img":
            src, alt = a.get("src"), (a.get("alt") or "").strip()
            if src:
                self.images.append({"src": src, "alt": alt})
                self._out(f"![{alt}]({src})")
        elif tag in ("strong", "b"):
            self._out("**")
        elif tag in ("em", "i"):
            self._out("*")
        elif tag == "code" and not self._pre_depth:
            self._out("`")
        elif tag == "pre":
            self._pre_depth += 1
            self._newline(2)
            self._out("```\n")
        elif tag == "blockquote":
            self._quote_depth += 1
            self._newline(2)
            self._out("> ")
        elif tag == "hr":
            self._newline(2)
            self._out("---")
            self._newline(2)
        elif tag == "table":
            self._table_rows = []
        elif tag == "tr" and self._table_rows is not None:
            self._row = []
            self._row_is_header = False
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            if tag == "th":
                self._row_is_header = True

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag == "title":
            self._in_title = False
            return
        if self._content_root:
            if tag == self._content_root and self._root_depth > 0:
                self._root_depth -= 1
            elif self._root_depth > 0 and self._root_tag_depth > 0:
                self._root_tag_depth -= 1
        if not self._emitting:
            return

        if tag == "a" and self._href is not None:
            text = "".join(self._link_text).strip()
            href = self._href
            self._href, self._link_text = None, []
            if text:
                self.links.append({"text": text[:200], "href": href})
                # Preserve the href. Markdown without link targets is just plain text, and the
                # targets are the one thing a caller cannot reconstruct from the page body.
                self._out(f"[{text}]({href})")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
                self._ol_counter.pop()
            self._newline()
        elif tag in ("strong", "b"):
            self._out("**")
        elif tag in ("em", "i"):
            self._out("*")
        elif tag == "code" and not self._pre_depth:
            self._out("`")
        elif tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._out("\n```")
            self._newline(2)
        elif tag == "blockquote":
            self._quote_depth = max(0, self._quote_depth - 1)
            self._newline(2)
        elif tag in ("td", "th") and self._cell is not None:
            cell = re.sub(r"\s+", " ", "".join(self._cell)).strip().replace("|", "\\|")
            self._cell = None
            if self._row is not None:
                self._row.append(cell)
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self._table_rows.append((self._row, self._row_is_header))
            self._row = None
        elif tag == "table" and self._table_rows is not None:
            self._emit_table(self._table_rows)
            self._table_rows = None
        elif tag in ("div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline()

    def _emit_table(self, rows: list[tuple[list[str], bool]]) -> None:
        """Render as a GitHub pipe table. Flat text loses the row/column association entirely, which
        is the only information a table carries."""
        if not rows:
            return
        width = max(len(r) for r, _ in rows)
        self._newline(2)
        header, is_header = rows[0]
        header = header + [""] * (width - len(header))
        self.chunks.append("| " + " | ".join(header) + " |\n")
        self.chunks.append("|" + "|".join([" --- "] * width) + "|\n")
        for cells, _ in rows[1:] if is_header else rows:
            cells = cells + [""] * (width - len(cells))
            self.chunks.append("| " + " | ".join(cells) + " |\n")
        self._newline()

    def handle_data(self, data):
        # <title> lives in <head>, which is outside the content root — so it must be captured before
        # the emitting gate, or restricting extraction to <main> silently discards the page title.
        if self._in_title:
            if not self._skip_depth:
                self.title += data.strip() + " "
            return
        if self._skip_depth or not self._emitting:
            return
        if self._pre_depth:
            self._out(data)   # verbatim: indentation IS the content of a code block
            return
        text = re.sub(r"[ \t\r\n]+", " ", data)
        if text.strip():
            self._out(text)


def _html_to_markdown(html: str) -> tuple[str, str, dict]:
    # Prefer the page's declared content region when it has one — the single most reliable signal
    # for where the article is, and cheaper and safer than scoring every div.
    root = None
    for candidate in ("article", "main"):
        if re.search(rf"<{candidate}[\s>]", html, re.I):
            root = candidate
            break
    p = _Extract(content_root=root)
    p.feed(html)
    md = "".join(p.chunks)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    meta = {
        "content_root": root,
        "links": p.links[:200],
        "link_count": len(p.links),
        "images": p.images[:100],
        "image_count": len(p.images),
        "boilerplate_blocks_removed": sum(p.dropped_tags.values()),
        "boilerplate_by_tag": p.dropped_tags,
    }
    return p.title.strip(), md, meta


class UrlInspectNode(Node):
    name = "url.inspect"
    price_usdt = 0.001
    deterministic = False
    engine = "httpx"

    def engine_available(self) -> bool:
        try:
            import httpx  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import httpx
        url = str(ctx.input.get("url", "")).strip()
        if not url:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'url'")
        try:
            r = safe_get(url, timeout=15, headers={"User-Agent": "EpistemeBot/1.0"})
        except NodeError:
            # A policy refusal is not a network failure. Rewrapping it as FETCH_FAILED told the
            # caller the site was unreachable when in fact we declined to go there, which hides a
            # blocked redirect behind what reads as a transient error worth retrying.
            raise
        except Exception as e:
            raise NodeError(ErrorCode.FETCH_FAILED, f"fetch failed: {e}")
        return {
            "final_url": str(r.url),
            "status": r.status_code,
            "content_type": r.headers.get("content-type"),
            "content_length": int(r.headers.get("content-length") or len(r.content)),
            "redirects": len(r.history),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="got_status", passed=isinstance(result.get("status"), int))]


class UrlToMarkdownNode(Node):
    name = "url.to_markdown"
    price_usdt = 0.01
    deterministic = False  # network fetch varies; HTML input is deterministic
    engine = "episteme-extract"

    def run(self, ctx: NodeContext) -> dict:
        html = ctx.input.get("html")
        source = None
        if html is None:
            url = str(ctx.input.get("url", "")).strip()
            if not url:
                raise NodeError(ErrorCode.INVALID_INPUT, "provide 'html' or 'url'")
            try:
                r = safe_get(url, timeout=20, headers={"User-Agent": "EpistemeBot/1.0"})
                html = r.text
                source = str(r.url)
            except NodeError:
                # A policy refusal is not a network failure — see the note above.
                raise
            except Exception as e:
                raise NodeError(ErrorCode.FETCH_FAILED, f"fetch failed: {e}")
        title, md, meta = _html_to_markdown(str(html))
        return {
            "title": title,
            "markdown": md,
            "char_count": len(md),
            "source_url": source,
            "word_count": len(md.split()),
            **meta,
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        md = result.get("markdown", "")
        # Chrome that survived extraction is the defect this node kept shipping: a page returned with
        # its whole navigation inlined is not "converted", it is dumped. Assert the markers are gone.
        leaked = [m for m in ("<nav", "<script", "<style", "<footer", "<aside") if m in md.lower()]
        return [
            ValidationCheck(name="markdown_nonempty", passed=len(md) > 0),
            ValidationCheck(name="no_html_tags_leaked", passed=not leaked,
                            detail=f"found {leaked}" if leaked else "no raw markup in the output"),
            # A page whose links all lost their href converted the text but destroyed the citation
            # graph — the thing a research agent is actually reading the page for.
            ValidationCheck(
                name="links_carry_targets",
                passed=(result.get("link_count", 0) == 0 or "](" in md),
                detail=f"{result.get('link_count', 0)} link(s) preserved with their targets",
            ),
            ValidationCheck(
                name="code_blocks_balanced",
                passed=md.count("```") % 2 == 0,
                detail="every fenced block is closed, so the markdown parses",
            ),
        ]
