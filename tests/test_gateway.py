"""Gateway integration tests — x402 A2MCP flow, listing compliance, A2A, self-probe."""
import base64
import json

import pytest

import x402


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from gateway import create_app
    return TestClient(create_app())


def test_healthz_fast(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_nodes_list(client):
    r = client.get("/nodes")
    assert r.status_code == 200
    eps = [n["endpoint"] for n in r.json()["nodes"]]
    assert "file.inspect" in eps and "csv.profile" in eps


def test_unpaid_returns_402_with_header(client):
    r = client.post("/a2mcp/file.inspect", json={"input": {}})
    assert r.status_code == 402
    hdr = r.headers.get("PAYMENT-REQUIRED")
    assert hdr, "missing PAYMENT-REQUIRED header"
    ch = json.loads(base64.b64decode(hdr))
    assert ch["x402Version"] == 2
    assert ch["accepts"][0]["network"] == "eip155:196"


def test_paid_flow_returns_200_deliverable(client):
    # get challenge
    r = client.post("/a2mcp/hash.compute", json={"input": {"text": "hi"}})
    ch = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
    pay = x402.make_dev_payment(ch)
    r2 = client.post("/a2mcp/hash.compute", json={"input": {"text": "hi"}},
                     headers={"X-PAYMENT": pay})
    assert r2.status_code == 200
    env = r2.json()
    assert env["ok"] is True
    assert env["result"]["digests"]["sha256"]
    assert env["receipt"]["algo"] == "ed25519"
    assert r2.headers.get("X-PAYMENT-RESPONSE")


def test_unknown_endpoint_404(client):
    r = client.post("/a2mcp/nope.nope", json={})
    assert r.status_code == 404


def test_listing_is_okx_compliant(client):
    r = client.get("/listing")
    m = r.json()
    assert 3 <= len(m["name"]) <= 25
    assert len(m["description"]) <= 500
    names = set()
    for s in m["services"]:
        # camelCase keys
        assert set(["serviceName", "serviceDescription", "serviceType", "fee", "endpoint"]) <= set(s)
        assert 5 <= len(s["serviceName"]) <= 30
        assert s["serviceName"] != m["name"]           # service != agent name
        assert s["serviceType"] in ("A2MCP", "A2A")
        assert s["fee"].replace(".", "", 1).isdigit()  # digits-only string
        assert s["endpoint"].startswith("https://")
        assert "\n" in s["serviceDescription"]         # 2-part
        # no forbidden content
        low = s["serviceDescription"].lower()
        assert "http" not in low and "github" not in low and "e.g." not in low
        names.add(s["serviceName"])
    assert len(names) == len(m["services"])            # unique service names


def test_a2a_message_fast_reply(client):
    r = client.post("/a2a/message", json={"message": "I would like to use the services of agent ID 4927"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "Episteme" in r.json()["reply"]


def test_a2a_task_lifecycle(client):
    r = client.post("/a2a/tasks", json={"service": "launch-readiness", "brief": "audit my repo", "budget": "5"})
    tid = r.json()["id"]
    assert r.json()["status"] == "open"
    assert client.post(f"/a2a/tasks/{tid}/accept").json()["status"] == "accepted"
    d = client.post(f"/a2a/tasks/{tid}/deliver", json={"endpoint": "document.to_markdown",
                                                       "input": {"text": "# hi"}})
    assert d.json()["status"] == "delivered"
    assert d.json()["artifact"]["ok"] is True
    assert client.post(f"/a2a/tasks/{tid}/confirm").json()["status"] == "settled"


def test_selfprobe_all_paid_endpoints_pass(client):
    import selfprobe
    report = selfprobe.run_all(client)
    failed = [r for r in report["reports"] if not r["passed"]]
    assert report["passed"], f"self-probe failures: {failed}"


def test_internal_handoff_serves_without_second_402(client, monkeypatch):
    """The front Node gateway settles the payment via the OKX SDK, then proxies here. That hand-off
    must be served, not re-challenged — a second 402 means the caller paid and got nothing, which is
    what OKX rejects an ASP for."""
    monkeypatch.setenv("EPISTEME_INTERNAL_SECRET", "s3cret-handoff")
    r = client.post("/a2mcp/hash.compute", json={"input": {"text": "hi"}},
                    headers={"X-Episteme-Internal": "s3cret-handoff"})
    assert r.status_code == 200, f"trusted hand-off was re-challenged: {r.status_code} {r.text[:200]}"


def test_internal_handoff_rejects_wrong_secret(client, monkeypatch):
    """A forged or stale secret must still pay — otherwise anyone guessing the header gets paid work free."""
    monkeypatch.setenv("EPISTEME_INTERNAL_SECRET", "s3cret-handoff")
    r = client.post("/a2mcp/hash.compute", json={"input": {"text": "hi"}},
                    headers={"X-Episteme-Internal": "wrong"})
    assert r.status_code == 402


def test_internal_handoff_denied_when_secret_unset(client, monkeypatch):
    """Fail-closed: with no secret configured the bypass must not exist at all, so a misconfigured
    deploy charges for work instead of giving it away."""
    monkeypatch.delenv("EPISTEME_INTERNAL_SECRET", raising=False)
    r = client.post("/a2mcp/hash.compute", json={"input": {"text": "hi"}},
                    headers={"X-Episteme-Internal": ""})
    assert r.status_code == 402
