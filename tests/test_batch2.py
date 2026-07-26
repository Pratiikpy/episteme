"""Tests for batch-2 endpoints: redact_pii, email.validate, join, clean, diff, sitemap, api.to_mcp, mcp.validate."""
import json

import pytest

from contract import ArtifactRequest


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


def test_redact_pii(runtime):
    env = runtime.execute(_req("document.redact_pii",
                               text="Email jane@acme.com, phone 415-555-1234, ssn 123-45-6789."))
    assert env.ok
    assert env.result["found_pii"] is True
    assert env.result["pii_counts"].get("email") == 1
    assert "jane@acme.com" not in env.result["redacted_text"]
    assert "[EMAIL]" in env.result["redacted_text"]


def test_email_validate(runtime):
    # check_mx=False keeps this case offline and purely about syntax. It used to assert that
    # jane.doe@example.com was deliverable — but example.com publishes an RFC 7505 null MX and can
    # never receive mail, so the assertion was encoding the old syntax-only bug as expected
    # behaviour. See test_email_validate_null_mx_is_undeliverable for the real verdict.
    ok = runtime.execute(_req("email.validate", email="jane.doe@example.com", check_mx=False)).result
    assert ok["syntax_ok"] is True
    assert ok["verdict"] == "unknown", "no MX check was requested, so deliverability is not known"
    assert ok["mx_checked"] is False
    bad = runtime.execute(_req("email.validate", email="not-an-email", check_mx=False)).result
    assert bad["syntax_ok"] is False and bad["verdict"] == "undeliverable"
    disp = runtime.execute(_req("email.validate", email="x@mailinator.com", check_mx=False)).result
    assert disp["is_disposable"] is True and disp["risk"] == "high"


def test_email_validate_normalizes_gmail_aliases(runtime):
    """Gmail ignores dots and anything after '+', so these are one mailbox. Without normalization a
    deduplicated list still double-sends to the same person."""
    a = runtime.execute(_req("email.validate", email="Jane.Doe+news@Gmail.com", check_mx=False)).result
    b = runtime.execute(_req("email.validate", email="janedoe@gmail.com", check_mx=False)).result
    assert a["normalized"] == b["normalized"] == "janedoe@gmail.com"


def test_email_validate_flags_typo_domains(runtime):
    """Typosquatted provider domains resolve and accept mail, so MX alone calls them deliverable.
    The mail still reaches a stranger, which is why the typo itself has to be reported."""
    r = runtime.execute(_req("email.validate", email="user@gmial.com", check_mx=False)).result
    assert r["likely_typo_of"] == "gmail.com" and r["risk"] == "high"
    clean = runtime.execute(_req("email.validate", email="user@gmail.com", check_mx=False)).result
    assert clean["likely_typo_of"] is None


def test_email_validate_never_claims_an_uncheck_as_checked(runtime):
    """mx_checked must describe what actually happened. A confident verdict on no evidence is the
    failure that makes a validator worthless — the buyer cannot tell it apart from a real pass."""
    r = runtime.execute(_req("email.validate", email="a@b.com", check_mx=False)).result
    assert r["mx_checked"] is False and r["has_mx"] is None and r["verdict"] == "unknown"


def test_data_join_inner(runtime):
    env = runtime.execute(_req("data.join",
                               left=[{"id": 1, "a": "x"}, {"id": 2, "a": "y"}],
                               right=[{"id": 1, "b": "p"}], on="id", how="inner"))
    assert env.ok
    assert env.result["joined_rows"] == 1
    assert env.result["rows"][0]["b"] == "p"


def test_data_clean(runtime):
    env = runtime.execute(_req("data.clean", rows=[{"n": "  Bob   Smith "}], ops=["trim", "collapse_ws"]))
    assert env.ok
    assert env.result["rows"][0]["n"] == "Bob Smith"
    assert env.result["cells_changed"] == 1


def test_object_diff(runtime):
    env = runtime.execute(_req("object.diff", a={"x": 1, "y": 2}, b={"x": 1, "y": 3, "z": 4}))
    assert env.ok
    assert "$.z" in env.result["added"]
    assert any(c["path"] == "$.y" for c in env.result["changed"])
    assert env.result["identical"] is False


def test_site_map_sitemap(runtime):
    xml = "<urlset><url><loc>https://e.com/a</loc></url><url><loc>https://e.com/b</loc></url></urlset>"
    env = runtime.execute(_req("site.map", sitemap=xml))
    assert env.ok
    assert env.result["url_count"] == 2
    assert "https://e.com/a" in env.result["urls"]


def test_api_to_mcp(runtime):
    spec = {"openapi": "3.0.0", "paths": {"/u": {"get": {"operationId": "getU", "summary": "get user",
            "parameters": [{"name": "id", "in": "query", "schema": {"type": "string"}}]}}}}
    env = runtime.execute(_req("api.to_mcp", spec=spec))
    assert env.ok
    assert env.result["tool_count"] == 1
    t = env.result["tools"][0]
    assert t["name"] == "getU"
    assert t["inputSchema"]["properties"]["id"]["type"] == "string"


def test_mcp_validate(runtime):
    good = runtime.execute(_req("mcp.validate", tools=[
        {"name": "t1", "description": "d", "inputSchema": {"type": "object", "properties": {}}}])).result
    assert good["valid"] is True
    bad = runtime.execute(_req("mcp.validate", tools=[{"name": "t1"}, {"description": "x"}])).result
    assert bad["valid"] is False and bad["finding_count"] >= 2


def test_email_validate_null_mx_is_undeliverable(runtime):
    """example.com is reserved by IANA and publishes an RFC 7505 null MX ("."), meaning it refuses
    all mail. A syntax-only validator calls it deliverable; a real one must not. Skips rather than
    fails if the sandbox has no DNS, so the suite stays honest about what it actually verified."""
    r = runtime.execute(_req("email.validate", email="jane.doe@example.com")).result
    if not r["mx_checked"]:
        pytest.skip("no DNS resolver available in this environment")
    assert r["has_mx"] is False
    assert r["verdict"] == "undeliverable"
    assert r["deliverable_guess"] is False


def test_email_validate_real_domain_has_mx(runtime):
    r = runtime.execute(_req("email.validate", email="someone@gmail.com")).result
    if not r["mx_checked"]:
        pytest.skip("no DNS resolver available in this environment")
    assert r["has_mx"] is True and r["verdict"] == "deliverable"
    assert any("google" in h for h in r["mx_hosts"]), r["mx_hosts"]
