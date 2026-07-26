"""OKX x402 facilitator client — production settlement on X Layer.

Matches the official OKX client exactly (okx/payments → x402/http/okx_facilitator_client.py):
  base:    https://web3.okx.com
  path:    /api/v6/pay/x402
  routes:  POST /verify, POST /verify-signature, POST /settle, GET /supported,
           GET /settle/status?txHash=...
  body:    {"x402Version": 2, "paymentPayload": {...}, "paymentRequirements": {...}}
           (+ "syncSettle": bool on /settle)
  auth:    HMAC-SHA256 base64 over (timestamp + method + path + body) with headers
           OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP / OK-ACCESS-PASSPHRASE
  response: {"code","msg","data"} envelope → unwrapped

Credentials come from env (OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE) and are
referenced by name only; values are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

OKX_DEFAULT_BASE_URL = "https://web3.okx.com"
OKX_BASE_PATH = "/api/v6/pay/x402"


class FacilitatorError(RuntimeError):
    pass


def okx_timestamp(now: datetime | None = None) -> str:
    """ISO UTC timestamp in OKX format, e.g. 2026-07-25T07:30:00.123Z."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def compute_signature(secret_key: str, timestamp: str, method: str, path: str, body: str) -> str:
    prehash = timestamp + method + path + body
    mac = hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def create_headers(api_key: str, secret_key: str, passphrase: str,
                   method: str, path: str, body: str = "",
                   timestamp: str | None = None) -> dict[str, str]:
    ts = timestamp or okx_timestamp()
    return {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": compute_signature(secret_key, ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }


def build_body(payment_payload: dict, payment_requirements: dict,
               sync_settle: bool | None = None) -> dict:
    body = {
        "x402Version": 2,
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    }
    if sync_settle is not None:
        body["syncSettle"] = sync_settle
    return body


def unwrap_envelope(data, endpoint: str) -> dict:
    """OKX responses are {"code","msg","data"}; code '0' means success."""
    if not isinstance(data, dict):
        raise FacilitatorError(f"non-object response from {endpoint}: {str(data)[:120]}")
    if "code" in data:
        try:
            code_int = int(data["code"])
        except (TypeError, ValueError):
            code_int = -1
        if code_int != 0:
            raise FacilitatorError(f"OKX API error (code={data['code']}) from {endpoint}: {data.get('msg')}")
        inner = data.get("data")
        if isinstance(inner, list):
            return inner[0] if inner else {}
        return inner if isinstance(inner, dict) else {}
    return data


class OkxFacilitator:
    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 base_url: str = OKX_DEFAULT_BASE_URL, sync_settle: bool = True,
                 timeout: float = 20.0):
        if not (api_key and secret_key and passphrase):
            raise FacilitatorError("OKX API credentials required (key/secret/passphrase)")
        self.base_url = (base_url or OKX_DEFAULT_BASE_URL).rstrip("/")
        self.sync_settle = sync_settle
        self.timeout = timeout
        self._key, self._secret, self._pass = api_key, secret_key, passphrase

    def _request(self, method: str, endpoint: str, body: dict | None = None) -> dict:
        import httpx
        path = OKX_BASE_PATH + endpoint
        url = self.base_url + path
        body_str = json.dumps(body) if body is not None else ""
        headers = create_headers(self._key, self._secret, self._pass, method, path, body_str)
        try:
            if method == "GET":
                r = httpx.get(url, headers=headers, timeout=self.timeout)
            else:
                r = httpx.post(url, headers=headers, content=body_str, timeout=self.timeout)
        except Exception as e:
            raise FacilitatorError(f"transport error to {endpoint}: {e}")
        if not (200 <= r.status_code < 300):
            raise FacilitatorError(f"HTTP {r.status_code} from {endpoint}: {r.text[:200]}")
        return unwrap_envelope(r.json(), endpoint)

    def verify(self, payment_payload: dict, payment_requirements: dict) -> dict:
        return self._request("POST", "/verify", build_body(payment_payload, payment_requirements))

    def verify_signature(self, payment_payload: dict, payment_requirements: dict) -> dict:
        return self._request("POST", "/verify-signature",
                             build_body(payment_payload, payment_requirements))

    def settle(self, payment_payload: dict, payment_requirements: dict) -> dict:
        return self._request("POST", "/settle",
                             build_body(payment_payload, payment_requirements,
                                        sync_settle=self.sync_settle))

    def supported(self) -> dict:
        return self._request("GET", "/supported")

    def settle_status(self, tx_hash: str) -> dict:
        return self._request("GET", f"/settle/status?txHash={tx_hash}")
