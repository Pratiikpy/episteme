"""Tests for utility batch 3: text.stats, csv.to_table, receipt.verify."""
from contract import ArtifactRequest


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


def test_text_stats(runtime):
    env = runtime.execute(_req("text.stats", text="Hello world. Two sentences here!"))
    assert env.ok
    assert env.result["words"] == 5
    assert env.result["sentences"] == 2


def test_csv_to_table(runtime):
    env = runtime.execute(_req("csv.to_table", csv="a,b\n1,2\n3,4\n", format="github"))
    assert env.ok
    assert env.result["rows"] == 2 and env.result["cols"] == 2
    assert "a" in env.result["table"] and "|" in env.result["table"]


def test_receipt_verify_valid(runtime):
    # produce a real signed envelope, serialize it, then verify independently
    src = runtime.execute(_req("hash.compute", text="verify me"))
    env_dict = src.model_dump(mode="json")
    out = runtime.execute(_req("receipt.verify", envelope=env_dict))
    assert out.ok
    assert out.result["receipt_valid"] is True
    assert out.result["manifest_matches"] is True
    assert out.result["signature_valid"] is True
    assert out.result["verdict"] == "VALID"


def test_receipt_verify_detects_tamper(runtime):
    src = runtime.execute(_req("hash.compute", text="verify me"))
    env_dict = src.model_dump(mode="json")
    env_dict["output_hash"] = "sha256:" + "0" * 64  # tamper the signed field
    out = runtime.execute(_req("receipt.verify", envelope=env_dict))
    assert out.ok
    assert out.result["receipt_valid"] is False
    assert out.result["verdict"] == "INVALID_OR_TAMPERED"
