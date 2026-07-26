"""Final batch tests + the hero multi-node verifiable artifact graph."""
from contract import ArtifactRequest


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


def test_data_pivot(runtime):
    rows = [{"cat": "x", "v": 1}, {"cat": "x", "v": 3}, {"cat": "y", "v": 10}]
    env = runtime.execute(_req("data.pivot", rows=rows, group_by="cat", agg={"v": "sum"}))
    assert env.ok
    assert env.result["group_count"] == 2
    by = {r["cat"]: r for r in env.result["rows"]}
    assert by["x"]["v_sum"] == 4.0
    assert by["y"]["v_sum"] == 10.0


def test_data_pivot_count(runtime):
    rows = [{"c": "a"}, {"c": "a"}, {"c": "b"}]
    env = runtime.execute(_req("data.pivot", rows=rows, group_by="c"))
    assert env.ok
    by = {r["c"]: r["count"] for r in env.result["rows"]}
    assert by == {"a": 2, "b": 1}


def test_robots_check(runtime):
    robots = "User-agent: *\nDisallow: /private\nSitemap: https://e.com/sitemap.xml"
    allowed = runtime.execute(_req("robots.check", robots=robots, path="/public")).result
    assert allowed["allowed"] is True
    assert "https://e.com/sitemap.xml" in allowed["sitemaps"]
    blocked = runtime.execute(_req("robots.check", robots=robots, path="/private/x")).result
    assert blocked["allowed"] is False


def test_hero_multi_node_artifact_graph(runtime):
    """Flagship: one instruction -> a verified artifact graph of several nodes,
    with data flowing between steps and every node independently signed."""
    steps = [
        {"id": "md", "endpoint": "document.to_markdown",
         "input": {"text": "Contact jane@acme.com or 415-555-1234 about the launch."}},
        {"id": "redact", "endpoint": "document.redact_pii",
         "input": {"text": "$md.markdown"}},
        {"id": "stats", "endpoint": "text.stats",
         "input": {"text": "$redact.redacted_text"}},
        {"id": "hash", "endpoint": "hash.compute",
         "input": {"text": "$redact.redacted_text"}},
    ]
    env = runtime.execute(_req("workflow.compose", steps=steps))
    assert env.ok
    assert env.result["step_count"] == 4
    assert env.result["all_ok"] is True
    # data flowed md -> redact (PII removed) -> stats/hash
    assert all(s["receipt_signed"] for s in env.result["steps"])
    # dependency edges captured through the graph
    edge_pairs = {(e["from"], e["to"]) for e in env.result["edges"]}
    assert ("md", "redact") in edge_pairs
    assert ("redact", "stats") in edge_pairs
    assert ("redact", "hash") in edge_pairs
    # final node (hash) produced a digest over the redacted text
    assert env.result["final_result"]["digests"]["sha256"]
    # the whole graph is itself signed
    assert runtime.verify_receipt(env)
