"""A caller who says nothing must not be billed, and must not have to pay to learn the contract.

A buying agent decides how to call a service by reading the x402 challenge. When the challenge
carries only prose, the agent sends an empty body, pays, and gets back an explanation instead of an
artifact. The money is gone and nothing was computed. Several nodes here take their input through a
helper whose parameter names appear nowhere in the endpoint name, price or description, so guessing
is not a realistic fallback.

Two guarantees, and both have to hold or the other is worthless: the contract is published *before*
payment, and an empty request is refused *before* settlement rather than explained after it.
"""
from __future__ import annotations

import base64
import json

import pytest

import x402
from config import get_settings


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from gateway import create_app
    return TestClient(create_app())


PAID_ENDPOINT = "file.inspect"


def _challenge(resp) -> dict:
    hv = resp.headers.get(x402.PAYMENT_REQUIRED_HEADER)
    assert hv, "every 402 must carry the PAYMENT-REQUIRED header"
    return json.loads(base64.b64decode(hv))


def test_challenge_publishes_the_input_contract():
    """Without this the caller is guessing, and a wrong guess costs them the fee."""
    s = get_settings()
    _, ch = x402.build_challenge(PAID_ENDPOINT, "0.001", s, "test",
                                 input_schema={"type": "object",
                                               "properties": {"text": {"type": "string"}}})
    assert ch["resource"]["inputSchema"]["properties"]["text"]["type"] == "string"


def test_challenge_omits_the_key_when_there_is_no_schema():
    """An absent schema must be absent, not present and empty — an empty object reads as
    'this endpoint takes no arguments', which is a different and wrong statement."""
    s = get_settings()
    _, ch = x402.build_challenge(PAID_ENDPOINT, "0.001", s, "test", input_schema=None)
    assert "inputSchema" not in ch["resource"]


def test_unpaid_empty_probe_still_returns_402(client):
    """The listing validator probes with no body at all and reads the status code. This must not
    regress to 400 or 200 — a listing has been rejected for exactly that."""
    r = client.post(f"/a2mcp/{PAID_ENDPOINT}", json={})
    assert r.status_code == 402
    assert r.headers.get(x402.PAYMENT_REQUIRED_HEADER)


def test_live_challenge_carries_the_schema(client):
    r = client.post(f"/a2mcp/{PAID_ENDPOINT}", json={})
    ch = _challenge(r)
    schema = ch["resource"].get("inputSchema")
    assert schema, "the challenge must state what to send"
    assert schema.get("type") == "object"
    assert schema.get("properties"), "a schema with no properties tells the caller nothing"


def test_paid_call_with_no_input_is_refused_before_settlement(client, monkeypatch):
    """The defect itself: pay, send nothing, get charged, receive no artifact.

    The payment path is failed loudly if it is reached at all — an empty request must never get as
    far as settlement, so any call to verify is a regression regardless of what it would return.
    """
    def must_not_settle(*a, **k):                                    # noqa: ANN002, ANN003
        raise AssertionError("settlement was attempted for a request that supplied no input")

    monkeypatch.setattr(x402, "verify_payment", must_not_settle)

    r = client.post(f"/a2mcp/{PAID_ENDPOINT}", json={},
                    headers={x402.PAYMENT_HEADER: "irrelevant-because-it-must-not-be-read"})
    assert r.status_code == 402
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "no_input_supplied"
    assert "not_charged" in body
    assert "result" not in body, "a refusal must not be shaped like a result"


def test_the_refusal_hands_back_a_usable_example(client):
    """Telling someone they sent nothing is only half an answer; the other half is what to send."""
    r = client.post(f"/a2mcp/{PAID_ENDPOINT}", json={})
    body = r.json()
    example = body.get("example") or body.get("example_request") or {}
    assert example, "the refusal must include a worked example"
    assert all(not isinstance(v, dict) or v for v in example.values())
