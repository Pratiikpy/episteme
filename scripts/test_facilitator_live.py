"""Verify REAL OKX facilitator auth using the credentials already on this machine
(verity/.env). Never prints secret values — only presence and the API's response.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC = Path(r"C:\Users\prate\okx\verity\.env")
WANT = {"OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"}
found = {}
if SRC.exists():
    for line in SRC.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in WANT and v:
            found[k] = v
            os.environ[k] = v

print("credentials found (values hidden):", {k: f"<{len(v)} chars>" for k, v in found.items()})
if WANT - set(found):
    print("MISSING:", WANT - set(found))
    raise SystemExit(1)

from okx_facilitator import OkxFacilitator, FacilitatorError

fac = OkxFacilitator(found["OKX_API_KEY"], found["OKX_SECRET_KEY"], found["OKX_PASSPHRASE"])
print("facilitator base:", fac.base_url + "/api/v6/pay/x402")
try:
    data = fac.supported()
    print("AUTH OK -> /supported returned:", str(data)[:400])
    print("RESULT: real OKX facilitator authentication WORKS")
except FacilitatorError as e:
    print("FacilitatorError:", str(e)[:400])
    print("RESULT: reachable but rejected — see message above")
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])
