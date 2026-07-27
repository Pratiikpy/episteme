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
_REGISTRY = build_registry()
manifest = L.build_listing(_REGISTRY, settings)

# OKX caps a service description at 500 characters and rejects the listing above it. The example is
# what makes the call succeed, so when the two compete the prose gives way, not the example.
_MAX_DESCRIPTION = 500


def _required_any_of(endpoint: str) -> list:
    """Field names of which at least one must be present for the call to produce anything.

    Union of `required` and the `anyOf` alternatives: a node taking either `text` or `content_b64`
    is satisfied by either, so the gateway must accept either and refuse only when none is there.
    An empty list means "cannot tell" and the gateway leaves that route alone — silence here must
    never become a refusal, because refusing a valid call is worse than the bug being fixed.
    """
    import schemas as _schemas

    node = _REGISTRY.get(endpoint)
    if node is None:
        return []
    schema = _schemas.schema_for(node) or {}
    keys = list(schema.get("required") or [])
    for alt in schema.get("anyOf", []):
        keys.extend(alt.get("required") or [])
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _with_example(description: str, endpoint: str) -> str:
    """Append a request that would actually work, taken from the node's own schema."""
    import json as _json

    import schemas as _schemas

    node = _REGISTRY.get(endpoint)
    if node is None:
        return description[:_MAX_DESCRIPTION]
    schema = _schemas.schema_for(node) or {}
    props = schema.get("properties") or {}
    if not props:
        return description[:_MAX_DESCRIPTION]

    # `anyOf` alternatives are mutually exclusive — showing two of them together tells the caller to
    # send both, which is precisely the wrong instruction.
    alts = [list(a["required"])[0] for a in schema.get("anyOf", []) if a.get("required")]
    keys = [k for k in (schema.get("required") or []) if k not in alts]
    if not keys:
        keys = [k for k in props if k not in alts][:2]
    if alts:
        keys = [alts[0], *keys]
    if not keys:
        return description[:_MAX_DESCRIPTION]

    example = {}
    for k in keys:
        spec = props.get(k) if isinstance(props.get(k), dict) else {}
        example[k] = spec.get("example") or spec.get("default") or f"<{k}>"
    suffix = " Send: " + _json.dumps(example, ensure_ascii=False, separators=(",", ":"))
    room = _MAX_DESCRIPTION - len(suffix)
    if room < 40:                      # an example with no description left is not worth shipping
        return description[:_MAX_DESCRIPTION]
    return description[:room].rstrip() + suffix

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
    endpoint = svc["endpoint"].split("/a2mcp/", 1)[1]
    routes["/a2mcp" + path] = {
        "price": "$" + svc["fee"],
        # decimals:6 must live in `extra` — USDT0 is not in OKX's token list and a
        # top-level `decimals` is dropped by OKX's canonical re-serialization.
        "maxTimeoutSeconds": 300,
        # The worked example rides in the description because that is the one field the x402 SDK
        # carries verbatim into both the PAYMENT-REQUIRED header and the mirrored body. A buying
        # agent reads the challenge to decide what to send; with prose alone it sends an empty body,
        # pays, and gets no artifact. Several nodes take their input through a helper whose parameter
        # names appear nowhere in the endpoint name or the price, so guessing is not a fallback.
        "description": _with_example(svc["serviceDescription"].replace("\n", " "), endpoint),
        "mimeType": "application/json",
        # The gateway refuses — without settling — a request carrying none of these. Emitted here
        # rather than hard-coded in the gateway so a new node cannot be added with a paywall that
        # bills for input it never received.
        "requiredAnyOf": _required_any_of(endpoint),
    }

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway", "routes.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump(routes, f, indent=2, ensure_ascii=False)

print(f"wrote {len(routes)} A2MCP routes -> gateway/routes.json")
for k, v in list(routes.items())[:5]:
    print(f"  {k}  {v['price']}")
print(f"  ... (+{max(0, len(routes) - 5)} more)")
