"""Live HTTP probe against a running Episteme server (real wire, not TestClient)."""
import base64, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
import x402

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8099")

# 1) unpaid -> expect 402 + PAYMENT-REQUIRED header
r = httpx.post(f"{BASE}/a2mcp/hash.compute", json={"input": {"text": "hi"}}, timeout=15)
print("unpaid status:", r.status_code)
hdr = r.headers.get("payment-required") or r.headers.get("PAYMENT-REQUIRED")
print("PAYMENT-REQUIRED header present:", bool(hdr))
challenge = json.loads(base64.b64decode(hdr))
acc = challenge["accepts"][0]
print("x402Version:", challenge["x402Version"], "| network:", acc["network"], "| asset:", acc["asset"], "| amount:", acc["amount"])

# 2) paid -> expect 200 + deliverable + X-PAYMENT-RESPONSE
pay = x402.make_dev_payment(challenge)
r2 = httpx.post(f"{BASE}/a2mcp/hash.compute", json={"input": {"text": "hi"}},
                headers={"X-PAYMENT": pay}, timeout=15)
print("paid status:", r2.status_code)
env = r2.json()
print("ok:", env.get("ok"), "| sha256:", env.get("result", {}).get("digests", {}).get("sha256", "")[:16], "...")
print("verification level:", env.get("validation", {}).get("level"))
print("signed receipt algo:", env.get("receipt", {}).get("algo"))
print("X-PAYMENT-RESPONSE present:", bool(r2.headers.get("x-payment-response")))

# 3) health (gate No.2 responsiveness)
h = httpx.get(f"{BASE}/healthz", timeout=10)
print("healthz:", h.status_code, h.json().get("status"))
ok = (r.status_code == 402 and bool(hdr) and r2.status_code == 200 and env.get("ok") is True and h.status_code == 200)
print("LIVE_HTTP_PROBE_PASSED:", ok)
sys.exit(0 if ok else 1)
