"""Watch every registered endpoint over hours, the way a reviewer arriving at any moment would.

David's list has two entries that a single point-in-time check cannot answer:

  * "Pay OK, no body / status 0 / timeout — Cold start; paid POST must answer in 300s"
  * "x402-check stable for hours"

Both are about behaviour ACROSS TIME, so this runs rounds on an interval and records, per endpoint,
every status seen and the worst latency observed. A single green sweep proves nothing about the moment
a reviewer happens to look.

    python scripts/stability_watch.py --rounds 12 --interval 900     # 3 hours
    python scripts/stability_watch.py --rounds 2 --interval 30       # smoke test
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

AGENTS = ("9177", "9178", "9165")
OUT = "stability_watch.json"


def registered() -> list[tuple[str, str, float]]:
    """(agent_id, endpoint_url, registered_fee) for every registered service."""
    rows = []
    for aid in AGENTS:
        r = subprocess.run(["onchainos", "agent", "service-list", "--agent-id", aid],
                           capture_output=True, text=True, timeout=120)
        for s in json.loads(r.stdout)["data"][0]["list"]:
            if s.get("endpoint"):
                rows.append((aid, s["endpoint"].strip(), float(s["fee"])))
    return rows


def probe(url: str, fee: float) -> dict:
    """One unpaid probe: the exact thing OKX's validator does. Records status, whether the header was
    present, whether the advertised amount still matches, and how long it took."""
    t0 = time.time()
    try:
        with httpx.Client(timeout=60.0) as c:
            r = c.get(url)
    except Exception as e:  # noqa: BLE001
        return {"status": None, "err": f"{type(e).__name__}", "ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    hdr = next((v for k, v in r.headers.items() if k.lower() == "payment-required"), None)
    amount_ok = None
    if hdr:
        try:
            amount_ok = abs(int(json.loads(base64.b64decode(hdr))["accepts"][0]["amount"]) / 1e6 - fee) < 1e-9
        except Exception:  # noqa: BLE001
            amount_ok = False
    return {"status": r.status_code, "header": bool(hdr), "amount_ok": amount_ok, "ms": ms}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--interval", type=int, default=900, help="seconds between rounds")
    args = ap.parse_args()

    services = registered()
    print(f"watching {len(services)} registered endpoints, {args.rounds} rounds every {args.interval}s "
          f"(~{args.rounds * args.interval / 3600:.1f}h)")

    seen: dict[str, set] = defaultdict(set)
    worst: dict[str, int] = defaultdict(int)
    bad_rounds: list[dict] = []

    for rnd in range(1, args.rounds + 1):
        started = datetime.now(timezone.utc).isoformat()
        failures = []
        for _aid, url, fee in services:
            p = probe(url, fee)
            ok = p.get("status") == 402 and p.get("header") and p.get("amount_ok") is not False
            seen[url].add(p.get("status"))
            worst[url] = max(worst[url], p["ms"])
            if not ok:
                failures.append({"url": url, **p})
            time.sleep(0.25)
        slowest = max(worst.values()) if worst else 0
        print(f"  round {rnd:>2}/{args.rounds} {started[11:19]}Z  "
              f"{len(services) - len(failures)}/{len(services)} correct  slowest so far {slowest}ms")
        for f in failures:
            print(f"      BAD {f['url'][-46:]} -> {f.get('status')} hdr={f.get('header')} "
                  f"amt_ok={f.get('amount_ok')} {f.get('err', '')}")
        if failures:
            bad_rounds.append({"round": rnd, "at": started, "failures": failures})
        if rnd < args.rounds:
            time.sleep(args.interval)

    flapping = {u: sorted(s, key=lambda x: (x is None, x)) for u, s in seen.items() if len(s) > 1}
    report = {
        "rounds": args.rounds,
        "interval_s": args.interval,
        "endpoints": len(services),
        "all_rounds_clean": not bad_rounds,
        "endpoints_that_ever_varied": flapping,
        "slowest_ms_overall": max(worst.values()) if worst else 0,
        "slowest_endpoints": sorted(((v, k) for k, v in worst.items()), reverse=True)[:5],
        "bad_rounds": bad_rounds,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)

    print()
    print(f"all rounds clean: {report['all_rounds_clean']}")
    print(f"endpoints that ever returned something other than 402: {len(flapping)}")
    for u, statuses in list(flapping.items())[:8]:
        print(f"  {u[-52:]} saw {statuses}")
    print(f"slowest unpaid probe seen: {report['slowest_ms_overall']}ms")
    print(f"wrote {OUT}")
    return 0 if report["all_rounds_clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
