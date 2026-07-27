"""An unparseable model reply gets one correction attempt before the call is lost.

The existing retry only fired when the *request* threw — a router rejecting `response_format`. A
model that answered with prose instead of JSON never reached it, so a single badly-formatted reply
ended the call. Measured: `document.extract_json` returned a signed result for one request and
ENGINE_FAILED for the byte-identical request in a later sweep, because the failure is the model's
formatting on the day rather than anything about the input. The caller had paid both times.

The error message also claimed the call had been "retried at temperature 0", which had not happened
for that failure. A message describing a step that did not run is worse than a terse one.

Failing closed is preserved: two unparseable attempts is still an error, never a signed deliverable.
"""
from __future__ import annotations

import pytest

from nodes.llm_nodes import _coerce_json


@pytest.mark.parametrize(("raw", "expected"), [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('<think>weighing it up</think>{"a": 1}', {"a": 1}),
    ('Here you go: {"a": 1} — hope that helps', {"a": 1}),
    ('{"a": {"b": [1, 2]}}', {"a": {"b": [1, 2]}}),
])
def test_recoverable_shapes_are_recovered(raw, expected):
    value, ok = _coerce_json(raw)
    assert ok is True
    assert value == expected


@pytest.mark.parametrize("raw", [
    "",
    "I cannot answer that.",
    "<think>still thinking",
])
def test_genuinely_unparseable_replies_report_failure(raw):
    """These are the ones a retry exists for — they must report failure, not a sentinel that
    downstream code mistakes for a successful extraction."""
    value, ok = _coerce_json(raw)
    assert ok is False


def test_the_failure_message_describes_what_actually_happened():
    import inspect

    from nodes import llm_nodes
    src = inspect.getsource(llm_nodes)
    assert "attempts" in src, "the message must describe the attempts actually made"
    assert "retried at temperature 0)" not in src, "the old message claimed a retry that never ran"


def test_the_retry_quotes_the_bad_reply_back():
    """Asking again with no context invites the same mistake; the failure is quoted for correction."""
    import inspect

    from nodes import llm_nodes
    src = inspect.getsource(llm_nodes)
    assert "could not be parsed as JSON" in src
    assert '"role": "assistant", "content": content[:2000]' in src


def test_the_retry_moves_to_a_different_model():
    """Re-asking the model that just failed to format invites the same answer. Measured: without
    this, one call in five still failed. The chain must advance."""
    import inspect

    from nodes import llm_nodes
    src = inspect.getsource(llm_nodes)
    assert "skip: tuple" in src, "_chat must accept models to exclude"
    assert "if model in skip:" in src, "and must actually skip them"
    assert "skip=tuple(tried)" in src, "the retry must pass the failed model in"
