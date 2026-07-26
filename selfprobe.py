"""Self-probe — mimics the OKX reviewer's x402 gate (No.1) before we submit.

Runs against a FastAPI TestClient (offline) or, with a base URL, a live deploy.
Asserts: unpaid -> 402 + PAYMENT-REQUIRED header that decodes to the exact v2
challenge with the right chain/token/amount/payTo; paid -> 200 + deliverable.
Ship only if report['passed'] is True (Greenlight/PreFlight pattern).
"""
from __future__ import annotations

import base64
import json

import x402
from config import get_settings


def probe_endpoint(client, endpoint: str) -> dict:
    settings = get_settings()
    checks: list[dict] = []

    def chk(name, ok, detail=""):
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    # 1. unpaid call
    r = client.post(f"/a2mcp/{endpoint}", json={"input": {}})
    chk("unpaid_status_402", r.status_code == 402, f"got {r.status_code}")
    hdr = r.headers.get(x402.PAYMENT_REQUIRED_HEADER)
    chk("payment_required_header_present", bool(hdr))

    challenge = None
    if hdr:
        try:
            challenge = json.loads(base64.b64decode(hdr))
            chk("header_decodes_json", True)
        except Exception as e:
            chk("header_decodes_json", False, str(e))

    if challenge:
        chk("x402Version_2", challenge.get("x402Version") == 2, str(challenge.get("x402Version")))
        acc = (challenge.get("accepts") or [{}])[0]
        chk("scheme_exact", acc.get("scheme") == "exact", acc.get("scheme"))
        chk("network_xlayer", acc.get("network") == "eip155:196", acc.get("network"))
        chk("asset_usdt0", acc.get("asset") == settings.x402_asset, acc.get("asset"))
        chk("amount_present", bool(acc.get("amount")), acc.get("amount"))
        chk("payTo_present", bool(acc.get("payTo")), acc.get("payTo"))
        chk("extra_eip3009", bool(acc.get("extra", {}).get("name")), str(acc.get("extra")))
        chk("resource_url_matches",
            (challenge.get("resource", {}).get("url", "").endswith(f"/a2mcp/{endpoint}")),
            challenge.get("resource", {}).get("url"))

    # 2. paid call (forge a valid dev payment for the challenge)
    if challenge:
        pay = x402.make_dev_payment(challenge)
        r2 = client.post(f"/a2mcp/{endpoint}", json={"input": _sample_input(endpoint)},
                         headers={x402.PAYMENT_HEADER: pay})
        chk("paid_status_200", r2.status_code == 200, f"got {r2.status_code}")
        try:
            env = r2.json()
            chk("deliverable_ok", env.get("ok") is True and bool(env.get("result")))
            chk("has_signed_receipt", bool(env.get("receipt", {}).get("signature")))
            chk("payment_response_header", bool(r2.headers.get(x402.PAYMENT_RESPONSE_HEADER)))
        except Exception as e:
            chk("deliverable_ok", False, str(e))

    passed = all(c["pass"] for c in checks)
    return {"endpoint": endpoint, "passed": passed, "checks": checks}


def _sample_input(endpoint: str) -> dict:
    samples = {
        "file.inspect": {"text": "hello"},
        "hash.compute": {"text": "hello"},
        "artifact.verify": {"text": "hello", "expected_sha256":
                            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"},
        "text.diff": {"a": "x", "b": "y"},
        "unit.convert": {"value": 1, "from": "km", "to": "m"},
        "csv.profile": {"csv": "a,b\n1,2\n3,4\n"},
        "data.query_sql": {"csv": "a,b\n1,2\n", "sql": "SELECT * FROM t"},
        "data.diff": {"a": "x\n1\n", "b": "x\n2\n"},
        "url.to_markdown": {"html": "<h1>Hi</h1><p>body</p>"},
        "document.to_markdown": {"text": "# Title\nbody"},
        "document.chunk": {"text": "Para one.\n\nPara two.", "chunk_size": 100, "overlap": 10},
        "openapi.inspect": {"spec": {"openapi": "3.0.0", "info": {"title": "x"},
                                     "paths": {"/a": {"get": {"operationId": "a"}}}}},
        "data.transform_json": {"data": {"a": 1, "b": 2}, "op": "keys"},
        "document.compare": {"a": "line1\nline2", "b": "line1\nline2 changed"},
        "data.convert": {"csv": "a,b\n1,2\n3,4\n", "to": "json"},
        "data.validate": {"rows": [{"a": 1}], "schema": {"type": "object",
                          "properties": {"a": {"type": "integer"}}, "required": ["a"]}},
        "data.stats": {"rows": [{"x": 1}, {"x": 3}, {"x": 5}]},
        "data.dedupe": {"rows": [{"a": 1}, {"a": 1}, {"a": 2}]},
        "chart.spec": {"rows": [{"k": "a", "v": 1}, {"k": "b", "v": 2}], "x": "k", "y": "v", "mark": "bar"},
        "openapi.lint": {"spec": {"openapi": "3.0.0", "info": {"title": "x", "version": "1"},
                         "paths": {"/a": {"get": {"operationId": "a",
                         "responses": {"200": {"description": "ok"}}, "summary": "a"}}}}},
        "openapi.diff": {"old": {"paths": {"/a": {"get": {}}}},
                         "new": {"paths": {"/a": {"get": {}}, "/b": {"get": {}}}}},
        "schema.generate": {"example": {"a": 1, "b": "x"}},
        "page.links": {"html": "<a href='/x'>X</a><a href='https://e.com/p'>E</a>"},
        "page.extract": {"html": "<h1>T</h1><p class='b'>body text</p>",
                         "selectors": {"title": "h1", "body": ".b"}},
        "repo.map": {"files": {"a.py": "import os\ndef f():\n    return 1\nclass C:\n    pass\n"}},
        "repo.lint": {"files": {"a.py": "x = 1\n"}},
        "repo.scan_secrets": {"files": {"cfg.py": "api_key = 'ABCDEFGHIJKLMNOP1234'\n"}},
        "workflow.compose": {"steps": [
            {"id": "i", "endpoint": "file.inspect", "input": {"text": "hi"}},
            {"id": "h", "endpoint": "hash.compute", "input": {"text": "hi"}},
        ]},
        "sim.run": {"topic": "feature adoption", "population": 40, "rounds": 5,
                    "seed": 1, "intervention_strength": 0.2},
        "document.redact_pii": {"text": "Reach me at jane@acme.com or 415-555-1234."},
        "email.validate": {"email": "jane.doe@example.com"},
        "data.join": {"left": [{"id": 1, "a": "x"}], "right": [{"id": 1, "b": "y"}], "on": "id", "how": "inner"},
        "data.clean": {"rows": [{"n": "  Bob  "}], "ops": ["trim", "collapse_ws"]},
        "object.diff": {"a": {"x": 1, "y": 2}, "b": {"x": 1, "y": 3, "z": 4}},
        "site.map": {"sitemap": "<urlset><url><loc>https://e.com/a</loc></url><url><loc>https://e.com/b</loc></url></urlset>"},
        "api.to_mcp": {"spec": {"openapi": "3.0.0", "paths": {"/u": {"get": {"operationId": "getU",
                       "summary": "get user", "parameters": [{"name": "id", "in": "query",
                       "schema": {"type": "string"}}]}}}}},
        "mcp.validate": {"tools": [{"name": "t1", "description": "d", "inputSchema": {"type": "object", "properties": {}}}]},
        "text.stats": {"text": "Hello world. This is a test sentence."},
        "csv.to_table": {"csv": "a,b\n1,2\n3,4\n", "format": "github"},
        "data.pivot": {"rows": [{"cat": "x", "v": 1}, {"cat": "x", "v": 3}, {"cat": "y", "v": 5}],
                       "group_by": "cat", "agg": {"v": "sum"}},
        "robots.check": {"robots": "User-agent: *\nDisallow: /private\nSitemap: https://e.com/sitemap.xml",
                         "path": "/public", "user_agent": "*"},
    }
    return samples.get(endpoint, {"text": "hello"})


def run_all(client) -> dict:
    from nodes import build_registry
    reg = build_registry()
    from gateway import _INTERNAL_ONLY
    endpoints = [n["endpoint"] for n in reg.list()
                 # by name, not by suffix — a `.verify` suffix filter also skipped the two priced
                 # verifier services and hid the fact that they 404'd in production
                 if n["endpoint"] not in _INTERNAL_ONLY
                 and n["price_usdt"] and n["price_usdt"] > 0
                 and n["endpoint"] not in {
                     "document.extract_json",  # needs live LLM
                     "text.summarize",         # needs live LLM
                     "url.inspect",            # needs network
                     "image.inspect", "image.transform", "pdf.manipulate",  # binary inputs (tested directly)
                     "receipt.verify",  # needs a full envelope input (tested directly)
                 }]
    reports = [probe_endpoint(client, e) for e in endpoints]
    return {"passed": all(r["passed"] for r in reports), "reports": reports}
