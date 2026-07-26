"""x402 v2 unit tests — exact challenge shape, fee math, verify, replay protection."""
import base64
import json

import x402
from config import get_settings


def test_fee_to_min_units():
    assert x402.fee_to_min_units("0.01", 6) == "10000"
    assert x402.fee_to_min_units("1", 6) == "1000000"
    assert x402.fee_to_min_units("0.000001", 6) == "1"


def test_challenge_exact_shape():
    s = get_settings()
    header_val, ch = x402.build_challenge("file.inspect", "0.01", s, "test")
    # header decodes to same JSON
    decoded = json.loads(base64.b64decode(header_val))
    assert decoded["x402Version"] == 2
    acc = ch["accepts"][0]
    assert acc["scheme"] == "exact"
    assert acc["network"] == "eip155:196"          # X Layer mainnet
    assert acc["asset"] == "0x779ded0c9e1022225f8e0630b35a9b54be713736"  # USDT0
    assert acc["amount"] == "10000"                # 0.01 * 1e6
    assert acc["extra"] == {"name": s.x402_asset_name, "version": "1", "decimals": 6}
    assert ch["resource"]["url"].endswith("/a2mcp/file.inspect")
    assert "nonce" in ch


def test_verify_dev_signature_and_replay():
    s = get_settings()
    _, ch = x402.build_challenge("hash.compute", "0.001", s, "t")
    pay = x402.make_dev_payment(ch)
    r1 = x402.verify_payment(pay, s)
    assert r1.ok is True
    assert r1.response_header
    # single-use: replaying the same payment fails (nonce consumed)
    r2 = x402.verify_payment(pay, s)
    assert r2.ok is False


def test_verify_rejects_wrong_network():
    s = get_settings()
    _, ch = x402.build_challenge("hash.compute", "0.001", s, "t")
    pay = x402.make_dev_payment(ch)
    payload = json.loads(base64.b64decode(pay))
    payload["network"] = "eip155:8453"  # Base — wrong
    bad = base64.b64encode(x402.canonical(payload)).decode()
    assert x402.verify_payment(bad, s).ok is False
