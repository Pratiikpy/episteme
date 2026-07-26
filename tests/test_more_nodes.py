"""Tests for the broadened endpoints: openapi.inspect, data.transform_json, document.chunk."""
from contract import ArtifactRequest, VerificationLevel


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


def test_openapi_inspect(runtime):
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Demo API"},
        "paths": {
            "/users": {"get": {"operationId": "listUsers", "summary": "list"},
                       "post": {"operationId": "createUser"}},
            "/users/{id}": {"get": {"operationId": "getUser"}},
        },
        "components": {"securitySchemes": {"bearer": {"type": "http"}}},
    }
    env = runtime.execute(_req("openapi.inspect", spec=spec))
    assert env.ok
    assert env.result["operation_count"] == 3
    assert env.result["path_count"] == 2
    assert "bearer" in env.result["security_schemes"]
    assert env.validation.level == VerificationLevel.L3_REPRODUCED


def test_data_transform_pick(runtime):
    env = runtime.execute(_req("data.transform_json",
                               data=[{"a": 1, "b": 2}, {"a": 3, "b": 4}],
                               op="pick", fields=["a"]))
    assert env.ok
    assert env.result["result"] == [{"a": 1}, {"a": 3}]


def test_data_transform_flatten(runtime):
    env = runtime.execute(_req("data.transform_json",
                               data={"x": {"y": 1}, "z": [2, 3]}, op="flatten"))
    assert env.ok
    assert env.result["result"]["x.y"] == 1
    assert env.result["result"]["z.0"] == 2


def test_data_transform_select(runtime):
    env = runtime.execute(_req("data.transform_json",
                               data=[{"t": "a"}, {"t": "b"}], op="select", field="t", value="b"))
    assert env.result["result"] == [{"t": "b"}]


def test_document_chunk(runtime):
    text = "Para one.\n\nPara two is here.\n\n" + ("x" * 2000)
    env = runtime.execute(_req("document.chunk", text=text, chunk_size=500, overlap=50))
    assert env.ok
    assert env.result["chunk_count"] >= 4
    assert all(len(c) <= 500 for c in env.result["chunks"])
    assert env.validation.level == VerificationLevel.L3_REPRODUCED


def test_document_chunk_bad_params(runtime):
    env = runtime.execute(_req("document.chunk", text="hi", chunk_size=100, overlap=100))
    assert env.ok is False
