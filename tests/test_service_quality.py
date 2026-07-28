"""Regression tests for the nine services an end-to-end audit found were charging for weak output.

Each test names the specific defect it locks out. These are not shape assertions — every one of them
fails against the previous implementation, which is the only reason a test like this earns its place.
"""
import pytest

from contract import ArtifactRequest


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


# --------------------------------------------------------------- repo.scan_secrets
"""Fixtures are assembled from fragments rather than written as literals.

Every value below is a synthetic key that has never existed, but they deliberately match the exact
shape of real provider keys — that is the whole point of the test. Written as literals, they also match
the patterns GitHub's push protection scans for, and the push is rejected as if the repository leaked a
live Stripe key. Concatenating the prefix keeps the value under test byte-for-byte identical while no
scannable literal appears in the file.
"""
@pytest.mark.parametrize("secret,expected_rule", [
    # `sk-[A-Za-z0-9]{20,}` cannot match a hyphen, so every modern OpenAI/Anthropic key was missed.
    ("OPENAI_KEY=" + "sk-" + "proj-abc123XYZ_deadbeef-cafe0987654321zzTT", "openai-project-key"),
    ("ANTHROPIC=" + "sk-" + "ant-api03-Zx9_yQ7-abcdefgHIJKLmnop123456", "anthropic-key"),
    ("STRIPE=" + "sk_" + "live_" + "51H8xKvGhJkLmNoPqRsTuVwX", "stripe-live-secret"),
    ("GOOGLE=" + "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY", "gcp-api-key"),
    ("DATABASE_URL=postgresql://admin:S3cr3tP4ssw0rd@db.internal:5432/prod", "db-uri-with-password"),
    ("HF=" + "hf_" + "QWERTYuiopASDFGHjklZXCVBNmqwertyuio", "huggingface-token"),
    ("NPM=" + "npm_" + "abcdefghijklmnopqrstuvwxyz0123456789", "npm-token"),
    ("KEY=0x" + "3da03682e3ad141dc95f265526443b0a849886962e20ab71089289c46aa6ac3e", "hex-private-key"),
])
def test_scan_secrets_catches_real_provider_keys(runtime, secret, expected_rule):
    r = runtime.execute(_req("repo.scan_secrets", text=secret)).result
    assert r["has_secrets"] is True, f"missed: {secret[:30]}"
    assert expected_rule in r["findings_by_rule"], r["findings_by_rule"]


def test_scan_secrets_catches_bare_env_assignment(runtime):
    """The generic rule required the value to be QUOTED, so an entire .env file matched nothing."""
    r = runtime.execute(_req("repo.scan_secrets", text="MY_API_KEY=Zx91mQ7bV3nR8kL2wY6tE4uI0oP5aS")).result
    assert r["has_secrets"] is True


def test_scan_secrets_handles_screaming_snake_names(runtime):
    r"""A leading \b never matches inside DO_TOKEN because '_' is a word character — so the most
    common naming convention for credentials defeated the rule entirely."""
    r = runtime.execute(_req("repo.scan_secrets", text="DO_TOKEN=dop_v1_9f3e2a1b8c7d6e5f4a3b2c1d0e9f8a7b")).result
    assert r["has_secrets"] is True


def test_scan_secrets_reports_two_secrets_on_one_line(runtime):
    """.search() stopped at the first match per rule per line, so the second key was dropped."""
    line = "AKIAIOSFODNN7EXAMPLE and AKIAJ2VBQ3XKLMNOPQRS"
    r = runtime.execute(_req("repo.scan_secrets", text=line)).result
    assert r["findings_by_rule"].get("aws-access-key") == 2, r["findings"]


@pytest.mark.parametrize("benign", [
    "API_KEY=your-api-key-here", "PASSWORD=changeme", "TOKEN=xxxxxxxxxxxx",
    "DB_USER=postgres", "LOG_LEVEL=debug", "SECRET=<INSERT_VALUE>",
])
def test_scan_secrets_ignores_placeholders(runtime, benign):
    """False positives train a buyer to ignore the tool, which is its own kind of failure."""
    r = runtime.execute(_req("repo.scan_secrets", text=benign)).result
    assert r["has_secrets"] is False, f"false positive on {benign!r}: {r['findings']}"


def test_scan_secrets_never_returns_the_secret_itself(runtime):
    raw = "AKIAIOSFODNN7EXAMPLE"
    env = runtime.execute(_req("repo.scan_secrets", text=f"AWS={raw}"))
    assert raw not in env.model_dump_json()


# --------------------------------------------------------------- document.chunk
def test_chunk_actually_applies_requested_overlap(runtime):
    """Overlap was applied ONLY to a single oversized paragraph. On ordinary prose adjacent chunks
    overlapped by zero characters despite the caller requesting and paying for overlap."""
    text = "\n\n".join(f"Paragraph {i} " + "word " * 25 for i in range(6))
    r = runtime.execute(_req("document.chunk", text=text, chunk_size=300, overlap=80)).result
    assert r["chunk_count"] > 1
    assert r["min_overlap_applied"] > 0, r["overlap_applied_chars"]


def test_chunk_covers_whole_document_without_losing_text(runtime):
    text = "\n\n".join(f"Section {i}. " + "alpha beta gamma delta " * 8 for i in range(5))
    r = runtime.execute(_req("document.chunk", text=text, chunk_size=250, overlap=50)).result
    detailed = r["chunks_detailed"]
    assert detailed[0]["start"] == 0
    # Trailing whitespace is trimmed off the span, so the last offset lands on the last real
    # character rather than the end of the raw string.
    assert detailed[-1]["end"] == len(text.rstrip())
    for prev, cur in zip(detailed, detailed[1:]):
        gap = text[prev["end"]:cur["start"]]
        assert not gap.strip(), f"dropped content between chunks: {gap!r}"


def test_chunk_never_exceeds_requested_size(runtime):
    text = "lorem ipsum dolor sit amet " * 60
    r = runtime.execute(_req("document.chunk", text=text, chunk_size=180, overlap=40)).result
    assert r["max_chunk_chars"] <= 180
    assert all(len(c) <= 180 for c in r["chunks"])


def test_chunk_does_not_shatter_into_single_character_strides(runtime):
    """An overlap-shifted greedy packer advances one character at a time whenever a chunk comes out
    shorter than the overlap. Measured before the fix: 51 chunks for a 600-char document."""
    text = "\n\n".join(["Short para one.", "Another short paragraph here.",
                        "A third paragraph that is a little longer than the others but still small."])
    r = runtime.execute(_req("document.chunk", text=text, chunk_size=200, overlap=60)).result
    assert r["chunk_count"] <= 6, f"degenerate chunking: {r['chunk_count']} chunks"


# --------------------------------------------------------------- data.transform_json
def test_flatten_preserves_empty_containers(runtime):
    """Recursing into {} / [] emitted nothing, so the keys vanished and flatten became non-invertible."""
    data = {"user": {"name": "a", "tags": [], "meta": {}}, "items": [1, 2], "note": None}
    r = runtime.execute(_req("data.transform_json", data=data, op="flatten")).result
    assert r["result"]["user.tags"] == []
    assert r["result"]["user.meta"] == {}


def test_flatten_is_lossless_and_says_so(runtime):
    data = {"a": {"b": {"c": [{"d": 1}, {"d": 2}]}}, "empty": [], "x": [[], [{}], [1, [2, [3]]]]}
    env = runtime.execute(_req("data.transform_json", data=data, op="flatten"))
    check = next(c for c in env.validation.tests if c.name == "flatten_is_lossless")
    assert check.passed


# --------------------------------------------------------------- chart.spec
def test_arc_mark_uses_theta_not_xy(runtime):
    """Vega-Lite draws `arc` from theta+color. Handing it x/y produced a schema-valid spec that
    rendered a completely blank chart — a paid pie chart that shows nothing."""
    rows = [{"segment": "Retail", "share": 45}, {"segment": "Online", "share": 25}]
    r = runtime.execute(_req("chart.spec", rows=rows, x="segment", y="share", mark="arc")).result
    enc = r["spec"]["encoding"]
    assert "theta" in enc and "color" in enc
    assert "x" not in enc and "y" not in enc


def test_temporal_x_axis_is_not_typed_nominal(runtime):
    """A date axis typed `nominal` sorts alphabetically, so 2024-11 precedes 2024-2 and the trend
    line is drawn through the wrong points — while still rendering, which is why it went unnoticed."""
    rows = [{"date": "2024-02-01", "revenue": 1200.5}, {"date": "2024-11-01", "revenue": 3400.25}]
    r = runtime.execute(_req("chart.spec", rows=rows, x="date", y="revenue", mark="line")).result
    assert r["x_type"] == "temporal"
    assert r["spec"]["encoding"]["x"]["sort"] == "ascending"


def test_numeric_measure_stays_quantitative(runtime):
    rows = [{"temp": 12.4, "sales": 100}, {"temp": 22.1, "sales": 240}, {"temp": 31.9, "sales": 390}]
    r = runtime.execute(_req("chart.spec", rows=rows, x="temp", y="sales", mark="point")).result
    assert r["x_type"] == "quantitative" and r["y_type"] == "quantitative"


def test_chart_rejects_field_absent_from_data(runtime):
    """A typo'd field name produced a valid spec that rendered an empty panel with no error."""
    rows = [{"date": "2024-01-01", "revenue": 5}]
    env = runtime.execute(_req("chart.spec", rows=rows, x="daet", y="revenue", mark="line"))
    assert not env.ok
    assert "daet" in (env.error.message if env.error else "")


# --------------------------------------------------------------- email.validate
def test_email_verdict_requires_evidence(runtime):
    """deliverable_guess was "the regex matched", so every non-existent domain passed."""
    r = runtime.execute(_req("email.validate", email="a@b.com", check_mx=False)).result
    assert r["verdict"] == "unknown" and r["mx_checked"] is False


def test_email_flags_typosquatted_provider(runtime):
    r = runtime.execute(_req("email.validate", email="user@gmial.com", check_mx=False)).result
    assert r["likely_typo_of"] == "gmail.com"


def test_email_disposable_list_is_not_five_entries(runtime):
    r = runtime.execute(_req("email.validate", email="x@mailinator.com", check_mx=False)).result
    assert r["disposable_domains_known"] >= 40


# --------------------------------------------------------------- api.to_mcp
def _spec_with_body():
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/pets/{petId}": {
                "parameters": [{"name": "petId", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "get": {"operationId": "getPet", "summary": "Fetch one pet"},
            },
            "/pets": {
                "post": {"operationId": "createPet", "summary": "Create a pet",
                         "requestBody": {"required": True, "content": {"application/json": {
                             "schema": {"$ref": "#/components/schemas/Pet"}}}}},
                "get": {"operationId": "listPets", "summary": "List pets",
                        "parameters": [{"$ref": "#/components/parameters/Limit"}]},
            },
        },
        "components": {
            "parameters": {"Limit": {"name": "limit", "in": "query", "schema": {"type": "integer"}}},
            "schemas": {
                "Pet": {"type": "object", "required": ["name"], "properties": {
                    "name": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "owner": {"$ref": "#/components/schemas/Owner"}}},
                "Owner": {"type": "object", "properties": {"email": {"type": "string"}}},
            },
        },
    }


def test_api_to_mcp_captures_request_body(runtime):
    """requestBody was ignored outright, so every generated POST/PUT tool had no way to send data —
    calling one would hit the API with an empty body and get a 400 every time."""
    r = runtime.execute(_req("api.to_mcp", spec=_spec_with_body())).result
    create = next(t for t in r["tools"] if t["name"] == "createPet")
    assert "name" in create["inputSchema"]["properties"]
    assert "name" in create["inputSchema"]["required"]
    assert create["_has_body"] is True


def test_api_to_mcp_resolves_refs(runtime):
    """Real specs are almost entirely $ref. Unresolved, the tool got an empty argument list."""
    r = runtime.execute(_req("api.to_mcp", spec=_spec_with_body())).result
    assert r["refs_resolved"] > 0 and r["unresolved_refs"] == []
    create = next(t for t in r["tools"] if t["name"] == "createPet")
    assert create["inputSchema"]["properties"]["owner"]["properties"]["email"]["type"] == "string"
    assert create["inputSchema"]["properties"]["tags"]["items"]["type"] == "string"


def test_api_to_mcp_uses_path_level_parameters(runtime):
    """Path-item parameters are shared by every method beneath them, and are where path params are
    usually declared — without them the URL cannot even be constructed."""
    r = runtime.execute(_req("api.to_mcp", spec=_spec_with_body())).result
    get_pet = next(t for t in r["tools"] if t["name"] == "getPet")
    assert "petId" in get_pet["inputSchema"]["properties"]
    assert get_pet["_param_location"]["petId"] == "path"


def test_api_to_mcp_resolves_referenced_parameters(runtime):
    r = runtime.execute(_req("api.to_mcp", spec=_spec_with_body())).result
    lst = next(t for t in r["tools"] if t["name"] == "listPets")
    assert "limit" in lst["inputSchema"]["properties"]


def test_api_to_mcp_reports_server_url(runtime):
    r = runtime.execute(_req("api.to_mcp", spec=_spec_with_body())).result
    assert r["base_url"] == "https://api.example.com/v1"


def test_api_to_mcp_never_emits_duplicate_tool_names(runtime):
    spec = {"openapi": "3.0.0", "paths": {
        "/a": {"get": {"operationId": "same"}}, "/b": {"get": {"operationId": "same"}}}}
    r = runtime.execute(_req("api.to_mcp", spec=spec)).result
    names = [t["name"] for t in r["tools"]]
    assert len(set(names)) == len(names), names


# --------------------------------------------------------------- mcp.validate
def test_mcp_validate_catches_required_naming_unknown_property(runtime):
    """No argument set can satisfy such a schema, so every call fails validation."""
    tools = [{"name": "t", "description": "a real description here",
              "inputSchema": {"type": "object", "properties": {"id": {"type": "string",
                              "description": "d"}}, "required": ["userId"]}}]
    r = runtime.execute(_req("mcp.validate", tools=tools)).result
    assert r["valid"] is False
    assert "required-unknown-property" in r["findings_by_rule"]


@pytest.mark.parametrize("tool,rule", [
    ({"name": "bad name", "description": "a real description here",
      "inputSchema": {"type": "object", "properties": {}}}, "name-invalid-chars"),
    ({"name": "t", "description": "a real description here",
      "inputSchema": {"type": "object", "properties": {"a": {"type": "array", "description": "d"}}}},
     "array-without-items"),
    ({"name": "t", "description": "a real description here",
      "inputSchema": {"type": "object", "properties": {"a": {"type": "strng", "description": "d"}}}},
     "property-bad-type"),
    ({"name": "t", "description": "a real description here",
      "inputSchema": {"type": "object", "properties": {"a": {"type": "integer", "default": "five",
                                                             "description": "d"}}}},
     "default-type-mismatch"),
    ({"name": "t", "description": "a real description here",
      "inputSchema": {"type": "object", "properties": {"a": {"$ref": "#/x"}}}}, "schema-contains-ref"),
])
def test_mcp_validate_rule_coverage(runtime, tool, rule):
    r = runtime.execute(_req("mcp.validate", tools=[tool])).result
    assert rule in r["findings_by_rule"], r["findings_by_rule"]


def test_mcp_validate_passes_a_well_formed_server(runtime):
    """A linter that flags everything is as useless as one that flags nothing."""
    tools = [{"name": "search_web", "description": "Search the open web and return ranked results.",
              "inputSchema": {"type": "object", "properties": {
                  "query": {"type": "string", "description": "The search query"}},
                  "required": ["query"]}}]
    r = runtime.execute(_req("mcp.validate", tools=tools)).result
    assert r["valid"] is True and r["finding_count"] == 0


def test_mcp_validate_separates_errors_from_advice(runtime):
    tools = [{"name": "t", "description": "short",
              "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}}}]
    r = runtime.execute(_req("mcp.validate", tools=tools)).result
    assert r["error_count"] == 0 and r["warning_count"] > 0
    assert r["valid"] is True, "warnings must not fail an otherwise usable server"


# --------------------------------------------------------------- url.to_markdown
_CHROME_PAGE = """<html><head><title>Consensus</title><style>.x{color:red}</style></head><body>
<header id="site-header"><a href="/">Home</a></header>
<nav class="navbar"><ul><li><a href="/a">Docs</a></li></ul></nav>
<div class="cookie-consent">We use cookies.</div>
<main>
<h1>Consensus</h1>
<p>A <strong>mechanism</strong>. See the <a href="https://bitcoin.org/bitcoin.pdf">paper</a>.</p>
<table><tr><th>Type</th><th>Energy</th></tr><tr><td>PoW</td><td>High</td></tr></table>
<pre><code>def verify(b):
    return True</code></pre>
</main>
<aside class="related"><a href="/x">Related</a></aside>
<footer>(c) 2026 <a href="/privacy">Privacy</a></footer></body></html>"""


def test_markdown_strips_navigation_chrome(runtime):
    """A page came back with its entire sidebar, header and footer inlined — the buyer paid for
    markdown and still had to clean it."""
    r = runtime.execute(_req("url.to_markdown", html=_CHROME_PAGE)).result
    md = r["markdown"]
    assert "cookies" not in md and "Privacy" not in md and "Related" not in md
    assert r["boilerplate_blocks_removed"] >= 5


def test_markdown_preserves_link_targets(runtime):
    """Bare text loses every href — the page's citation graph, and the one thing a caller cannot
    reconstruct from the body."""
    r = runtime.execute(_req("url.to_markdown", html=_CHROME_PAGE)).result
    assert "[paper](https://bitcoin.org/bitcoin.pdf)" in r["markdown"]


def test_markdown_keeps_table_structure(runtime):
    r = runtime.execute(_req("url.to_markdown", html=_CHROME_PAGE)).result
    assert "| Type | Energy |" in r["markdown"]
    assert "| PoW | High |" in r["markdown"]


def test_markdown_keeps_code_indentation(runtime):
    """Whitespace-collapsing a code block destroys the only thing that made it code."""
    r = runtime.execute(_req("url.to_markdown", html=_CHROME_PAGE)).result
    assert "```" in r["markdown"]
    assert "    return True" in r["markdown"]


def test_markdown_still_captures_title_outside_content_root(runtime):
    r = runtime.execute(_req("url.to_markdown", html=_CHROME_PAGE)).result
    assert r["title"] == "Consensus"


# --------------------------------------------------------------- receipt attribution
def test_receipt_verify_rejects_a_forgery_signed_with_another_key(runtime):
    """The most dangerous case: an attacker tampers with the result, recomputes the manifest, and
    re-signs with their OWN valid Ed25519 keypair. Every cryptographic check passes — only the
    identity is wrong. The previous implementation returned verdict "VALID" for exactly this, which
    means Episteme would have certified an attacker's forgery as its own work."""
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from runtime import sha256_hex

    genuine = json.loads(runtime.execute(_req("hash.compute", text="attribution")).model_dump_json())
    attacker = Ed25519PrivateKey.generate()
    forged = json.loads(json.dumps(genuine))
    forged["result"] = {"sha256": "0" * 64}
    # A competent forger recomputes the digest of what they substituted. The earlier version of this
    # fixture did not, which made it a *content* forgery rather than an identity one — and once
    # receipt.verify started re-hashing the result, it was caught on those grounds and never reached
    # the attribution check this test exists to exercise. Recomputing here leaves exactly one thing
    # wrong with the envelope: who signed it.
    forged["output_hash"] = sha256_hex(forged["result"])
    manifest = sha256_hex({
        "endpoint": forged["endpoint"], "input_hashes": forged["input_hashes"],
        "output_hash": forged["output_hash"], "tool": forged["tool"],
        "level": forged["validation"]["level"], "job_id": forged["job_id"],
    })
    forged["receipt"] = {
        "manifest_sha256": manifest, "algo": "ed25519",
        "signature": attacker.sign(manifest.encode()).hex(),
        "public_key": attacker.public_key().public_bytes_raw().hex(),
        "signed_at": "2026-01-01T00:00:00Z",
    }
    r = runtime.execute(_req("receipt.verify", envelope=forged,
                             expected_public_key=runtime.signer.public_hex)).result
    assert r["signature_valid"] is True, "the forgery is cryptographically sound by construction"
    assert r["attributed"] is False
    assert r["verdict"] == "VALID_SIGNATURE_UNKNOWN_ISSUER"


def test_receipt_verify_accepts_a_genuine_receipt(runtime):
    import json
    env = json.loads(runtime.execute(_req("hash.compute", text="genuine")).model_dump_json())
    r = runtime.execute(_req("receipt.verify", envelope=env,
                             expected_public_key=runtime.signer.public_hex)).result
    assert r["verdict"] == "VALID" and r["attributed"] is True


def test_receipt_verify_detects_tampering(runtime):
    import json
    env = json.loads(runtime.execute(_req("hash.compute", text="tamper")).model_dump_json())
    env["output_hash"] = "deadbeef"
    r = runtime.execute(_req("receipt.verify", envelope=env)).result
    assert r["manifest_matches"] is False and r["verdict"] == "INVALID_OR_TAMPERED"


def test_receipt_verify_will_not_claim_valid_without_attribution(runtime):
    """With no expected key and no configured service key there is nothing to attribute against, so
    the honest answer is VALID_UNATTRIBUTED — never a bare VALID."""
    import json
    env = json.loads(runtime.execute(_req("hash.compute", text="unattributed")).model_dump_json())
    r = runtime.execute(_req("receipt.verify", envelope=env)).result
    assert r["verdict"] in {"VALID", "VALID_UNATTRIBUTED"}
    if r["attributed"] is not True:
        assert r["verdict"] == "VALID_UNATTRIBUTED"


def test_email_unavailable_resolver_is_unknown_not_undeliverable(runtime, monkeypatch):
    """Collapsing "could not check" into "no MX" is a false negative with real consequences: it
    shipped, and with dnspython missing from the container gmail.com came back UNDELIVERABLE — which
    would have someone delete a perfectly good address from their list. Inability to check is not
    evidence of absence."""
    import builtins

    from nodes import more2_nodes

    # An earlier test in this process may have cached a real answer for the domain, and the cache is
    # consulted before the resolver is even imported — so without clearing it this test would pass
    # for the wrong reason.
    monkeypatch.setattr(more2_nodes, "_MX_CACHE", {})

    real_import = builtins.__import__

    def no_dns(name, *args, **kwargs):
        if name.startswith("dns"):
            raise ImportError("simulated: dnspython not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_dns)
    r = runtime.execute(_req("email.validate", email="someone@gmail.com")).result
    assert r["has_mx"] is None
    assert r["verdict"] == "unknown", "a missing resolver must never produce 'undeliverable'"
    assert r["mx_checked"] is False


def test_email_nxdomain_is_real_evidence_of_undeliverable(runtime):
    """A domain that does not exist IS evidence — it must not be softened to 'unknown'."""
    r = runtime.execute(_req("email.validate",
                             email="user@definitely-not-a-real-domain-xyz123456.invalid")).result
    if r["mx_note"] and r["mx_note"].startswith("lookup_failed"):
        pytest.skip("no DNS available to distinguish NXDOMAIN from a transport failure")
    assert r["has_mx"] is False and r["verdict"] == "undeliverable"


# --------------------------------------------------------------- found by exercising every service
def test_data_diff_accepts_rows_not_only_csv(runtime):
    """`str(list_of_dicts)` fed to a CSV parser SUCCEEDS as a zero-row table, so passing rows — which
    every sibling node accepts — reported added=0/removed=0, i.e. "no differences", for datasets that
    plainly differ. ok=True, money taken, no warning anywhere."""
    a = [{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]
    b = [{"id": 1, "amount": 100}, {"id": 2, "amount": 999}]
    r = runtime.execute(_req("data.diff", a=a, b=b)).result
    assert r["rows_a"] == 2 and r["rows_b"] == 2
    assert r["modified_rows"] == 1, r
    assert r["modified"][0]["changes"]["amount"] == {"from": 200, "to": 999}


def test_data_diff_still_accepts_csv(runtime):
    r = runtime.execute(_req("data.diff", a="id,v\n1,10\n2,20", b="id,v\n1,10\n2,99")).result
    assert r["rows_a"] == 2 and r["modified_rows"] == 1


def test_data_diff_refuses_to_guess_at_a_bad_type(runtime):
    env = runtime.execute(_req("data.diff", a={"not": "a list"}, b=[]))
    assert not env.ok and "CSV string or a list" in env.error.message


def test_robots_check_answers_about_the_url_it_was_given(runtime):
    """`url` was ignored entirely and the node fell back to path "/", which is almost always allowed —
    so a disallowed URL came back allowed. For a compliance tool that is the dangerous direction."""
    robots = "User-agent: *\nDisallow: /admin\nAllow: /"
    denied = runtime.execute(_req("robots.check", robots=robots, url="https://e.com/admin/secret")).result
    assert denied["path"] == "/admin/secret" and denied["allowed"] is False
    ok = runtime.execute(_req("robots.check", robots=robots, url="https://e.com/public/page")).result
    assert ok["allowed"] is True


def test_robots_check_refuses_to_guess_the_path(runtime):
    env = runtime.execute(_req("robots.check", robots="User-agent: *\nDisallow: /admin"))
    assert not env.ok, "defaulting to '/' would report a false 'allowed'"


def test_data_pivot_accepts_the_shorthand_and_never_leaks_an_internal_error(runtime):
    """`agg="sum"` reached `agg.items()` and escaped as a raw AttributeError under ENGINE_FAILED —
    an internal Python error handed to a paying caller with nothing to act on."""
    rows = [{"region": "EMEA", "amount": 100}, {"region": "EMEA", "amount": 25},
            {"region": "APAC", "amount": 7}]
    r = runtime.execute(_req("data.pivot", rows=rows, group_by=["region"], agg="sum", value="amount")).result
    by = {x["region"]: x["amount_sum"] for x in r["rows"]}
    assert by == {"EMEA": 125, "APAC": 7}


@pytest.mark.parametrize("bad,expect", [
    ({"group_by": ["region"], "agg": "sum"}, "needs a column"),
    ({"group_by": ["nope"], "agg": {"amount": "sum"}}, "not present"),
    ({"group_by": ["region"], "agg": 7}, "must be either"),
])
def test_data_pivot_reports_actionable_input_errors(runtime, bad, expect):
    rows = [{"region": "EMEA", "amount": 100}]
    env = runtime.execute(_req("data.pivot", rows=rows, **bad))
    assert not env.ok
    assert env.error.code.name == "INVALID_INPUT", env.error.message
    assert expect in env.error.message


def _five_page_pdf() -> str:
    import base64 as b64
    import io as _io

    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(5):
        w.add_blank_page(width=200, height=200)
    buf = _io.BytesIO()
    w.write(buf)
    return b64.b64encode(buf.getvalue()).decode()


@pytest.mark.parametrize("spec,expected", [
    ("1-2", 2), ("1,3,5", 3), ("2-", 4), ("3", 1), ([1, 4], 2),
])
def test_pdf_extract_pages_understands_ranges(runtime, spec, expected):
    """A range is how anyone says which pages they want, and it was not supported: the old code
    iterated the STRING "1-2" and called int('-'), crashing with a raw ValueError."""
    r = runtime.execute(_req("pdf.manipulate", content_b64=_five_page_pdf(),
                             op="extract_pages", pages=spec)).result
    assert r["out_pages"] == expected


def test_pdf_extract_pages_rejects_out_of_range_instead_of_returning_nothing(runtime):
    """Out-of-range pages were skipped silently, so this returned an EMPTY pdf on a paid call."""
    env = runtime.execute(_req("pdf.manipulate", content_b64=_five_page_pdf(),
                               op="extract_pages", pages="5-7"))
    assert not env.ok and "out of range" in env.error.message


def test_every_priced_service_advertises_how_to_send_its_input(runtime):
    """csv.profile, data.stats, repo.map and repo.lint advertised an EMPTY input schema, because their
    data arrives through a helper the AST walker cannot see. A buyer had no way to discover what to
    send for the thing they were paying to have processed."""
    from schemas import schema_for

    empty = []
    for info in runtime.registry.list():
        if info["price_usdt"] <= 0:
            continue
        node = runtime.registry.get(info["endpoint"])
        if not (schema_for(node) or {}).get("properties"):
            empty.append(info["endpoint"])
    assert not empty, f"these priced services document no inputs at all: {empty}"


def test_advertised_parameter_names_are_the_ones_the_code_reads(runtime):
    """The openapi.diff overlay named `a`/`b` while the node reads `old`/`new` — anything built from
    the advertised schema would have been rejected as INVALID_INPUT."""
    from schemas import schema_for

    node = runtime.registry.get("openapi.diff")
    props = set((schema_for(node) or {}).get("properties", {}))
    assert {"old", "new"} <= props and not ({"a", "b"} & props)


def test_no_service_demands_mutually_exclusive_parameters(runtime):
    """data.pivot required rows, group_by, agg, value, values AND field — but `agg` defaults to a
    count and value/values/field are three names for one optional argument. A caller obeying that
    schema would send nonsense. Any node listing two alternatives as both required is the same bug."""
    from schemas import schema_for

    alternatives = [("value", "values", "field"), ("text", "content_b64"),
                    ("rows", "csv", "json", "jsonl"), ("html", "url"), ("url", "path")]
    offenders = []
    for info in runtime.registry.list():
        if info["price_usdt"] <= 0:
            continue
        required = set((schema_for(runtime.registry.get(info["endpoint"])) or {}).get("required", []))
        for group in alternatives:
            if len(required & set(group)) > 1:
                offenders.append((info["endpoint"], sorted(required & set(group))))
    assert not offenders, f"these require alternatives that cannot all be supplied: {offenders}"


def test_an_overlay_overrides_a_wrong_derived_type(runtime):
    """`_KNOWN` typed data.pivot's `value` as a number from its name alone, but it is the NAME of the
    column to aggregate. An overlay must win over a derived guess, not defer to it."""
    from schemas import schema_for

    schema = schema_for(runtime.registry.get("data.pivot"))
    assert schema["properties"]["value"]["type"] == "string"


def test_schema_is_discoverable_over_http():
    """OKX's service format has no field for an input schema, so this endpoint is the only
    machine-readable statement of what to send. Without it a caller must infer the contract from
    prose — and several nodes take their input through a helper whose parameter names appear nowhere
    in the endpoint name."""
    from fastapi.testclient import TestClient

    from gateway import create_app

    client = TestClient(create_app())
    body = client.get("/nodes").json()
    documented = [n for n in body["nodes"] if (n.get("inputSchema") or {}).get("properties")]
    assert len(documented) >= 45, f"only {len(documented)} nodes publish a schema"

    one = client.get("/nodes/csv.profile/schema").json()
    assert "csv" in one["inputSchema"]["properties"]
    assert one["fee_usdt"] == 0.01
    assert client.get("/nodes/does.not.exist/schema").status_code == 404
    # Internal-only differential engines are not callable, so they must not be advertised.
    assert client.get("/nodes/csv.profile.alt/schema").status_code == 404


def test_absence_of_data_is_never_reported_as_a_clean_result(runtime):
    """The defect this locks out, stated generally: a check that could not run must never contribute
    to a positive verdict.

    Episteme's email.validate mapped "resolver unavailable" to has_mx=False and called gmail.com
    UNDELIVERABLE. Aletheia's wallet-health read risk fields off failed sub-checks, so with all three
    data sources down every condition was false and it returned a signed "HEALTHY — clean footprint"
    for a wallet it knew nothing about. Same root cause, opposite direction, both dangerous.
    """
    from nodes import more2_nodes

    saved = dict(more2_nodes._MX_CACHE)
    more2_nodes._MX_CACHE.clear()
    try:
        import builtins
        real = builtins.__import__

        def no_dns(name, *a, **k):
            if name.startswith("dns"):
                raise ImportError("no resolver")
            return real(name, *a, **k)

        builtins.__import__ = no_dns
        try:
            r = runtime.execute(_req("email.validate", email="someone@gmail.com")).result
        finally:
            builtins.__import__ = real
        assert r["verdict"] != "deliverable", "claimed a positive result with no evidence"
        assert r["verdict"] != "undeliverable", "claimed a negative result with no evidence"
        assert r["verdict"] == "unknown"
    finally:
        more2_nodes._MX_CACHE.clear()
        more2_nodes._MX_CACHE.update(saved)


# ------------------------------------------- learned from a LISTED ASP's review-compliance history
def _paid_client():
    """A client whose requests look like they have already been paid for, via the internal hand-off."""
    import os

    from fastapi.testclient import TestClient

    from gateway import create_app
    os.environ["EPISTEME_INTERNAL_SECRET"] = "test-internal-secret"
    return TestClient(create_app()), {"X-Episteme-Internal": "test-internal-secret"}


def test_paid_request_with_no_body_returns_the_contract_not_an_error():
    """OKX's availability probe sends an empty body, and payment settles BEFORE the handler runs — so a
    422 here charges the caller and hands them nothing.

    ShieldSuite, a listed ASP, was made to change exactly this during its OKX review (commit eff7c6d,
    "update x402 middleware logic to comply with okx review"): they moved the paywall to app.use so it
    covered every method, and stopped rejecting a paid request whose body was absent.
    """
    client, headers = _paid_client()
    r = client.post("/a2mcp/csv.profile", headers=headers)
    assert r.status_code == 200, "an empty paid request must not be answered with a 4xx"
    body = r.json()
    assert body["status"] == "no_input_supplied"
    # It must not be mistakable for a result.
    assert body["ok"] is False and "result" not in body
    assert body["inputSchema"]["properties"], "the reply must carry the contract"
    assert "csv" in body["one_of"]


def test_no_input_reply_shows_a_request_that_would_actually_work():
    """An example naming two mutually-exclusive alternatives tells the caller to send both."""
    client, headers = _paid_client()
    for endpoint, alts in [("csv.profile", {"csv", "text", "content_b64"}),
                           ("data.stats", {"rows", "json", "jsonl", "csv"})]:
        example = client.post(f"/a2mcp/{endpoint}", headers=headers).json()["example_request"]["input"]
        assert len(alts & set(example)) == 1, f"{endpoint} example has {alts & set(example)}"


def test_a_present_but_invalid_body_still_gets_its_specific_diagnostic():
    """The empty-body path must not swallow real caller errors — a wrong input is still a 422 naming
    what was wrong, which is the useful answer for someone who did send something."""
    client, headers = _paid_client()
    r = client.post("/a2mcp/csv.profile", json={"input": {"nonsense": 1}}, headers=headers)
    assert r.status_code == 422
    assert "csv" in r.json()["error"]["message"]


def test_valid_input_is_unaffected():
    client, headers = _paid_client()
    r = client.post("/a2mcp/csv.profile", json={"input": {"csv": "a,b\n1,2\n3,4"}}, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["result"]["rows"] == 2


def test_every_priced_endpoint_answers_an_empty_paid_request_with_200():
    """Swept across all of them, because OKX probes whichever it likes."""
    client, headers = _paid_client()
    from nodes import build_registry
    bad = []
    for info in build_registry().list():
        if info["price_usdt"] <= 0:
            continue
        r = client.post(f"/a2mcp/{info['endpoint']}", headers=headers)
        if r.status_code != 200 or r.json().get("status") != "no_input_supplied":
            bad.append((info["endpoint"], r.status_code))
    assert not bad, f"these answered an empty paid request with an error: {bad}"


# ------------------------------- the three BLOCKING findings from the advertised-vs-actual audit
def test_workflow_does_not_certify_a_graph_whose_steps_failed(runtime):
    """A failed step is still SIGNED — the receipt attests "this is what happened", and what happened
    was an error. So `all_steps_signed` passed with every step broken, and the buyer got HTTP 200,
    validation.status="validated", L2_VALIDATED and a signature over a graph that produced nothing.
    A signed receipt asserting success over failed work is worse than no receipt: it lends the failure
    authority."""
    env = runtime.execute(_req("workflow.compose", steps=[
        {"endpoint": "hash.compute", "input": {"text": "ok"}},
        {"endpoint": "csv.profile", "input": {"nonsense": 1}},
    ]))
    assert env.result["all_ok"] is False
    check = next(c for c in env.validation.tests if c.name == "all_steps_succeeded")
    assert not check.passed and "csv.profile" in (check.detail or "")
    assert env.validation.status == "quality_failed"
    assert env.validation.level.value.startswith("L1"), "a broken graph must not reach a verified level"


def test_workflow_still_validates_when_every_step_works(runtime):
    env = runtime.execute(_req("workflow.compose", steps=[
        {"endpoint": "hash.compute", "input": {"text": "ok"}},
        {"endpoint": "text.stats", "input": {"text": "hello world"}},
    ]))
    assert env.result["all_ok"] is True
    assert all(c.passed for c in env.validation.tests)
    assert env.validation.status == "validated"


@pytest.mark.parametrize("table", ["t", "data", "csv", "rows", "dataset", "tbl", "input"])
def test_query_sql_accepts_any_reasonable_table_name(runtime, table):
    """The dataset was registered under one hard-coded relation name `t`, and nothing in the listing,
    the description or the published schema said so — so no working query could be constructed from
    anything a buyer can see."""
    r = runtime.execute(_req("data.query_sql", csv="region,amount\nEMEA,10\nAPAC,20",
                             sql=f"SELECT COUNT(*) AS n FROM {table}")).result
    assert r["rows"][0]["n"] == 2


def test_query_sql_accepts_a_caller_named_table(runtime):
    r = runtime.execute(_req("data.query_sql", csv="a,b\n1,2", sql="SELECT * FROM sales",
                             table="sales")).result
    assert r["row_count"] == 1


def test_query_sql_names_the_alternatives_when_the_table_is_wrong(runtime):
    env = runtime.execute(_req("data.query_sql", csv="a,b\n1,2", sql="SELECT * FROM nope"))
    assert not env.ok
    msg = env.error.message
    assert "available as any of" in msg and "Columns: a, b" in msg, msg


def test_language_model_dependency_is_disclosed(runtime):
    """Four services depend on a third-party model and none of the registered descriptions says so.
    A buyer paying for "summarizes long text" cannot otherwise know the result is non-reproducible and
    can fail with ENGINE_UNAVAILABLE. The registered copy needs an `agent update` to change, so the
    disclosure has to appear everywhere a caller can reach without one."""
    from schemas import schema_for

    for endpoint in ("text.summarize", "document.extract_json", "sim.run"):
        schema = schema_for(runtime.registry.get(endpoint))
        assert schema.get("x-ai-backed") is True, endpoint
        assert "language model" in schema.get("x-ai-disclosure", "")
    # ...and must NOT appear on the deterministic ones, or it means nothing.
    assert "x-ai-backed" not in schema_for(runtime.registry.get("csv.profile"))


def test_a_paying_caller_never_receives_a_raw_python_exception():
    """`AttributeError: 'str' object has no attribute 'get'` is accurate and useless.

    The runtime's catch-all returned `f"{type(e).__name__}: {e}"` verbatim, so a buyer who sent a
    string where a list belonged was handed the interpreter's own words and no idea what to send
    instead. Image nodes were worse: `not an image: cannot identify image file <_io.BytesIO object
    at 0x74f3cc339800>` shipped a memory address to a customer.
    """
    import io

    from runtime import humanise_error

    msg = humanise_error(AttributeError("'str' object has no attribute 'get'"))
    assert "check the field types" in msg
    assert "'str' object" not in msg
    assert "[AttributeError]" in msg, "the class name still helps whoever files the bug report"

    # No memory address survives, wherever it appears in the text.
    leaky = Exception(f"not an image: cannot identify image file {io.BytesIO()!r}")
    out = humanise_error(leaky)
    assert "0x" not in out
    assert "BytesIO object at" not in out

    # An already-readable message is passed through rather than replaced with a generic one.
    assert "provide 'rows' (list of objects)" in humanise_error(
        RuntimeError("provide 'rows' (list of objects)"))
