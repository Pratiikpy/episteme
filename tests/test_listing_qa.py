"""Listing self-validation (mirrors OKX validate-listing QA)."""
import listing as L


def test_our_listing_passes_qa(runtime, registry):
    from config import get_settings
    m = L.build_listing(registry, get_settings())
    report = L.validate_listing(m)
    assert report["pass"] is True, f"listing QA blocks: {[i for i in report['issues'] if i['severity']=='block']}"
    assert report["block_count"] == 0
    assert len(m["services"]) >= 25


def test_validate_catches_bad_fields():
    bad = {
        "name": "test-agent",  # test marker
        "description": "x",
        "services": [
            {"serviceName": "Q", "serviceDescription": "one line only",
             "serviceType": "weird", "fee": "10 USDT", "endpoint": "https://ok.example/x"},
            {"serviceName": "Localhost Caller", "serviceDescription": "cap\ninputs",
             "serviceType": "A2MCP", "fee": "0.01", "endpoint": "http://localhost/x"},
        ],
    }
    r = L.validate_listing(bad)
    assert r["pass"] is False
    fields = {i["field"] for i in r["issues"]}
    assert any("name" in f for f in fields)                 # test marker
    assert any("serviceName" in f for f in fields)          # "Q" too short
    assert any("serviceType" in f for f in fields)          # "weird"
    assert any("fee" in f for f in fields)                  # "10 USDT"
    assert any("serviceDescription" in f for f in fields)   # 1-part
    assert any("endpoint" in f for f in fields)             # http://localhost (A2MCP)


def test_every_priced_listed_service_is_paywalled():
    """A service the marketplace lists at a fee MUST be in the gateway paywall table.

    A listed-but-unpaywalled service serves its work free while OKX advertises a price for it —
    which OKX reads as a non-compliant paid endpoint, and which lets any agent take the work for
    nothing. This regressed once when export_routes.py skipped services by declared type instead
    of by fee, dropping the two priced A2A-typed services (sim.run, workflow.compose)."""
    import json
    import os

    from config import get_settings
    from nodes import build_registry
    import listing as listing_mod

    manifest = listing_mod.build_listing(build_registry(), get_settings())
    routes_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "gateway", "routes.json")
    routes = json.load(open(routes_path, encoding="utf-8"))

    missing = []
    for svc in manifest["services"]:
        try:
            free = float(svc["fee"]) == 0.0
        except (TypeError, ValueError):
            free = False
        if free:
            continue
        path = "/a2mcp/" + svc["endpoint"].split("/a2mcp/", 1)[1]
        if path not in routes:
            missing.append((svc["serviceName"], path, svc["fee"]))

    assert not missing, f"priced services missing from the paywall (would serve free): {missing}"


def test_paywall_price_matches_the_listed_fee():
    """The 402 must charge exactly what the listing advertises — a mismatch is a mechanical reject."""
    import json
    import os

    from config import get_settings
    from nodes import build_registry
    import listing as listing_mod

    manifest = listing_mod.build_listing(build_registry(), get_settings())
    routes_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "gateway", "routes.json")
    routes = json.load(open(routes_path, encoding="utf-8"))

    mismatched = []
    for svc in manifest["services"]:
        path = "/a2mcp/" + svc["endpoint"].split("/a2mcp/", 1)[1]
        spec = routes.get(path)
        if not spec:
            continue
        if spec["price"].lstrip("$") != svc["fee"]:
            mismatched.append((svc["serviceName"], svc["fee"], spec["price"]))

    assert not mismatched, f"listed fee != charged price: {mismatched}"
