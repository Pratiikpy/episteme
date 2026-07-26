"""A buyer who lands on the WRONG endpoint must be redirected, not dead-ended.

Why: a listed ASP on this marketplace took a 1-star review because a buyer's very first call landed on
the FIRST service in its listing — an endpoint that could not accept that request at all — even though
the service worked perfectly and the buyer's next call succeeded. Its owner observed the same funnel
twice: unfamiliar wallets default to whatever is listed first.

Episteme's first-listed service is `file.inspect`, which takes a file. Someone asking "is this token
safe?" gets INVALID_INPUT, and payment has already settled in the middleware by then. The reply must
therefore carry what the endpoint does, a request that would work, and where to go instead.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from gateway import create_app


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("EPISTEME_INTERNAL_SECRET", "s3cret-handoff")
    return TestClient(create_app())


def _post_paid(client: TestClient, endpoint: str, body: dict):
    """Call a PRICED endpoint through the documented internal hand-off, so the test exercises the real
    post-payment path (which is where a misdirected buyer actually lands) without signing an invoice."""
    return client.post(f"/a2mcp/{endpoint}", json=body,
                       headers={"X-Episteme-Internal": "s3cret-handoff"})


def test_invalid_input_carries_help_and_a_working_example(monkeypatch):
    c = _client(monkeypatch)
    r = _post_paid(c, "receipt.verify", {"input": {"question": "is 0xdAC17F95 safe to trade?"}})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_INPUT"
    help_ = body.get("help")
    assert help_, "an invalid-input failure must explain how to call the endpoint correctly"
    assert "example_request" in help_ and help_["example_request"], "must show a request that works"
    assert help_["schema_url"] == "/nodes/receipt.verify/schema"
    assert help_["all_services"] == "/nodes"
    assert "did_you_mean" in help_


def test_help_points_at_plausible_siblings(monkeypatch):
    c = _client(monkeypatch)
    # Wording that clearly belongs to a different family should surface that family.
    r = _post_paid(c, "receipt.verify", {"input": {"wanted": "pivot my csv table and dedupe rows"}})
    assert r.status_code == 422
    suggestions = r.json()["help"]["did_you_mean"]
    assert any(s.startswith(("data.", "csv.")) for s in suggestions), suggestions


def test_help_is_not_attached_to_successful_results(monkeypatch):
    c = _client(monkeypatch)
    r = _post_paid(c, "artifact.hash", {"input": {"text": "hello"}})
    if r.status_code == 200:
        assert "help" not in r.json(), "a successful result must not be padded with troubleshooting"


def test_no_input_still_returns_the_usage_contract_not_an_error(monkeypatch):
    """Distinct from wrong input: an EMPTY body is OKX's availability probe, and payment has settled,
    so it gets the contract at 200 rather than a 422 that charges for nothing."""
    c = _client(monkeypatch)
    r = _post_paid(c, "receipt.verify", {})
    assert r.status_code == 200
    assert r.json()["status"] == "no_input_supplied"
