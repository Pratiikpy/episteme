"""Exercise every priced service with realistic input and print the actual outcome.

Not a test — a judgement harness. The question it answers is the one a buyer asks: given a real input,
is the thing that comes back actually useful? Green routes and passing shape assertions have repeatedly
hidden services that return technically-valid nonsense, so this prints the real output for reading.

    python scripts/exercise_all.py [name-substring ...]
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings
from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

CSV = """order_id,customer,region,amount,ordered_at,qty
1001,Acme Corp,EMEA,1284500.50,2024-02-01,3
1002,Globex,APAC,4300.00,2024-11-15,1
1003,Acme Corp,EMEA,0.000015,2024-03-20,12
1004,Initech,AMER,88000,2024-07-04,7
1005,Globex,APAC,4300.00,2024-11-15,1
1006,Umbrella,EMEA,,2024-05-30,2
"""

ROWS = [
    {"order_id": 1001, "customer": "Acme Corp", "region": "EMEA", "amount": 1284500.50, "qty": 3},
    {"order_id": 1002, "customer": "Globex", "region": "APAC", "amount": 4300.00, "qty": 1},
    {"order_id": 1003, "customer": "Acme Corp", "region": "EMEA", "amount": 0.000015, "qty": 12},
    {"order_id": 1004, "customer": "Initech", "region": "AMER", "amount": 88000, "qty": 7},
    {"order_id": 1005, "customer": "Globex", "region": "APAC", "amount": 4300.00, "qty": 1},
]

PROSE = """Retrieval-augmented generation grounds a model in retrieved documents. The retriever
selects passages and the generator conditions on them.

Chunking decides what the retriever can find. Too large and the embedding blurs across topics; too
small and the surrounding context is lost entirely.

Overlap exists so that a passage split across a boundary is still recoverable from at least one
chunk. Contact jane.doe@acme.com or call 415-555-1234 for the dataset."""

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/pets/{petId}": {
            "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "get": {"operationId": "getPet", "summary": "Fetch one pet",
                    "responses": {"200": {"description": "ok"}}},
        },
        "/pets": {
            "post": {"operationId": "createPet", "summary": "Create a pet",
                     "requestBody": {"required": True, "content": {"application/json": {
                         "schema": {"$ref": "#/components/schemas/Pet"}}}},
                     "responses": {"201": {"description": "created"}}},
            "get": {"operationId": "listPets", "summary": "List pets",
                    "responses": {"200": {"description": "ok"}}},
        },
    },
    "components": {"schemas": {"Pet": {"type": "object", "required": ["name"],
                                       "properties": {"name": {"type": "string"}}}}},
}

HTML = """<html><head><title>Consensus Mechanisms</title><style>.a{}</style></head><body>
<nav class="navbar"><a href="/docs">Docs</a><a href="/blog">Blog</a></nav>
<div class="cookie-consent">We use cookies.</div>
<main><h1>Consensus Mechanisms</h1>
<p>A <strong>consensus mechanism</strong> lets nodes agree. See the
<a href="https://bitcoin.org/bitcoin.pdf">original paper</a> and the
<a href="https://ethereum.org/en/roadmap/">roadmap</a>.</p>
<table><tr><th>Type</th><th>Energy</th></tr><tr><td>PoW</td><td>High</td></tr>
<tr><td>PoS</td><td>Low</td></tr></table>
<pre><code>def verify(block):
    return block.hash.startswith("0000")</code></pre>
</main><footer>(c) 2026 <a href="/privacy">Privacy</a></footer></body></html>"""

PY_SRC = """import os, sys
# Split so no scannable key literal exists in the file — see tests/test_service_quality.py
API_KEY = "sk-" + "proj-abc123XYZ_deadbeef-cafe0987654321zzTT"

def process(items):
    out = []
    for i in items:
        if i > 0:
            out.append(i * 2)
    return out

class Handler:
    def __init__(self, cfg): self.cfg = cfg
    def run(self): return process(self.cfg.get("items", []))
"""


def _png(w: int = 240, h: int = 160) -> str:
    from PIL import Image
    im = Image.new("RGB", (w, h), (40, 90, 160))
    for x in range(w):
        for y in range(0, h, 20):
            im.putpixel((x, y), (240, 200, 90))
    b = io.BytesIO()
    im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def _pdf(pages: int = 5) -> str:
    """A real, structurally valid PDF. A hand-rolled byte blob has no xref table, so pypdf rejects it
    with "startxref not found" and the failure says nothing about the node under test."""
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    b = io.BytesIO()
    w.write(b)
    return base64.b64encode(b.getvalue()).decode()


# endpoint -> input. Realistic values with the awkward cases a buyer's real data contains: a huge
# number, a sub-cent number, a duplicate row, a missing cell, a leaked key, nav chrome, a $ref.
CASES: dict[str, dict] = {
    "hash.compute": {"text": "attribution test"},
    "text.stats": {"text": PROSE},
    "text.diff": {"a": "the quick brown fox\njumps over\nthe lazy dog",
                  "b": "the quick red fox\njumps over\nthe lazy cat"},
    "unit.convert": {"value": 26.2, "from": "mi", "to": "km"},
    "file.inspect": {"content_b64": _png(), "name": "chart.png"},
    "object.diff": {"a": {"x": 1, "y": 2, "nested": {"k": "v"}},
                    "b": {"x": 1, "y": 3, "z": 4, "nested": {"k": "w"}}},
    "csv.profile": {"csv": CSV},
    "csv.to_table": {"csv": CSV, "format": "github"},
    "data.stats": {"rows": ROWS, "column": "amount"},
    "data.clean": {"rows": [{"n": "  Bob   Smith ", "e": " A@B.COM "}], "ops": ["trim", "collapse_ws", "lowercase"]},
    "data.dedupe": {"rows": ROWS, "keys": ["customer", "region", "amount"]},
    "data.diff": {"a": ROWS, "b": ROWS[:3] + [{"order_id": 1004, "customer": "Initech",
                                               "region": "AMER", "amount": 99000, "qty": 7}]},
    "data.join": {"left": [{"id": 1, "a": "x"}, {"id": 2, "a": "y"}],
                  "right": [{"id": 1, "b": "p"}], "on": "id", "how": "left"},
    "data.pivot": {"rows": ROWS, "group_by": ["region"], "agg": "sum", "value": "amount"},
    "data.query_sql": {"csv": CSV, "sql": "SELECT region, SUM(amount) AS total FROM t GROUP BY region ORDER BY total DESC"},
    "data.validate": {"rows": ROWS, "schema": {"type": "object", "required": ["order_id", "amount"],
                                               "properties": {"order_id": {"type": "integer"},
                                                              "amount": {"type": "number"}}}},
    "data.convert": {"csv": CSV, "to": "json"},
    "data.transform_json": {"data": {"user": {"name": "a", "tags": [], "meta": {}}, "items": [1, 2]},
                            "op": "flatten"},
    "document.chunk": {"text": PROSE, "chunk_size": 220, "overlap": 60},
    "document.compare": {"a": PROSE, "b": PROSE.replace("Overlap exists", "Overlap is present")},
    "document.redact_pii": {"text": PROSE},
    "document.to_markdown": {"content_b64": base64.b64encode(PROSE.encode()).decode(), "name": "notes.txt"},
    "email.validate": {"email": "Jane.Doe+news@Gmail.com", "check_mx": False},
    "chart.spec": {"rows": [{"date": "2024-02-01", "revenue": 1200.5},
                            {"date": "2024-11-01", "revenue": 3400.25}],
                   "x": "date", "y": "revenue", "mark": "line"},
    "schema.generate": {"example": ROWS[0]},
    "openapi.inspect": {"spec": OPENAPI},
    "openapi.lint": {"spec": OPENAPI},
    "openapi.diff": {"old": OPENAPI, "new": json.loads(json.dumps(OPENAPI).replace('"listPets"', '"listAllPets"'))},
    "api.to_mcp": {"spec": OPENAPI},
    "mcp.validate": {"tools": [{"name": "search_web", "description": "Search the web and return results.",
                                "inputSchema": {"type": "object",
                                                "properties": {"q": {"type": "string", "description": "query"}},
                                                "required": ["q"]}}]},
    "repo.scan_secrets": {"files": {"app.py": PY_SRC, "README.md": "# Docs\nNo secrets here."}},
    "repo.lint": {"files": {"app.py": PY_SRC}},
    "repo.map": {"files": {"app.py": PY_SRC, "util/helpers.py": "def h(): pass\n",
                           "tests/test_app.py": "def test_h(): assert True\n"}},
    "url.to_markdown": {"html": HTML},
    "page.extract": {"html": HTML, "selectors": {"heading": "h1", "code": "pre code", "cells": "td"}},
    "page.links": {"html": HTML, "base_url": "https://example.com/article"},
    "site.map": {"sitemap": "<urlset><url><loc>https://e.com/a</loc></url>"
                            "<url><loc>https://e.com/b</loc></url></urlset>"},
    "robots.check": {"robots": "User-agent: *\nDisallow: /admin\nAllow: /\nSitemap: https://e.com/sitemap.xml",
                     "url": "https://e.com/admin/secret", "user_agent": "EpistemeBot"},
    "image.inspect": {"content_b64": _png()},
    "image.transform": {"content_b64": _png(), "op": "resize", "width": 80},
    "pdf.manipulate": {"content_b64": _pdf(), "op": "extract_pages", "pages": "1-2"},
    "artifact.verify": {"text": "attribution test",
                        "expected_sha256": "0fd2e7e0e4a4a7a35b2b0f2e7b3a5f1e"},  # deliberately wrong
    "url.inspect": {"url": "https://example.com"},
    "text.summarize": {"text": PROSE},
    "document.extract_json": {"text": "Invoice 44 for Acme Corp, total 1284500.50 USD, due 2024-03-01",
                              "fields": ["invoice_number", "customer", "total", "due_date"]},
    "sim.run": {"topic": "a new transaction fee is introduced on a busy L2", "population": 40,
                "rounds": 5},
    "workflow.compose": {"steps": [{"endpoint": "csv.profile", "input": {"csv": "a,b\n1,2\n3,4"}},
                                   {"endpoint": "text.stats", "input": {"text": "hello world"}}]},
    "receipt.verify": {},   # filled in at runtime with a genuine envelope
}

SKIP_NETWORK = {"url.inspect"}          # needs egress; exercised separately against the live deploy

# Services that call the model. They are ATTEMPTED whenever a key is configured, and only skipped when
# there is genuinely no key — an unconditional skip is why sim.run and workflow.compose sat here with
# inputs that could never have worked (`{contract}` and `{goal}`, when they take `{topic}` and
# `{steps}`). Nothing caught it until a real paid sweep hit them, because nothing ever ran them.
NEEDS_LLM = {"text.summarize", "document.extract_json", "sim.run", "workflow.compose"}


def _llm_configured() -> bool:
    import pathlib as _p
    if os.environ.get("MINIMAX_API_KEY") or os.environ.get("LLM_API_KEY"):
        return True
    env = _p.Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("MINIMAX_API_KEY", "LLM_API_KEY") and v.strip():
                os.environ.setdefault(k.strip(), v.strip())
                return True
    return False


def summarise(v: object, width: int = 300) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    s = " ".join(s.split())
    return s[:width] + ("…" if len(s) > width else "")


def main() -> int:
    filters = [a.lower() for a in sys.argv[1:]]
    llm_ok = _llm_configured()
    rt = Runtime(build_registry(), get_settings())
    genuine = json.loads(rt.execute(ArtifactRequest(endpoint="hash.compute",
                                                    input={"text": "seed"})).model_dump_json())
    CASES["receipt.verify"] = {"envelope": genuine, "expected_public_key": rt.signer.public_hex}

    priced = sorted(n["endpoint"] for n in rt.registry.list() if n["price_usdt"] > 0)
    missing = [e for e in priced if e not in CASES]
    if missing:
        print(f"!! {len(missing)} priced endpoint(s) have no exercise case: {missing}\n")

    counts = {"ok": 0, "failed": 0, "checkfail": 0, "skipped": 0, "nocase": len(missing)}
    for ep in priced:
        if filters and not any(f in ep.lower() for f in filters):
            continue
        if ep in SKIP_NETWORK or (ep in NEEDS_LLM and not llm_ok):
            counts["skipped"] += 1
            why = "network" if ep in SKIP_NETWORK else "no LLM key configured"
            print(f"-  {ep:<24} SKIPPED ({why})")
            continue
        if ep not in CASES:
            continue
        env = rt.execute(ArtifactRequest(endpoint=ep, input=CASES[ep]))
        failed = [c.name for c in env.validation.tests if not c.passed]
        if not env.ok:
            counts["failed"] += 1
            mark = "FAIL"
        elif failed:
            counts["checkfail"] += 1
            mark = "CHECK"
        else:
            counts["ok"] += 1
            mark = "ok"
        print(f"{mark:>5} {ep:<24} L={env.validation.level.value.split('_')[0]:<3} "
              f"checks={len(env.validation.tests)}"
              f"{' FAILED=' + str(failed) if failed else ''}")
        if not env.ok:
            print(f"      error: {env.error.code if env.error else '?'} — "
                  f"{(env.error.message if env.error else '')[:160]}")
        else:
            print(f"      -> {summarise(env.result)}")
            for a in env.artifacts:
                print(f"      artifact: {a.name} {a.bytes}B {a.mime_type} inlined={a.content_inlined}")
    print("\n" + json.dumps(counts))
    return 1 if counts["failed"] or counts["checkfail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
