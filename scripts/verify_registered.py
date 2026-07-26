"""Verify every endpoint OKX actually has registered is live and correctly paywalled.

Reads the registered service list straight from the marketplace API for each agent, then probes each
endpoint exactly as OKX's availability check does: a bare request with no body, expecting HTTP 402 and
a PAYMENT-REQUIRED header. A registered service whose endpoint 404s is an advertised price for
something that cannot be bought — the clearest possible rejection cause.

Written in Python on purpose. Doing this in shell has produced two false outages already: URLs read
from a file carried a Windows \\r that silently corrupted every request, and a `while read` loop had
curl eating its own stdin. Both looked exactly like a total outage.

    python scripts/verify_registered.py 9177 9178 9165
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import httpx

TIMEOUT = 30.0
PAUSE = 0.7   # keep the probe polite; a burst gets throttled and reads as an outage


def registered_services(agent_id: str) -> list[dict]:
    out = subprocess.run(
        ["onchainos", "agent", "service-list", "--agent-id", agent_id],
        capture_output=True, text=True, timeout=120,
    )
    payload = json.loads(out.stdout)
    if not payload.get("ok"):
        raise RuntimeError(f"service-list failed for {agent_id}: {out.stdout[:200]}")
    return payload["data"][0]["list"]


def probe(url: str) -> tuple[int | None, bool, str]:
    """(status, has_payment_required_header, note) for a bare unpaid request."""
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as c:
            r = c.get(url)
        header = any(k.lower() == "payment-required" for k in r.headers)
        return r.status_code, header, ""
    except Exception as e:  # noqa: BLE001
        return None, False, f"{type(e).__name__}: {e}"


def main() -> int:
    agent_ids = sys.argv[1:] or ["9177", "9178", "9165"]
    total_bad = 0
    for aid in agent_ids:
        services = registered_services(aid)
        print(f"\n=== agent {aid}: {len(services)} registered services ===")
        bad: list[str] = []
        for s in services:
            url = (s.get("endpoint") or "").strip()
            name = (s.get("serviceName") or "?")[:26]
            # An A2A service has NO http endpoint by design — the CLI requires `endpoint` to be
            # omitted for A2A, because the work is negotiated over XMTP through the A2A daemon rather
            # than called over HTTP. Probing for one and calling its absence "BROKEN" is this script
            # misreading a correct registration.
            if (s.get("serviceType") or "").upper() == "A2A":
                print(f"  a2a {name:<26} no endpoint (correct — negotiated over XMTP)")
                continue
            if not url:
                bad.append(f"{name}: no endpoint registered")
                continue
            status, header, note = probe(url)
            if status is None:
                # One retry: a transient connect timeout is indistinguishable from a dead endpoint, and
                # this script exists to tell the difference. Verified case: /tx-report timed out once at
                # 0.4s spacing, then answered 402 four times in a row when paced.
                time.sleep(2.0)
                status, header, note = probe(url)
            ok = status == 402 and header
            if not ok:
                bad.append(f"{name} {url} -> {status} header={header} {note}")
            print(f"  {'ok ' if ok else 'BAD'} {name:<26} {status} hdr={int(header)} "
                  f"fee={s.get('fee')}")
            time.sleep(PAUSE)
        http_svcs = [s for s in services if (s.get("serviceType") or "A2MCP").upper() != "A2A"]
        print(f"  -> {len(http_svcs) - len(bad)}/{len(http_svcs)} HTTP services return 402 + PAYMENT-REQUIRED"
              f"{f' (+{len(services) - len(http_svcs)} A2A, no endpoint by design)' if len(http_svcs) != len(services) else ''}")
        for b in bad:
            print(f"     BROKEN: {b}")
        total_bad += len(bad)
    print(f"\n{'ALL REGISTERED ENDPOINTS LIVE AND PAYWALLED' if not total_bad else f'{total_bad} BROKEN'}")
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
