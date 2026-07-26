"""Tests for code pack (repo.map/lint/scan_secrets) and workflow.compose."""
from contract import ArtifactRequest


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


def test_repo_map(runtime):
    files = {
        "app.py": "import os\nimport sys\ndef main():\n    return 1\nclass App:\n    pass\n",
        "util.js": "function helper(){return 2}\n",
        "README.md": "# hi\n",
    }
    env = runtime.execute(_req("repo.map", files=files))
    assert env.ok
    assert env.result["file_count"] == 3
    assert env.result["languages"]["Python"]["files"] == 1
    assert env.result["symbols"]["functions"] >= 2
    assert env.result["symbols"]["classes"] >= 1
    assert "os" in env.result["dependencies"]


def test_repo_lint_syntax_error(runtime):
    env = runtime.execute(_req("repo.lint", files={"bad.py": "def f(:\n  pass\n"}))
    assert env.ok
    assert env.result["error_count"] >= 1
    assert env.result["passed"] is False


def test_repo_lint_clean(runtime):
    env = runtime.execute(_req("repo.lint", files={"ok.py": "x = 1\n"}))
    assert env.ok
    assert env.result["error_count"] == 0
    assert env.result["passed"] is True


def test_repo_scan_secrets_redacted(runtime):
    files = {"cfg.py": "aws = 'AKIAIOSFODNN7EXAMPLE'\napi_key = 'supersecretlongvalue123'\n"}
    env = runtime.execute(_req("repo.scan_secrets", files=files))
    assert env.ok
    assert env.result["has_secrets"] is True
    assert env.result["secret_count"] >= 1
    # the raw secret must NOT appear anywhere in the result
    import json
    blob = json.dumps(env.result)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "supersecretlongvalue123" not in blob


def test_workflow_compose_chains_and_signs(runtime):
    steps = [
        {"id": "inspect", "endpoint": "file.inspect", "input": {"text": "hello"}},
        {"id": "verify", "endpoint": "artifact.verify",
         "input": {"text": "hello", "expected_sha256": "$inspect.sha256"}},
    ]
    env = runtime.execute(_req("workflow.compose", steps=steps))
    assert env.ok
    assert env.result["step_count"] == 2
    assert env.result["all_ok"] is True
    # data passed from step 1 (sha256) into step 2 → verify MATCH
    assert env.result["final_result"]["matches"] is True
    # every node in the graph is independently signed
    assert all(s["receipt_signed"] for s in env.result["steps"])
    # dependency edge recorded (inspect -> verify)
    assert {"from": "inspect", "to": "verify"} in env.result["edges"]


def test_workflow_compose_rejects_nested(runtime):
    env = runtime.execute(_req("workflow.compose", steps=[
        {"endpoint": "workflow.compose", "input": {"steps": []}}]))
    assert env.ok is False
