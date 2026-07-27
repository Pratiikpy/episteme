"""A request field this service does not read must be named, not dropped in silence.

Measured on the live service: `text.summarize` asked for `max_words: 15` returned an 11-word summary,
and the same request with `max_wrds` returned 21 — the caller set a cap, was ignored, and went 40%
over it. On a node whose entire job is to fit a budget, that is a confident answer to a different
question, and nothing in the response said the cap had been discarded.

A warning rather than a refusal: rejecting an unexpected field would break any client sending one
today. Warnings ride in the envelope and are covered by the signature, so the notice cannot be
separated from the answer it qualifies.
"""
from __future__ import annotations

import pytest

from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

REGISTRY = build_registry()
RUNTIME = Runtime(REGISTRY)
TEXT = "X Layer settles to Ethereum and uses OKB for gas. " * 40


def run(endpoint, **inp):
    return RUNTIME.execute(ArtifactRequest(endpoint=endpoint, input=inp)).model_dump()


def _warnings(env) -> str:
    return " ".join(env.get("warnings") or [])


def test_a_typo_is_named_with_a_suggestion():
    env = run("text.summarize", text=TEXT, max_wrds=15)
    w = _warnings(env)
    assert "max_wrds" in w
    assert "did you mean 'max_words'" in w
    assert "may not be the one you intended" in w


def test_a_correct_request_is_not_warned_about():
    assert _warnings(run("text.summarize", text=TEXT, max_words=15)) == ""


def test_the_accepted_fields_are_listed():
    w = _warnings(run("text.summarize", text=TEXT, totally_made_up=1))
    assert "Accepted fields:" in w and "max_words" in w


def test_several_unknown_fields_are_all_named():
    w = _warnings(run("text.stats", text="hello world", aaa=1, bbb=2))
    assert "aaa" in w and "bbb" in w and "them:" in w


def test_the_warning_does_not_change_the_result():
    """A notice must annotate the answer, never replace or degrade it."""
    clean = run("text.stats", text=TEXT)
    noisy = run("text.stats", text=TEXT, nonsense=1)
    assert noisy["ok"] is True
    assert noisy["result"]["words"] == clean["result"]["words"]


def test_the_warning_is_inside_the_signed_envelope():
    env = run("text.stats", text="hello world", nonsense=1)
    assert env.get("warnings"), "the notice must be in the envelope"
    assert env.get("receipt"), "and covered by the receipt the buyer verifies"


@pytest.mark.parametrize("endpoint", ["text.stats", "hash.compute", "data.stats"])
def test_ordinary_calls_across_several_nodes_stay_silent(endpoint):
    """If the check misfires, it fires on everything — this is the canary for that."""
    inputs = {"text.stats": {"text": "hello"},
              "hash.compute": {"text": "hello", "algos": ["sha256"]},
              "data.stats": {"rows": [{"a": 1}, {"a": 2}]}}
    assert _warnings(run(endpoint, **inputs[endpoint])) == ""
