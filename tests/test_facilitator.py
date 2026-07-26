"""Tests for the OKX x402 facilitator client (auth, body, envelope) + graceful degradation."""
import base64
import hashlib
import hmac

import pytest

import okx_facilitator as F


def test_base_url_and_path_match_official():
    assert F.OKX_DEFAULT_BASE_URL == "https://web3.okx.com"
    assert F.OKX_BASE_PATH == "/api/v6/pay/x402"


def test_timestamp_format():
    ts = F.okx_timestamp()
    # 2026-07-25T07:30:00.123Z
    assert ts.endswith("Z") and "T" in ts and len(ts) == 24


def test_signature_matches_hmac_sha256_prehash():
    secret, ts, method, path, body = "s3cr3t", "2026-07-25T07:30:00.123Z", "POST", "/api/v6/pay/x402/verify", '{"a":1}'
    got = F.compute_signature(secret, ts, method, path, body)
    expect = base64.b64encode(
        hmac.new(secret.encode(), (ts + method + path + body).encode(), hashlib.sha256).digest()
    ).decode()
    assert got == expect


def test_headers_contain_required_okx_fields():
    h = F.create_headers("key", "secret", "pass", "POST", "/api/v6/pay/x402/verify", "{}",
                         timestamp="2026-07-25T07:30:00.123Z")
    assert h["OK-ACCESS-KEY"] == "key"
    assert h["OK-ACCESS-PASSPHRASE"] == "pass"
    assert h["OK-ACCESS-TIMESTAMP"] == "2026-07-25T07:30:00.123Z"
    assert h["Content-Type"] == "application/json"
    assert h["OK-ACCESS-SIGN"]


def test_build_body_shape():
    b = F.build_body({"p": 1}, {"r": 2})
    assert b == {"x402Version": 2, "paymentPayload": {"p": 1}, "paymentRequirements": {"r": 2}}
    b2 = F.build_body({"p": 1}, {"r": 2}, sync_settle=True)
    assert b2["syncSettle"] is True


def test_envelope_unwrap_success_and_error():
    assert F.unwrap_envelope({"code": "0", "msg": "", "data": {"isValid": True}}, "verify") == {"isValid": True}
    assert F.unwrap_envelope({"code": "0", "data": [{"x": 1}]}, "verify") == {"x": 1}
    with pytest.raises(F.FacilitatorError):
        F.unwrap_envelope({"code": "50011", "msg": "bad sign"}, "verify")


def test_requires_credentials():
    with pytest.raises(F.FacilitatorError):
        F.OkxFacilitator("", "", "")


def test_facilitator_mode_degrades_gracefully_without_creds(monkeypatch):
    """Without OKX creds, facilitator mode must fail closed with a clear reason (never 200)."""
    import x402
    from config import Settings
    s = Settings()
    monkeypatch.setattr(s, "x402_mode", "facilitator", raising=False)
    monkeypatch.setattr(s, "_okx_api_key", None, raising=False)
    monkeypatch.setattr(s, "_okx_api_secret", None, raising=False)
    monkeypatch.setattr(s, "_okx_api_passphrase", None, raising=False)
    _, ch = x402.build_challenge("hash.compute", "0.001", s, "t")
    pay = x402.make_dev_payment(ch)
    res = x402.verify_payment(pay, s)
    assert res.ok is False
    assert "OKX_API_KEY" in res.detail or "not configured" in res.detail
