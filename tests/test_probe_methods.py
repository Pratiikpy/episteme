"""Regression: OKX probes with a BARE GET (no body). A POST-only route would 405 and
fail x402 listing validation. Every verb must hit the paywall and return 402.
(Pattern confirmed by the working Aletheia deployment: spaceless route key = verb '*'.)
"""
import base64
import json

import pytest


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from gateway import create_app
    return TestClient(create_app())


def _assert_402_challenge(resp):
    assert resp.status_code == 402, f"expected 402, got {resp.status_code}"
    hdr = resp.headers.get("PAYMENT-REQUIRED")
    assert hdr, "missing PAYMENT-REQUIRED header"
    ch = json.loads(base64.b64decode(hdr))
    assert ch["x402Version"] == 2
    a = ch["accepts"][0]
    assert a["network"] == "eip155:196"
    assert a["asset"] == "0x779ded0c9e1022225f8e0630b35a9b54be713736"
    return ch


def test_bare_get_returns_402_not_405(client):
    """The exact OKX availability probe: GET with no body, no headers."""
    _assert_402_challenge(client.get("/a2mcp/hash.compute"))


def test_post_with_no_body_returns_402(client):
    _assert_402_challenge(client.post("/a2mcp/hash.compute"))


def test_other_verbs_also_challenge(client):
    for verb in ("put", "patch", "delete"):
        r = getattr(client, verb)("/a2mcp/hash.compute")
        assert r.status_code == 402, f"{verb.upper()} -> {r.status_code} (must be 402)"


def test_bare_get_on_every_paid_endpoint(client):
    """No paid endpoint may answer the bare probe with 404/405/500."""
    nodes = client.get("/nodes").json()["nodes"]
    # Exclude ONLY the internal differential verifiers, by name. This used to exclude by
    # `.verify`/`.alt` suffix, which also skipped artifact.verify and receipt.verify — the two
    # priced services that were 404ing in production. The suffix filter is precisely why this
    # test, whose whole job is to catch that, did not.
    from gateway import _INTERNAL_ONLY
    paid = [n["endpoint"] for n in nodes
            if n["price_usdt"] and n["endpoint"] not in _INTERNAL_ONLY]
    assert paid
    for ep in paid:
        r = client.get(f"/a2mcp/{ep}")
        assert r.status_code == 402, f"{ep}: bare GET -> {r.status_code}"
        assert r.headers.get("PAYMENT-REQUIRED"), f"{ep}: no PAYMENT-REQUIRED header"


def test_unknown_endpoint_still_404_on_get(client):
    assert client.get("/a2mcp/not.a.real.endpoint").status_code == 404
