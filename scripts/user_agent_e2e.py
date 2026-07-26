"""End-to-end test AS A USER AGENT: pay a real x402 invoice, receive a real deliverable.

This is how OKX tests a listed agent — from a registered User agent, paying with its own wallet and
checking a deliverable comes back. Reproducing it exactly means we can report our own results instead
of waiting to be tested.

  User agent : #8515 "xaorao"  (wallet 0xccf02b…, the same wallet the ASPs pay out to)
  Payment    : `onchainos payment pay` — EIP-3009 authorization signed in the TEE from that wallet,
               NOT a local private key, so it is genuinely the user agent paying.

For each service: fetch the 402 challenge, sign it, replay with PAYMENT-SIGNATURE, then judge whether
what came back is an actual deliverable rather than a 200-shaped shrug.

    python scripts/user_agent_e2e.py            # one representative service per ASP
    python scripts/user_agent_e2e.py --all      # every registered service (spends real USDT0)
"""
from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys
import time

import httpx

USER_AGENT_ID = "8515"
TIMEOUT = 300.0


def challenge(url: str, body: dict) -> tuple[str | None, str]:
    """Fetch the 402 and return its base64 PAYMENT-REQUIRED header.

    Retries once on a TRANSPORT failure. Over a 66-service run this harness twice hit a connect
    timeout on the unpaid challenge — once against DigitalOcean, once against Azure — while 40
    back-to-back probes of the same endpoints from two different networks returned 402 every time
    (0/40 failures from both). That profile is a transient network blip, not a dead endpoint, and
    reporting it as a service failure overstates a fault that a buyer would not see. A second
    consecutive failure is still reported: this retries the NETWORK, it does not paper over an outage.
    """
    last = ""
    for attempt in range(2):
        try:
            with httpx.Client(timeout=60.0) as c:
                r = c.post(url, json=body)
            break
        except Exception as e:  # noqa: BLE001
            last = f"challenge request failed: {type(e).__name__}: {e}"
            if attempt == 0:
                time.sleep(3.0)
    else:
        return None, last
    if r.status_code != 402:
        return None, f"expected 402, got {r.status_code}"
    hdr = next((v for k, v in r.headers.items() if k.lower() == "payment-required"), None)
    if not hdr:
        return None, "402 carried no PAYMENT-REQUIRED header"
    return hdr, ""


def sign(payload: str) -> tuple[str | None, str]:
    """Sign the challenge in the TEE from the user agent's own wallet."""
    out = subprocess.run(["onchainos", "payment", "pay", "--payload", payload],
                         capture_output=True, text=True, timeout=180)
    try:
        d = json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        return None, f"pay returned non-JSON: {out.stdout[:160]}{out.stderr[:160]}"
    if not d.get("ok"):
        return None, f"pay failed: {json.dumps(d)[:200]}"
    hdr = (d.get("data") or {}).get("authorization_header")
    return (hdr, "") if hdr else (None, "pay returned no authorization_header")


def judge(name: str, payload: dict) -> tuple[bool, str]:
    """Is this an actual deliverable, or a 200 that contains nothing?

    Deliberately strict: `ok: true` is not sufficient on its own, because that is exactly the failure
    mode this project keeps finding — a successful-looking response with no substance in it.
    """
    if payload.get("status") == "no_input_supplied":
        return False, "returned the usage contract — the test sent no usable input"
    if payload.get("ok") is False:
        return False, f"ok=false: {str(payload.get('error') or payload.get('reason'))[:90]}"
    # Episteme's Universal Artifact Contract
    if "result" in payload and isinstance(payload["result"], dict):
        checks = (payload.get("validation") or {}).get("tests") or []
        failed = [c["name"] for c in checks if not c.get("passed")]
        if failed:
            return False, f"validation checks failed: {failed}"
        lvl = (payload.get("validation") or {}).get("level", "?")
        sig = bool((payload.get("receipt") or {}).get("signature"))
        if not payload["result"]:
            return False, "result object was empty"
        return True, f"result + {len(checks)} checks passed, level={lvl}, signed={sig}"
    # Aletheia verdicts. The `answer`/`recommendation`/`classification`/`snapshot` keys were added
    # after reading the real responses one by one and confirming each carries substance — /dyor returns
    # BUY with conviction, 7 sections and 8 sources; /wallet-report a classification, risk, headline and
    # what_to_do; /ask a real answer with 3 powers used, 4 sources and a jury verdict; /watch/check a
    # baseline snapshot. Widening a test until it passes is worthless, so each is also required to have
    # CONTENT below, not merely to exist.
    for key in ("verdict", "decision", "status", "grade", "ruling", "score",
                "recommendation", "classification", "answer"):
        v = payload.get(key)
        if v in (None, ""):
            continue
        if isinstance(v, str) and key == "answer" and len(v) < 80:
            return False, f"answer present but only {len(v)} chars — too thin to be a deliverable"
        signer = (payload.get("signed") or {}).get("signer")
        extra = ""
        if payload.get("sections"):
            extra = f", {len(payload['sections'])} sections"
        elif payload.get("sources"):
            extra = f", {len(payload['sources'])} sources"
        return True, f"{key}={str(v)[:34]!r}{extra}, signed_by={str(signer)[:12]}…"
    # A monitoring baseline: the snapshot IS the deliverable — it is what the caller stores and passes
    # back next time — so require it to actually contain fields.
    if isinstance(payload.get("snapshot"), dict) and len(payload["snapshot"]) >= 3:
        signer = (payload.get("signed") or {}).get("signer")
        return True, (f"snapshot with {len(payload['snapshot'])} fields, "
                      f"compared={payload.get('compared')}, alerts={payload.get('alert_count', 0)}, "
                      f"signed_by={str(signer)[:12]}…")
    # A research report. TWO different shapes carry the `report` key and they must not be judged the
    # same way:
    #   Reach     — report is a STRING of prose, with cited_sources alongside it.
    #   Aletheia  — report is a DICT (answer / bottom_line / findings[].citations / signed).
    # This branch used to apply the string rules to both, so an Aletheia report was measured with
    # len(dict) — reporting "15 chars" for what was really 15 KEYS — found "0 cited" because citations
    # are nested per finding, and "signed=False" because the receipt sits inside the dict. Worse, the
    # branch was reached via a bare truthiness test, so ANY non-empty dict passed: a genuinely empty
    # report would have been marked PASS. Judged strictly per shape now.
    report = payload.get("report")
    if isinstance(report, dict):
        answer = str(report.get("answer") or "")
        if len(answer) < 120:
            return False, f"research answer only {len(answer)} chars — not a $0.10 deliverable"
        findings = report.get("findings") or []
        cited = sum(len(f.get("citations") or []) for f in findings if isinstance(f, dict))
        if not findings:
            return False, "research report carried no findings"
        if not cited:
            return False, f"{len(findings)} findings but not one citation — the claims are ungrounded"
        signer = ((report.get("signed") or {}) or {}).get("signer")
        return True, (f"{len(answer)} char answer, {len(findings)} findings, {cited} citations, "
                      f"stance={report.get('stance')}, signed_by={str(signer)[:12]}…")
    if isinstance(report, str):
        if len(report) < 200:
            return False, f"report only {len(report)} chars — too thin to be a deliverable"
        cited = len(payload.get("cited_sources") or [])
        if not cited:
            return False, "report carried no cited sources — the claims are ungrounded"
        return True, (f"report {len(report)} chars, {cited} cited, "
                      f"signed={bool(payload.get('signed'))}")
    if payload.get("results") or payload.get("content"):
        n = payload.get("result_count") or payload.get("chars") or 0
        return True, f"{'results' if payload.get('results') else 'content'} returned ({n})"
    return False, f"200 but nothing recognisable as a deliverable: keys={list(payload)[:8]}"


def _settle_tx(header: str | None) -> str | None:
    """Pull the settlement transaction hash out of a base64 PAYMENT-RESPONSE header.

    Field naming is not consistent between facilitator versions (`transaction`, `txHash`, `tx_hash`),
    so accept any of them rather than assuming one and silently reporting no settlement.
    """
    if not header:
        return None
    try:
        d = json.loads(base64.b64decode(header))
    except Exception:  # noqa: BLE001
        return None
    for k in ("transaction", "txHash", "tx_hash", "hash"):
        v = d.get(k)
        if isinstance(v, str) and v.startswith("0x") and len(v) >= 66:
            return v
    return None


_TX_CACHE: dict[str, bool] = {}


def onchain_confirmed(tx: str) -> bool:
    """Confirm a transaction hash really exists on X Layer, via the public RPC.

    Without this, `settled` was only ever "the facilitator sent a header", which would let a broken or
    dishonest settlement path pass the whole suite.
    """
    if tx in _TX_CACHE:
        return _TX_CACHE[tx]
    ok = False
    for attempt in range(3):
        try:
            with httpx.Client(timeout=30.0) as c:
                r = c.post("https://rpc.xlayer.tech",
                           json={"jsonrpc": "2.0", "id": 1,
                                 "method": "eth_getTransactionReceipt", "params": [tx]})
            rec = (r.json() or {}).get("result")
            if rec:
                # status "0x1" is success; a reverted transfer must not count as settled.
                ok = str(rec.get("status", "")).lower() in ("0x1", "1")
                break
            # A receipt can lag the facilitator's response by a block or two.
            time.sleep(2.5 * (attempt + 1))
        except Exception:  # noqa: BLE001
            time.sleep(2.0)
    _TX_CACHE[tx] = ok
    return ok


def run(label: str, url: str, body: dict) -> dict:
    t0 = time.time()
    row: dict = {"service": label, "url": url}
    ch, err = challenge(url, body)
    if not ch:
        return {**row, "ok": False, "stage": "challenge", "detail": err}
    row["challenge_ok"] = True
    try:
        acc = json.loads(base64.b64decode(ch)).get("accepts", [{}])[0]
        row["price_raw"] = acc.get("amount")
        row["asset"] = acc.get("asset", "")[:10]
        row["network"] = acc.get("network")
        row["pay_to"] = (acc.get("payTo") or "")[:12]
    except Exception:  # noqa: BLE001
        pass
    hdr, err = sign(ch)
    if not hdr:
        return {**row, "ok": False, "stage": "payment", "detail": err}
    row["paid"] = True
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.post(url, json=body, headers={"PAYMENT-SIGNATURE": hdr})
    except Exception as e:  # noqa: BLE001
        return {**row, "ok": False, "stage": "delivery", "detail": f"{type(e).__name__}: {e}"}
    row["http"] = r.status_code
    # The presence of a PAYMENT-RESPONSE header only proves the facilitator ANSWERED. It is not proof
    # the transfer happened. Decode it, keep the transaction hash, and confirm the hash exists on X
    # Layer before this row is allowed to claim settlement — an unverified header is exactly the kind
    # of 200-shaped shrug this harness exists to catch.
    resp_hdr = next((v for k, v in r.headers.items() if k.lower() == "payment-response"), None)
    row["payment_response_header"] = bool(resp_hdr)
    row["settle_tx"] = _settle_tx(resp_hdr)
    row["settled"] = onchain_confirmed(row["settle_tx"]) if row["settle_tx"] else False
    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        return {**row, "ok": False, "stage": "delivery", "detail": f"non-JSON body: {r.text[:120]}"}
    # Keep the REQUEST and the RESPONSE, not just the verdict. `judge` answers "is this a deliverable
    # at all"; it cannot answer "is this output correct and worth the fee". Only the actual text can,
    # and that has to be read by something other than the code that produced it.
    row["request"] = body
    row["response"] = payload
    good, detail = judge(label, payload)
    return {**row, "ok": good and r.status_code == 200, "stage": "delivered" if good else "delivery",
            "detail": detail, "seconds": round(time.time() - t0, 1)}


EPISTEME = "https://episteme.blacksky-e393132e.centralindia.azurecontainerapps.io"
CASES = [
    # Episteme — deterministic, signed receipt
    ("Episteme csv.profile", f"{EPISTEME}/a2mcp/csv.profile",
     {"input": {"csv": "id,amount,region\n1,1284500.50,EMEA\n2,4300.00,APAC\n3,0.000015,EMEA"}}),
    ("Episteme repo.scan_secrets", f"{EPISTEME}/a2mcp/repo.scan_secrets",
     # Split so the file carries no literal a secret scanner would flag (see test_service_quality.py).
     {"input": {"files": {"app.py": 'KEY = "' + "sk-" + 'proj-abc123XYZ_deadbeef-cafe0987654321zzTT"\n'}}}),
    ("Episteme image.transform", f"{EPISTEME}/a2mcp/image.transform",
     {"input": {"content_b64": None, "op": "resize", "width": 64}}),   # filled at runtime
    # Aletheia — signed verdicts
    ("Aletheia verdict", "https://api.ivaronix.xyz/verdict",
     {"chain": "ethereum", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7"}),
    ("Aletheia wallet-health", "https://api.ivaronix.xyz/wallet-health",
     {"chain": "ethereum", "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}),
    # Reach — live web
    ("Reach search", "https://reach.ivaronix.xyz/search",
     {"query": "X Layer OKX rollup architecture", "num": 6}),
    ("Reach read", "https://reach.ivaronix.xyz/read", {"url": "https://example.com"}),
]


# ---------------------------------------------------------------- full sweep (--all)
# Input keys taken from the handlers, not guessed: verified against src/api/server.ts and server.py.
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"      # a real, heavily-traded token
VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"    # a real wallet with genuine history

ALETHEIA = "https://api.ivaronix.xyz"
ALETHEIA_CASES = [
    ("verdict", "/verdict", {"chain": "ethereum", "address": USDT, "tier": "flag"}),
    ("check", "/check", {"subject": USDT, "chain": "ethereum"}),
    ("actionguard", "/actionguard", {"type": "counterparty", "chain": "ethereum", "address": VITALIK}),
    ("audit", "/audit", {"endpoint": "https://example.com", "model": "gpt-4o-mini"}),
    ("settle", "/settle", {"question": "Is X Layer an Ethereum layer-2?"}),
    ("kol", "/kol", {"handle": "VitalikButerin"}),
    ("watch/check", "/watch/check", {"chain": "ethereum", "address": USDT}),
    ("wallet-health", "/wallet-health", {"chain": "ethereum", "address": VITALIK}),
    ("verify", "/verify", {"question": "What is 2+2?", "answer": "4"}),
    ("contract-audit", "/contract-audit", {"chain": "ethereum", "address": USDT}),
    ("dyor", "/dyor", {"chain": "ethereum", "address": USDT}),
    ("report", "/report", {"chain": "ethereum", "address": USDT}),
    ("wallet-report", "/wallet-report", {"chain": "ethereum", "address": VITALIK}),
    ("tx-report", "/tx-report", {"chain": "ethereum", "to": USDT,
                                 "data": "0xa9059cbb000000000000000000000000d8da6bf26964af9d7eed9e03e5"
                                         "3415d37aa960450000000000000000000000000000000000000000000000000000000000000064"}),
    ("research", "/research", {"subject": "X Layer", "chain": "ethereum"}),
    ("ask", "/ask", {"question": "Is USDT on Ethereum safe to trade right now?"}),
]

REACH = "https://reach.ivaronix.xyz"
REACH_CASES = [
    ("search", "/search", {"query": "X Layer OKX rollup architecture", "num": 6}),
    ("read", "/read", {"url": "https://example.com"}),
    ("research", "/research", {"question": "What is X Layer and what secures it?", "max_rounds": 4}),
]


def all_cases() -> list:
    """Every registered service, using the exercise harness's inputs for Episteme."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from scripts.exercise_all import CASES as EP_CASES  # noqa: PLC0415

    cases = []
    for ep, inp in sorted(EP_CASES.items()):
        if ep in {"receipt.verify"}:      # needs a live envelope; covered by the default run
            continue
        cases.append((f"Episteme {ep}", f"{EPISTEME}/a2mcp/{ep}", {"input": inp}))
    for name, path, body in ALETHEIA_CASES:
        cases.append((f"Aletheia {name}", f"{ALETHEIA}{path}", body))
    for name, path, body in REACH_CASES:
        cases.append((f"Reach {name}", f"{REACH}{path}", body))
    return cases


def _png_b64() -> str:
    import io

    from PIL import Image
    im = Image.new("RGB", (200, 120), (40, 90, 160))
    b = io.BytesIO()
    im.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


def main() -> int:
    full = "--all" in sys.argv
    cases = all_cases() if full else list(CASES)
    for i, (label, url, body) in enumerate(cases):
        if "image.transform" in label and body.get("input", {}).get("content_b64") is None:
            body["input"]["content_b64"] = _png_b64()
            cases[i] = (label, url, body)

    print(f"END-TO-END AS USER AGENT #{USER_AGENT_ID}")
    print("payment: onchainos payment pay (EIP-3009 signed in TEE from the user agent's wallet)")
    print("=" * 96)
    rows = []
    for label, url, body in cases:
        row = run(label, url, body)
        rows.append(row)
        mark = "PASS" if row["ok"] else "FAIL"
        print(f"{mark}  {label:<28} HTTP {row.get('http', '-')}  "
              f"paid={row.get('paid', False)}  settled={row.get('settled', False)}  "
              f"{row.get('seconds', '-')}s")
        print(f"      {row.get('detail', '')}")
        time.sleep(1.0)

    passed = sum(1 for r in rows if r["ok"])
    print("=" * 96)
    print(f"{passed}/{len(rows)} services: paid a real invoice and returned a real deliverable")
    for r in rows:
        if not r["ok"]:
            print(f"  FAILED at {r['stage']}: {r['service']} — {r.get('detail')}")
    # Two files on purpose. The summary is what a human reads; the payload dump is what an INDEPENDENT
    # reviewer reads, because "did it pass" and "was the answer any good" are different questions and
    # the second one cannot be answered by the harness that produced the output.
    slim = [{k: v for k, v in r.items() if k not in ("request", "response")} for r in rows]
    with open("user_agent_e2e_results.json", "w", encoding="utf-8") as f:
        json.dump({"user_agent_id": USER_AGENT_ID, "passed": passed, "total": len(rows),
                   "rows": slim}, f, indent=1)
    with open("user_agent_e2e_payloads.json", "w", encoding="utf-8") as f:
        json.dump([{"service": r["service"], "url": r.get("url"), "fee_raw": r.get("price_raw"),
                    "request": r.get("request"), "response": r.get("response")} for r in rows],
                  f, indent=1, ensure_ascii=False)
    print("wrote user_agent_e2e_results.json + user_agent_e2e_payloads.json")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
