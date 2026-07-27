"""A receipt that blesses an edited deliverable is worse than no receipt at all.

`receipt.verify` rebuilt the signed manifest and checked the signature over it — correctly — but the
manifest commits to `output_hash`, not to the result. Nothing ever re-hashed the result sitting in
the envelope. So editing `result` and leaving `output_hash` untouched returned **VALID**: a tampered
artifact, cryptographically endorsed.

Found by buying the service and then attacking what it sold, which is the only way this class of
defect shows itself — every individual check passed, and the verdict was confident and wrong.

Each test below is an attack. A verifier is only worth its fee if it fails them.
"""
from __future__ import annotations

import copy

import pytest

from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

RUNTIME = Runtime(build_registry())


def _genuine_envelope() -> dict:
    """A real, signed envelope produced by this service — the thing an attacker starts from."""
    env = RUNTIME.execute(ArtifactRequest(
        endpoint="text.stats",
        input={"text": "A proof is verified on Ethereum, and settlement follows from it."},
    )).model_dump()
    assert env["ok"] is True and env.get("receipt"), "the fixture itself must be a valid receipt"
    return env


def verify(envelope: dict) -> dict:
    env = RUNTIME.execute(ArtifactRequest(
        endpoint="receipt.verify", input={"envelope": envelope})).model_dump()
    assert env["ok"] is True, "the verifier must answer, not crash, on hostile input"
    return env["result"]


def test_an_untouched_receipt_still_verifies():
    """The control. Without it, a verifier that rejects everything would pass every other test."""
    out = verify(_genuine_envelope())
    assert out["receipt_valid"] is True
    assert out["output_hash_matches"] is True
    assert out["verdict"] in {"VALID", "VALID_UNATTRIBUTED"}


def test_editing_the_result_is_caught():
    """The defect itself: change what was delivered, leave the digest, and it used to pass."""
    env = _genuine_envelope()
    tampered = copy.deepcopy(env)
    key = next(iter(tampered["result"]))
    tampered["result"][key] = "TAMPERED"

    out = verify(tampered)
    assert out["output_hash_matches"] is False
    assert out["receipt_valid"] is False
    assert out["verdict"] == "INVALID_OR_TAMPERED"


def test_adding_a_field_to_the_result_is_caught():
    """Tampering is not only substitution — appending a fabricated finding must fail too."""
    tampered = copy.deepcopy(_genuine_envelope())
    tampered["result"]["injected_finding"] = "this was never computed"

    out = verify(tampered)
    assert out["output_hash_matches"] is False
    assert out["verdict"] == "INVALID_OR_TAMPERED"


def test_swapping_the_output_hash_is_caught():
    """The mirror attack: keep the result, forge the digest. This one always failed correctly."""
    tampered = copy.deepcopy(_genuine_envelope())
    tampered["output_hash"] = "sha256:" + "00" * 32

    out = verify(tampered)
    assert out["receipt_valid"] is False
    assert out["verdict"] == "INVALID_OR_TAMPERED"


def test_replacing_the_signature_is_caught():
    tampered = copy.deepcopy(_genuine_envelope())
    tampered["receipt"]["signature"] = "ab" * 64

    out = verify(tampered)
    assert out["signature_valid"] is False
    assert out["verdict"] == "INVALID_OR_TAMPERED"


def test_relabelling_the_endpoint_is_caught():
    """Passing off a cheap node's output as a dear one's. The endpoint is inside the manifest."""
    tampered = copy.deepcopy(_genuine_envelope())
    tampered["endpoint"] = "sim.run"

    out = verify(tampered)
    assert out["manifest_matches"] is False
    assert out["verdict"] == "INVALID_OR_TAMPERED"


def test_an_envelope_with_no_digest_is_not_called_intact():
    """Absence of evidence must not be reported as evidence of integrity."""
    stripped = copy.deepcopy(_genuine_envelope())
    stripped.pop("output_hash", None)

    out = verify(stripped)
    assert out["output_hash_matches"] is None
    # The manifest commits to output_hash, so removing it breaks the manifest too — the point is
    # that it must never come back as a clean pass.
    assert out["verdict"] == "INVALID_OR_TAMPERED"


@pytest.mark.parametrize("envelope", [{}, {"receipt": None}, {"receipt": {}, "result": {}}])
def test_malformed_envelopes_are_answered_not_crashed(envelope):
    env = RUNTIME.execute(ArtifactRequest(
        endpoint="receipt.verify", input={"envelope": envelope})).model_dump()
    assert env["ok"] is True
    assert env["result"].get("receipt_valid") in (False, None)
