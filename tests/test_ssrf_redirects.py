"""The SSRF guard has to run on every hop, not only the one the caller typed.

`_ssrf_guard` validated the URL it was given and then handed it to a client with
`follow_redirects=True`. That is not a guard: a perfectly public address answering
`302 Location: http://169.254.169.254/latest/meta-data/iam/security-credentials/` walks straight past
it, and the service fetches the host's cloud credentials for the attacker. The first hop is the only
one they have to make look innocent.

Found by pointing a live paid call at a public redirector aimed into the metadata range. It reached
the address and failed only because that platform refused the connection — the hosting
environment's configuration, not our defence, and not something the same code can rely on elsewhere.
"""
from __future__ import annotations

import httpx
import pytest

from contract import ErrorCode
from nodes import web_nodes
from runtime import NodeError

BLOCKED = [
    "http://169.254.169.254/latest/meta-data/",          # AWS / Azure IMDS
    "http://127.0.0.1:8080/health",                      # loopback
    "http://10.0.0.1/",                                  # private range
    "http://localhost:8080/",                            # by name
]


def _client_returning(responses):
    """A fake httpx.Client yielding queued responses, so no packet ever leaves the test."""
    queue = list(responses)

    class FakeClient:
        def __init__(self, *a, **k):
            assert k.get("follow_redirects") is False, \
                "safe_get must never delegate redirect-following to the client"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return queue.pop(0)

    return FakeClient


@pytest.mark.parametrize("target", BLOCKED)
def test_a_redirect_into_a_blocked_address_is_refused(monkeypatch, target):
    """The attack itself: hop one is public, hop two is the metadata service."""
    hop1 = httpx.Response(302, headers={"location": target},
                          request=httpx.Request("GET", "https://example.org/go"))
    monkeypatch.setattr(httpx, "Client", _client_returning([hop1]))

    with pytest.raises(NodeError) as e:
        web_nodes.safe_get("https://example.org/go")
    assert e.value.code == ErrorCode.POLICY_BLOCKED


@pytest.mark.parametrize("target", BLOCKED)
def test_the_direct_address_is_still_refused(target):
    with pytest.raises(NodeError) as e:
        web_nodes._ssrf_guard(target)
    assert e.value.code == ErrorCode.POLICY_BLOCKED


def test_a_redirect_chain_is_bounded(monkeypatch):
    """A loop of public redirects must terminate rather than spin on the caller's money."""
    def endless(url, headers=None):
        n = int(url.rsplit("/", 1)[-1] or 0)
        return httpx.Response(302, headers={"location": f"https://example.org/{n + 1}"},
                              request=httpx.Request("GET", url))

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        get = staticmethod(endless)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(web_nodes, "_ssrf_guard", lambda u: None)

    with pytest.raises(NodeError) as e:
        web_nodes.safe_get("https://example.org/0")
    assert "redirect" in str(e.value).lower()


def test_a_plain_public_fetch_still_works(monkeypatch):
    """The control: guarding must not break the ordinary case."""
    ok = httpx.Response(200, text="<html>fine</html>",
                        request=httpx.Request("GET", "https://example.org/"))
    monkeypatch.setattr(httpx, "Client", _client_returning([ok]))
    monkeypatch.setattr(web_nodes, "_ssrf_guard", lambda u: None)

    assert web_nodes.safe_get("https://example.org/").status_code == 200
