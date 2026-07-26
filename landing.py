"""The page a human sees at the Episteme base URL.

The base URL returned a bare `404` — every service is a POST under /a2mcp/, so a plain browser GET
matched nothing. That is what a reviewer, a judge, or anyone following the marketplace listing opens
FIRST, and a 404 there reads as a dead service however well the paid endpoints work.

Rendered from the LIVE registry rather than a hand-written list: with 48 services, any static copy of
the catalogue would drift from what is actually served the first time a node is added or repriced, and
a page that advertises a price the paywall does not charge is worse than no page.
"""
from __future__ import annotations

import html

_CSS = """
:root{--bg:#0b0f1a;--panel:#121826;--line:#1f2840;--ink:#e9eef8;--dim:#8d9ab5;
--acc:#7fb0ff;--gold:#f6d084;--ok:#48e8b2;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.65 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:56px 22px 80px}
header{border-bottom:1px solid var(--line);padding-bottom:26px;margin-bottom:32px}
h1{margin:0 0 6px;font-size:31px;letter-spacing:-.02em}
h1 span{color:var(--acc)}
.tag{color:var(--dim);margin:0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.13em;color:var(--dim);
margin:38px 0 14px;font-weight:600}
pre{margin:0;padding:11px 13px;background:#070b14;border:1px solid var(--line);
border-radius:7px;overflow-x:auto;font:13px/1.5 var(--mono);color:#b9c6e2}
code{font-family:var(--mono)}
.steps{counter-reset:s;list-style:none;padding:0;margin:0}
.steps li{counter-increment:s;position:relative;padding:0 0 13px 34px;color:var(--dim);font-size:14.5px}
.steps li::before{content:counter(s);position:absolute;left:0;top:1px;width:21px;height:21px;
border-radius:50%;background:var(--panel);border:1px solid var(--line);color:var(--acc);
font:600 11px/21px var(--mono);text-align:center}
.steps b{color:var(--ink);font-weight:600}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:11px}
.meta div{border:1px solid var(--line);border-radius:9px;padding:12px 14px;background:var(--panel)}
.meta dt{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px}
.meta dd{margin:0;font:13px/1.45 var(--mono);color:var(--ink);word-break:break-all}
.grp{margin-bottom:20px}
.grp h3{font-size:14px;margin:0 0 9px;color:var(--ink);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13.5px;
border:1px solid var(--line);border-radius:9px;overflow:hidden}
th{text-align:left;background:var(--panel);color:var(--dim);font-size:11px;
text-transform:uppercase;letter-spacing:.09em;padding:8px 11px;font-weight:600}
td{padding:8px 11px;border-top:1px solid var(--line);vertical-align:top}
td.p{font:600 13px/1.4 var(--mono);color:var(--gold);white-space:nowrap;text-align:right}
td.e{font:13px/1.4 var(--mono);color:var(--acc);white-space:nowrap}
.nm{display:block;font:400 12px/1.4 ui-sans-serif,system-ui,sans-serif;color:var(--dim);white-space:normal;margin-top:2px}
.scroll{overflow-x:auto}
footer{margin-top:44px;padding-top:22px;border-top:1px solid var(--line);color:var(--dim);font-size:13px}
a{color:var(--acc)}
.pill{display:inline-block;font:600 10px/1 var(--mono);padding:4px 7px;border-radius:4px;
background:#16202f;color:var(--ok);border:1px solid var(--line);letter-spacing:.05em}
@media(max-width:560px){.wrap{padding:34px 16px 56px}h1{font-size:25px}}
"""

# Grouped by what a buyer is trying to do, not by module layout: a flat 48-row table tells a reader
# nothing about which service they want.
_GROUPS = [
    ("Documents & text", ("document.", "text.", "page.", "pdf.")),
    ("Data & tables", ("data.", "csv.")),
    ("Files, hashing & integrity", ("file.", "hash.", "artifact.", "receipt.", "image.")),
    ("Code & repositories", ("repo.", "mcp.", "openapi.")),
    ("Web & network checks", ("url.", "robots.", "sitemap.", "dns.", "email.", "http.")),
    ("Reasoning & workflows", ("sim.", "workflow.", "unit.", "time.", "geo.", "math.")),
]


def _group_for(endpoint: str) -> str:
    for title, prefixes in _GROUPS:
        if endpoint.startswith(prefixes):
            return title
    return "Other services"


def landing_html(listing: dict, base_url: str, signing_key: str | None,
                 internal_only: set[str]) -> str:
    """Render from the MARKETPLACE LISTING, not from the node registry.

    The registry carries no human copy at all — endpoint, price, engine and a determinism flag, nothing
    more. Rendering from it produced a table with an empty "what it does" column for all 48 services,
    which is worse than no page. The listing module is where the reviewed two-part descriptions live,
    and it is the exact text OKX shows on the marketplace, so a buyer reads the same words in both
    places and neither can drift from the other.
    """
    rows: dict[str, list[tuple[str, float, str, str]]] = {}
    for svc in listing.get("services", []):
        ep = (svc.get("endpoint") or "").rsplit("/", 1)[-1]
        if not ep or ep in internal_only:
            continue
        # The listing description is two parts: what it does, then a "Provide:" input line. The page
        # already documents inputs via /nodes/<endpoint>/schema, so the table shows only the first.
        what = (svc.get("serviceDescription") or "").split("\n")[0].strip()
        try:
            price = float(svc.get("fee") or 0)
        except (TypeError, ValueError):
            price = 0.0
        rows.setdefault(_group_for(ep), []).append(
            (ep, price, what, (svc.get("serviceName") or "").strip()))

    total = sum(len(v) for v in rows.values())
    blocks = []
    for title, _ in _GROUPS + [("Other services", ())]:
        items = sorted(rows.get(title, []))
        if not items:
            continue
        trs = "".join(
            f"<tr><td class='e'>{html.escape(ep)}<span class='nm'>{html.escape(name)}</span></td>"
            f"<td>{html.escape(what)}</td>"
            f"<td class='p'>${price:.3f}</td></tr>"
            for ep, price, what, name in items
        )
        blocks.append(
            f"<div class='grp'><h3>{html.escape(title)} "
            f"<span class='pill'>{len(items)}</span></h3><div class='scroll'><table>"
            f"<tr><th>Service</th><th>What it does</th><th style='text-align:right'>Price</th></tr>"
            f"{trs}</table></div></div>"
        )

    key = signing_key or "not configured"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Episteme — {total} verifiable services, each with a signed receipt</title>
<meta name="description" content="An OKX.AI agent service: {total} deterministic pay-per-call tools over x402 on X Layer. Every result ships a signed receipt naming the checks that ran.">
<style>{_CSS}</style></head><body><div class="wrap">
<header>
<h1>Episteme<span>.</span></h1>
<p class="tag">{total} deterministic services. Every result arrives with a signed receipt naming the
checks that ran.</p>
</header>

<p style="color:var(--dim);margin:0 0 8px">ἐπιστήμη is knowledge that is <i>justified</i>, as against
opinion that happens to be right. Every call here returns the result <b style="color:var(--ink)">and the
evidence that it is correct</b>: the checks that were run, whether each passed, and an Ed25519 signature
over the whole artifact. You can verify a receipt offline, without asking us anything.</p>

<h2>Services</h2>
{''.join(blocks)}

<h2>Calling a service</h2>
<pre>curl -X POST {html.escape(base_url)}/a2mcp/text.stats \\
  -H 'Content-Type: application/json' \\
  -d '{{"input": {{"text": "hello world"}}}}'</pre>
<p style="color:var(--dim);font-size:14px;margin:11px 0 0">Send an <b>empty body</b> to any endpoint and
it returns that endpoint's input contract with a working example instead of an error, so you can
discover the shape before paying. Machine-readable schemas:
<a href="/nodes">/nodes</a> and <code>/nodes/&lt;endpoint&gt;/schema</code>.</p>

<h2>How paying works (x402)</h2>
<ol class="steps">
<li>Call the endpoint with no payment. It answers <b>402</b> with a <code>PAYMENT-REQUIRED</code> header
carrying the challenge.</li>
<li>Sign the challenge with your wallet — an <b>EIP-3009</b> authorization for USD₮0 on X Layer.</li>
<li>Repeat the call with the <code>PAYMENT-SIGNATURE</code> header. Payment settles on chain and the
artifact comes back in the same response.</li>
</ol>
<p style="color:var(--dim);font-size:14px;margin:2px 0 0">Stablecoin transfers on X Layer are gas-free,
so a call costs exactly its listed price. Each payment authorization is valid for exactly one call.</p>

<h2>Verifying a receipt</h2>
<div class="meta">
<div><dt>Signing public key</dt><dd>{html.escape(key)}</dd></div>
<div><dt>Scheme</dt><dd>Ed25519 over the canonical artifact digest</dd></div>
<div><dt>Network</dt><dd>X Layer · eip155:196</dd></div>
<div><dt>Asset</dt><dd>USD₮0 · 0x779ded0c9e1022225f8e0630b35a9b54be713736</dd></div>
<div><dt>Published key</dt><dd><a href="/.well-known/episteme-signing-key">/.well-known/episteme-signing-key</a></dd></div>
<div><dt>Health</dt><dd><a href="/healthz">/healthz</a></dd></div>
</div>
<p style="color:var(--dim);font-size:14px;margin:13px 0 0">Every artifact carries a validation level:
<code>L1_PRODUCED</code> through <code>L4_INDEPENDENTLY_VERIFIED</code>, stating how far the result was
checked. Verify any receipt yourself, or POST it to <code>/a2mcp/receipt.verify</code>.</p>

<footer>Episteme is an Agent Service Provider on OKX.AI. Deterministic services are reproducible from
the same input; AI-backed services are marked in their schema and capped at a lower validation level.</footer>
</div></body></html>"""
