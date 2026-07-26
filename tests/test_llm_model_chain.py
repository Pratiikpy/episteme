"""The LLM model chain must survive a gated model, and must NOT hide real request errors.

Why this exists: the router gates availability PER MODEL (`403 BALANCE_INSUFFICIENT`), not per account.
A sibling ASP's paid /research returned `500 Internal Server Error` to a caller who had already been
charged, purely because its single configured model was gated while other models on the same key
answered normally. Two behaviours have to hold, and they pull in opposite directions:

  * a model that cannot serve must be skipped, so one gated model never takes a paid endpoint down;
  * a malformed REQUEST must surface immediately, because retrying it against every model in the chain
    would multiply the latency of a paid call and still fail.

Driven with a fake client so the test is deterministic and needs no key or network.
"""
from __future__ import annotations

import pytest

from contract import ErrorCode
from nodes.llm_nodes import _chat
from runtime import NodeError


class _Msg:
    def __init__(self, content): self.message = type("M", (), {"content": content})()


class _Resp:
    def __init__(self, content): self.choices = [_Msg(content)]


class _FakeClient:
    """Fails for every model in `gated`, answers for anything else. Records what it was asked."""

    def __init__(self, gated: set[str], error: str = "Error code: 403 - BALANCE_INSUFFICIENT"):
        self.gated, self.error, self.tried = gated, error, []
        outer = self

        class _Completions:
            def create(self, *, model, messages, **kw):
                outer.tried.append(model)
                if model in outer.gated:
                    raise RuntimeError(outer.error)
                return _Resp(f"answered by {model}")

        self.chat = type("C", (), {"completions": _Completions()})()


def test_skips_gated_model_and_reports_the_one_that_answered():
    client = _FakeClient(gated={"minimax-m3"})
    content, model = _chat(client, [{"role": "user", "content": "hi"}])
    assert model != "minimax-m3", "a gated model must not be reported as the one that answered"
    assert content == f"answered by {model}"
    assert client.tried[0] == "minimax-m3", "the configured model must still be tried first"
    assert len(client.tried) >= 2, "the chain must advance past the gated model"


def test_real_request_error_surfaces_immediately():
    # A 400 for a malformed request is not a model-availability problem: it must not walk the chain.
    client = _FakeClient(gated={"minimax-m3"}, error="Error code: 400 - messages[0] is invalid")
    with pytest.raises(RuntimeError, match="400"):
        _chat(client, [{"role": "user", "content": "hi"}])
    assert client.tried == ["minimax-m3"], "a bad request must fail on the first model, not all of them"


def test_every_model_gated_raises_engine_failed():
    from config import get_settings

    client = _FakeClient(gated=set(get_settings().llm_model_chain))
    with pytest.raises(NodeError) as ei:
        _chat(client, [{"role": "user", "content": "hi"}])
    assert ei.value.code == ErrorCode.ENGINE_FAILED
    assert client.tried == get_settings().llm_model_chain, "every model must be attempted before failing"
