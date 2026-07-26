"""Export A2MCP routes from listing.py -> gateway/routes.json (single source of truth).

Route KEYS carry no HTTP-method prefix on purpose: the OKX SDK treats a spaceless key as
verb "*", so the paywall answers 402 on OKX's bare-GET availability probe (not 404/405).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings
from nodes import build_registry
import listing as L


def _is_free(fee: str) -> bool:
    """A service is free only if its fee really is zero — anything else must be paywalled."""
    try:
        return float(fee) == 0.0
    except (TypeError, ValueError):
        return False  # unparseable fee: treat as paid so we fail closed, never free


settings = get_settings()
manifest = L.build_listing(build_registry(), settings)

routes = {}
for svc in manifest["services"]:
    # Paywall EVERY listed service that carries a non-zero fee, whatever its declared type.
    # Rationale: the marketplace registers each of these with that fee and probes the endpoint
    # for a 402. A service that is listed at a price but left off this table serves its work
    # for free — OKX reads that as a non-compliant paid endpoint (and any agent gets it gratis).
    # Only a genuinely free service (fee "0") may answer 200 unpaid.
    if _is_free(svc["fee"]):
        continue
    path = "/" + svc["endpoint"].split("/a2mcp/", 1)[1]
    routes["/a2mcp" + path] = {
        "price": "$" + svc["fee"],
        # decimals:6 must live in `extra` — USDT0 is not in OKX's token list and a
        # top-level `decimals` is dropped by OKX's canonical re-serialization.
        "maxTimeoutSeconds": 300,
        "description": svc["serviceDescription"].replace("\n", " "),
        "mimeType": "application/json",
    }

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway", "routes.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump(routes, f, indent=2, ensure_ascii=False)

print(f"wrote {len(routes)} A2MCP routes -> gateway/routes.json")
for k, v in list(routes.items())[:5]:
    print(f"  {k}  {v['price']}")
print(f"  ... (+{max(0, len(routes) - 5)} more)")
