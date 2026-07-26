"""Differential verification honesty: disagreement must be surfaced, not hidden.

Proves: when an independent verifier disagrees, the result does NOT reach L4,
`differential_agreement` is False, and a warning is recorded.
"""
from contract import ArtifactRequest, VerificationLevel, ValidationCheck
from runtime import Node, NodeContext, NodeRegistry, Runtime


class _Primary(Node):
    name = "demo.primary"
    price_usdt = 0.0
    deterministic = True
    engine = "demo"

    def run(self, ctx: NodeContext) -> dict:
        return {"answer": 42, "verify_key": "A"}

    def validate(self, result, ctx):
        return [ValidationCheck(name="ok", passed=True)]


class _AgreeVerifier(Node):
    name = "demo.verify.agree"
    deterministic = True
    engine = "demo-alt"

    def run(self, ctx: NodeContext) -> dict:
        return {"verify_key": "A"}  # agrees


class _DisagreeVerifier(Node):
    name = "demo.verify.disagree"
    deterministic = True
    engine = "demo-alt"

    def run(self, ctx: NodeContext) -> dict:
        return {"verify_key": "B"}  # disagrees


def _runtime_with(verifier_name, verifier_cls):
    reg = NodeRegistry()
    reg.register(_Primary(), verifier=verifier_name)
    reg.register(verifier_cls())
    return Runtime(reg)


def test_agreement_reaches_l4():
    rt = _runtime_with("demo.verify.agree", _AgreeVerifier)
    env = rt.execute(ArtifactRequest(endpoint="demo.primary", input={}))
    assert env.validation.level == VerificationLevel.L4_INDEPENDENT
    assert env.validation.differential_agreement is True
    assert env.validation.independent_verifier == "demo.verify.agree"


def test_disagreement_is_surfaced_not_hidden():
    rt = _runtime_with("demo.verify.disagree", _DisagreeVerifier)
    env = rt.execute(ArtifactRequest(endpoint="demo.primary", input={}))
    # stays at L3 (reproduced) — NOT elevated to L4
    assert env.validation.level == VerificationLevel.L3_REPRODUCED
    assert env.validation.differential_agreement is False
    # disagreement recorded as a visible warning + a failing check
    assert any("disagreed" in w for w in env.validation.warnings)
    assert any(c.name == "differential_verification" and c.passed is False
               for c in env.validation.tests)
    # receipt still valid (we sign honest state, including the disagreement)
    assert rt.verify_receipt(env)
