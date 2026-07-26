"""x402 v2 for OKX.AI — the #1 review gate.

Builds the exact challenge the marketplace validates (PAYMENT-REQUIRED header,
base64 JSON, x402Version 2, network eip155:196, USDT0 asset, 6-decimal amount,
EIP-3009 `extra`), and verifies the X-PAYMENT header before replaying the call.

Two verify modes:
  - 'signature' (local/dev/test): the payer signs the challenge nonce; we verify
    the Ed25519 signature. Proves a real payment *authorization* without a chain.
  - 'facilitator' (production): delegate verify/settle to the OKX seller SDK /
    x402 facilitator on X Layer mainnet. (HTTP hook.)
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from decimal import Decimal, ROUND_DOWN

from config import Settings

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_HEADER = "X-PAYMENT"
PAYMENT_RESPONSE_HEADER = "X-PAYMENT-RESPONSE"

# in-memory nonce store (single-use challenges). Production: Redis/Valkey.
_ISSUED: dict[str, dict] = {}


def fee_to_min_units(fee: str | float, decimals: int) -> str:
    """'0.01' + 6 decimals -> '10000' (string, integer min units)."""
    q = Decimal(str(fee)) * (Decimal(10) ** decimals)
    return str(int(q.quantize(Decimal(1), rounding=ROUND_DOWN)))


def build_challenge(endpoint: str, fee_usdt: str, settings: Settings, description: str) -> tuple[str, dict]:
    """Return (base64_header_value, challenge_dict) for a paid endpoint."""
    nonce = uuid.uuid4().hex
    amount = fee_to_min_units(fee_usdt, settings.x402_asset_decimals)
    resource_url = f"{settings.public_base_url.rstrip('/')}/a2mcp/{endpoint}"
    challenge = {
        "x402Version": settings.x402_version,
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": settings.x402_scheme,          # "exact"
                "network": settings.x402_network,        # "eip155:196" (X Layer mainnet)
                "asset": settings.x402_asset,            # USDT0 on X Layer
                "amount": amount,                        # min units, 6 decimals
                "payTo": settings.pay_to,                # real X Layer receive wallet
                "maxTimeoutSeconds": settings.x402_max_timeout_seconds,
                # decimals MUST live inside `extra`: USDT0 is not in OKX's token list, and a
                # top-level `decimals` is dropped by OKX's canonical re-serialization.
                # (Confirmed by the live, x402-check-passing Aletheia/Reach challenges.)
                "extra": {"name": settings.x402_asset_name,
                          "version": settings.x402_asset_version,
                          "decimals": settings.x402_asset_decimals},
            }
        ],
        "nonce": nonce,
    }
    _ISSUED[nonce] = {"endpoint": endpoint, "amount": amount, "url": resource_url, "ts": time.time()}
    header_val = base64.b64encode(canonical(challenge)).decode("ascii")
    return header_val, challenge


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode_challenge(header_value: str) -> dict:
    """Reviewer-side: PAYMENT-REQUIRED header base64 must decode to valid JSON."""
    return json.loads(base64.b64decode(header_value))


class PaymentResult:
    def __init__(self, ok: bool, detail: str, response_header: str | None = None):
        self.ok = ok
        self.detail = detail
        self.response_header = response_header


def verify_payment(x_payment_header: str, settings: Settings) -> PaymentResult:
    """Verify the X-PAYMENT header. Returns PaymentResult(ok, detail, response_header)."""
    try:
        payload = json.loads(base64.b64decode(x_payment_header))
    except Exception as e:
        return PaymentResult(False, f"undecodable X-PAYMENT: {e}")

    nonce = payload.get("nonce")
    if not nonce or nonce not in _ISSUED:
        return PaymentResult(False, "unknown or expired challenge nonce")
    issued = _ISSUED[nonce]

    if payload.get("scheme") != settings.x402_scheme:
        return PaymentResult(False, "scheme mismatch")
    if payload.get("network") != settings.x402_network:
        return PaymentResult(False, "network mismatch (must be X Layer eip155:196)")
    if str(payload.get("amount")) != issued["amount"]:
        return PaymentResult(False, "amount mismatch vs challenge")

    if settings.x402_mode == "facilitator":
        ok, detail = _verify_via_facilitator(payload, settings)
    else:
        ok, detail = _verify_signature(payload, nonce)

    if not ok:
        return PaymentResult(False, detail)

    _ISSUED.pop(nonce, None)  # single-use → prevents replay of the same payment
    resp = base64.b64encode(canonical({"success": True, "nonce": nonce, "settledAt": time.time()})).decode("ascii")
    return PaymentResult(True, "verified", resp)


def _verify_signature(payload: dict, nonce: str) -> tuple[bool, str]:
    """Dev/test: payer proves authorization by signing the nonce (Ed25519)."""
    pub = payload.get("payer_pubkey")
    sig = payload.get("signature")
    if not pub or not sig:
        return False, "missing payer_pubkey/signature"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(bytes.fromhex(sig), nonce.encode())
        return True, "signature ok"
    except Exception as e:
        return False, f"bad signature: {e}"


def _verify_via_facilitator(payload: dict, settings: Settings) -> tuple[bool, str]:
    """Production verify+settle through the official OKX x402 facilitator on X Layer."""
    if not settings.facilitator_configured:
        return False, ("facilitator mode requires OKX_API_KEY / OKX_API_SECRET / "
                       "OKX_API_PASSPHRASE (not configured)")
    try:
        from okx_facilitator import OkxFacilitator, FacilitatorError
    except Exception as e:  # noqa
        return False, f"facilitator client unavailable: {e}"

    issued = _ISSUED.get(payload.get("nonce"), {})
    payment_requirements = {
        "scheme": settings.x402_scheme,
        "network": settings.x402_network,
        "asset": settings.x402_asset,
        "amount": issued.get("amount") or str(payload.get("amount")),
        "payTo": settings.pay_to,
        "maxTimeoutSeconds": settings.x402_max_timeout_seconds,
        "resource": issued.get("url") or settings.public_base_url,
        "extra": {"name": settings.x402_asset_name, "version": settings.x402_asset_version},
    }
    try:
        fac = OkxFacilitator(
            settings.okx_api_key, settings.okx_api_secret, settings.okx_api_passphrase,
            base_url=settings.x402_facilitator_url or settings.okx_facilitator_base_url,
            sync_settle=settings.okx_sync_settle,
        )
        v = fac.verify(payload, payment_requirements)
        if not (v.get("isValid") is True or v.get("valid") is True or v.get("success") is True):
            return False, f"facilitator verify rejected: {str(v)[:160]}"
        s = fac.settle(payload, payment_requirements)
        ok = (s.get("success") is True or s.get("settled") is True
              or bool(s.get("txHash") or s.get("transaction")))
        return (True, f"settled {s.get('txHash') or ''}".strip()) if ok else \
               (False, f"facilitator settle failed: {str(s)[:160]}")
    except FacilitatorError as e:
        return False, f"facilitator error: {e}"
    except Exception as e:  # noqa
        return False, f"facilitator unexpected error: {type(e).__name__}: {e}"


# ---- test helper: forge a valid dev payment for a given challenge (used by self-probe/tests)
def make_dev_payment(challenge: dict) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    nonce = challenge["nonce"]
    accept = challenge["accepts"][0]
    payload = {
        "x402Version": challenge["x402Version"],
        "scheme": accept["scheme"],
        "network": accept["network"],
        "asset": accept["asset"],
        "amount": accept["amount"],
        "nonce": nonce,
        "payer_pubkey": sk.public_key().public_bytes_raw().hex(),
        "signature": sk.sign(nonce.encode()).hex(),
    }
    return base64.b64encode(canonical(payload)).decode("ascii")
