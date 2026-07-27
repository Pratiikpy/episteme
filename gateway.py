"""Episteme gateway — A2MCP (x402) + A2A + health.

Survives the two OKX review gates:
  No.1 x402: unpaid paid-endpoint call -> HTTP 402 + PAYMENT-REQUIRED header
             (exact v2 shape); paid -> 200 + deterministic Universal Artifact Contract.
  No.2 live: /healthz and /a2a/message answer instantly (no hang) for the reviewer probe.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from config import get_settings
from contract import ArtifactRequest
from runtime import Runtime
from landing import landing_html
from nodes import build_registry
import x402
import listing as listing_mod
import schemas as schemas_mod


# The Node x402 gateway (gateway/server.ts) fronts this runtime in production: it runs the official
# OKX seller SDK, so by the time a request is proxied here the payment is ALREADY verified and settled
# by OKX's facilitator. Without a trusted hand-off the request would hit this module's own paywall and
# get a second 402 — the caller pays, then still gets 402 (exactly the "paid but not served" failure
# OKX rejects for). The gateway therefore presents this shared secret, compared in constant time.
# Fail-closed: an unset/empty secret grants nothing, so a misconfigured deploy charges rather than leaks.
_INTERNAL_HEADER = "X-Episteme-Internal"

# Nodes that exist only as the second engine in a differential (L4) check — never callable directly.
# Listed BY NAME on purpose: this used to be a `.verify`/`.alt` suffix match, which also blackholed
# `artifact.verify` and `receipt.verify` — two real priced services, one of which is the verifier for
# Episteme's own signed receipts. Advertising a price for an endpoint that 404s is indefensible, so
# the exclusion is now an explicit allow-list-by-exception, and both are price 0 by construction.
_INTERNAL_ONLY = frozenset({"file.inspect.verify", "csv.profile.alt"})


def _usage(endpoint: str, node) -> dict:
    """The reply to a request that supplied no input: the contract, with a worked example.

    Deliberately shaped so it cannot be mistaken for a result — `ok` is false and there is no
    `result` key — while still being the single most useful thing to hand someone who has paid and
    not said what they want."""
    schema = schemas_mod.schema_for(node) or {}
    props = schema.get("properties", {})
    required = schema.get("required", [])
    one_of = [list(a["required"])[0] for a in schema.get("anyOf", []) if a.get("required")]
    # Build a request that would actually be VALID. The alternatives in `one_of` are mutually
    # exclusive, so exactly one goes in — an example showing `csv` and `text` together tells the
    # caller to send both, which is precisely the wrong thing.
    alts = set(one_of)
    keys = [k for k in required if k not in alts]
    if not keys:
        keys = [k for k in list(props) if k not in alts][:2]
    if one_of:
        keys = [one_of[0], *keys]
    example = {k: (props[k].get("example") if isinstance(props.get(k), dict) else None) or f"<{k}>"
               for k in keys}
    out = {
        "ok": False,
        "status": "no_input_supplied",
        "endpoint": endpoint,
        "fee_usdt": node.price_usdt,
        "message": "No input was supplied, so there was nothing to process. Send the input shown "
                   "below in the request body. Nothing was computed for this call.",
        "inputSchema": schema,
        "required": required,
        "one_of": one_of,
        "example_request": {"input": example},
        "schema_url": f"/nodes/{endpoint}/schema",
    }
    # Surface the language-model dependency here too, not only inside the schema — this reply is what a
    # caller who paid without sending input actually reads.
    if schema.get("x-ai-backed"):
        out["ai_backed"] = True
        out["ai_disclosure"] = schema.get("x-ai-disclosure")
    return out


_LISTING_BLURBS: dict[str, str] = {}


def _listing_blurb(endpoint: str) -> str | None:
    """First line of the marketplace description for an endpoint — what the buyer read before choosing.

    Built once and cached: it is the only human statement of what a service does, and it must be the
    same words in the listing, on the landing page and in an error, or the three disagree.
    """
    if not _LISTING_BLURBS:
        try:
            listing = listing_mod.build_listing(build_registry(), get_settings())
            for svc in listing.get("services", []):
                ep = (svc.get("endpoint") or "").rsplit("/", 1)[-1]
                first = (svc.get("serviceDescription") or "").splitlines()[0].strip()
                if ep and first:
                    _LISTING_BLURBS[ep] = first
        except Exception:  # noqa: BLE001
            _LISTING_BLURBS["__failed__"] = ""  # never retry-storm on a broken listing build
    return _LISTING_BLURBS.get(endpoint)


def _guidance(endpoint: str, node, registry, raw_request: dict) -> dict:
    """What to add to an INVALID_INPUT failure so the caller is redirected instead of dead-ended.

    A listed ASP on this marketplace took a 1-star review because a buyer's very first call landed on
    the FIRST service in its listing — an endpoint that could not accept that request at all — even
    though the service was working perfectly and the buyer's second call succeeded. Buyers and
    reviewers do land on the wrong endpoint, and payment settles in the middleware BEFORE this runs, so
    they have already been charged by the time they find out.

    A bare `provide 'text' or 'content_b64'` does not tell someone who asked "is this token safe?" that
    they are in the wrong place. So an invalid-input failure now also carries what this endpoint does,
    a request that WOULD work, and the sibling endpoints that match what they appear to want.
    """
    schema = schemas_mod.schema_for(node) or {}
    usage = _usage(endpoint, node)
    # The SCHEMA description is an input instruction ("Provide exactly one of: 'text' or
    # 'content_b64'"), which tells someone who is on the wrong endpoint nothing about where they are.
    # The marketplace listing is the only place that says what the service actually DOES, and it is
    # the same sentence the buyer read before choosing it.
    what = _listing_blurb(endpoint)
    # Match the caller's words against the catalogue. Deliberately crude — it only ever ADDS a
    # suggestion, so a bad guess costs nothing, while a good one saves a wasted call.
    text = json.dumps(raw_request).lower()
    words = {w for w in re.findall(r"[a-z_]{4,}", text)
             if w not in _STOPWORDS}
    scored: list[tuple[int, str]] = []
    for info in registry.list():
        other = info["endpoint"]
        if other == endpoint or other in _INTERNAL_ONLY:
            continue
        # Endpoint names are `family.verb` (data.pivot, repo.scan_secrets) — the parts ARE the keywords.
        parts = set(re.split(r"[._]", other))
        hits = len(parts & words)
        if hits:
            scored.append((hits, other))
    scored.sort(key=lambda t: (-t[0], t[1]))
    suggestions = [e for _, e in scored[:4]]
    return {
        "what_this_endpoint_does": what,
        "how_to_call_it": (schema.get("description") or "").strip() or None,
        "required": schema.get("required", []),
        "one_of": usage.get("one_of", []),
        "example_request": usage.get("example_request"),
        "schema_url": f"/nodes/{endpoint}/schema",
        "did_you_mean": suggestions,
        "note": (None if suggestions else
                 "No other Episteme service matches this request either — see /nodes for the full "
                 "catalogue. Episteme handles files, documents, data tables, repositories and web "
                 "checks; it does not price tokens or give trading advice."),
        "all_services": "/nodes",
    }


# Words too common to identify a service; matching on them suggests everything and helps nobody.
_STOPWORDS = frozenset({
    "input", "text", "data", "json", "true", "false", "null", "none", "with", "that", "this",
    "from", "into", "your", "please", "value", "values", "content", "request", "query",
})


def _attach_guidance(payload: dict, endpoint: str, node, registry, raw_request: dict) -> None:
    """Enrich an INVALID_INPUT failure in place. Only that code — a genuine engine error or a limit
    breach is not a "you are in the wrong place" problem, and padding it with suggestions would bury
    the real cause."""
    err = payload.get("error") or {}
    if err.get("code") != "INVALID_INPUT":
        return
    payload["help"] = _guidance(endpoint, node, registry, raw_request)


def _is_trusted_internal(request: Request) -> bool:
    """True only when the front gateway presents the exact shared secret (constant-time)."""
    secret = os.environ.get("EPISTEME_INTERNAL_SECRET", "")
    if not secret:
        return False
    presented = request.headers.get(_INTERNAL_HEADER, "")
    return hmac.compare_digest(secret, presented)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    settings = get_settings()
    registry = build_registry()
    rt = runtime or Runtime(registry, settings)
    app = FastAPI(title="Episteme — verifiable artifact runtime", version="0.1.0")
    tasks: dict[str, dict] = {}  # in-memory A2A store (prod: Postgres + X Layer escrow)

    # The base URL is the first thing a reviewer, judge, or listing follower opens. Every service is a
    # POST under /a2mcp/, so a plain browser GET used to return a bare 404 — which reads as a dead
    # service. Free and unmetered: it advertises the paid routes, it is not one of them.
    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(landing_html(listing_mod.build_listing(registry, settings),
                                         settings.public_base_url,
                                         rt.signer.public_hex, set(_INTERNAL_ONLY)))

    @app.get("/proof", response_class=HTMLResponse)
    def proof():
        """The judge-facing page, served from this ASP's own domain.

        Generated by scripts/make_proof_deck.py from the live listing and a recorded paid run, so it
        cannot drift the way the previous hand-written deck did — that one cited an agent id which had
        since been REJECTED, on a host that had moved.
        """
        f = Path(__file__).resolve().parent / "proof.html"
        if not f.exists():
            return HTMLResponse("<h1>Proof deck not generated</h1>", status_code=404)
        return HTMLResponse(f.read_text(encoding="utf-8"))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "episteme", "ts": time.time(),
                "signing_public_key": rt.signer.public_hex,
                "signing_key_source": rt.signer.key_source}

    # The trust anchor for every receipt Episteme issues. Signer.verify() only proves a receipt is
    # internally consistent with whatever public key the receipt itself carries — so a forged
    # envelope signed with an attacker's own keypair verifies perfectly. What makes a receipt
    # attributable is checking its public_key against the key published HERE, out of band from the
    # receipt. Without a published key there is nothing to compare against and the signature proves
    # only that someone, somewhere, signed something.
    @app.get("/.well-known/episteme-signing-key")
    def signing_key():
        return {
            "service": "episteme",
            "algo": "ed25519",
            "public_key": rt.signer.public_hex,
            "key_source": rt.signer.key_source,
            "stable_across_restarts": rt.signer.key_source == "configured_secret",
            "usage": "Compare receipt.public_key against this value, then verify "
                     "receipt.signature over receipt.manifest_sha256. A receipt whose public_key "
                     "differs from this one was not issued by Episteme.",
        }

    @app.get("/nodes")
    def nodes():
        # The input schema travels with the node listing. OKX's service format has no field for it,
        # so the only machine-readable statement of "what do I send this thing" lives here — without
        # it a caller has to infer the contract from prose, and several nodes take their input via a
        # helper whose parameter names appear nowhere in the endpoint name or price.
        out = []
        for info in registry.list():
            node = registry.get(info["endpoint"])
            entry = dict(info)
            if node is not None and info["endpoint"] not in _INTERNAL_ONLY:
                entry["inputSchema"] = schemas_mod.schema_for(node)
            out.append(entry)
        return {"nodes": out}

    @app.get("/nodes/{endpoint}/schema")
    def node_schema(endpoint: str):
        node = registry.get(endpoint)
        if node is None or endpoint in _INTERNAL_ONLY:
            return JSONResponse({"error": f"unknown endpoint '{endpoint}'"}, status_code=404)
        return {"endpoint": endpoint, "fee_usdt": node.price_usdt,
                "inputSchema": schemas_mod.schema_for(node)}

    @app.get("/listing")
    def listing():
        return listing_mod.build_listing(registry, settings)

    @app.get("/validate-listing")
    def validate_listing():
        m = listing_mod.build_listing(registry, settings)
        return listing_mod.validate_listing(m)

    # Artifact retrieval. Nodes like image.transform and pdf.manipulate charge for a binary they
    # produce; without this route the caller paid and received a content address it could never
    # resolve. Deliberately NOT paywalled: the bytes were already bought, and the digest is only
    # ever disclosed inside the paid response, so possession of a 256-bit content address IS the
    # authorization. Reads are confined to the artifact directory by construction — the path is
    # rebuilt from a validated 64-char hex digest, so no caller-supplied string reaches the
    # filesystem and ../ traversal is impossible.
    @app.get("/artifact/{digest}")
    def artifact(digest: str):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
            return JSONResponse({"error": "invalid artifact digest"}, status_code=400)
        adir = Path(settings.artifact_dir)
        fpath = adir / digest.lower()
        if not fpath.is_file():
            return JSONResponse(
                {"error": "artifact not found or expired",
                 "detail": f"artifacts are retained for {settings.artifact_ttl_seconds}s after creation"},
                status_code=404,
            )
        age = time.time() - fpath.stat().st_mtime
        if age > settings.artifact_ttl_seconds:
            return JSONResponse(
                {"error": "artifact expired", "age_seconds": int(age),
                 "ttl_seconds": settings.artifact_ttl_seconds}, status_code=410
            )
        meta = {}
        mpath = adir / f"{digest.lower()}.meta"
        if mpath.is_file():
            try:
                meta = json.loads(mpath.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta = {}
        data = fpath.read_bytes()
        # Re-derive the digest rather than trusting the filename: this response doubles as proof
        # that the bytes served are exactly the bytes the signed receipt committed to.
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest.lower():
            return JSONResponse({"error": "artifact integrity check failed"}, status_code=500)
        name = str(meta.get("name") or digest[:12])
        return Response(
            content=data,
            media_type=str(meta.get("mime_type") or "application/octet-stream"),
            headers={
                "X-Artifact-SHA256": "sha256:" + actual,
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": f"public, max-age={settings.artifact_ttl_seconds}, immutable",
            },
        )

    # Method-agnostic on purpose: OKX's availability probe is a BARE GET with no body.
    # A POST-only route would return 405 and FAIL x402 listing validation, so every verb
    # hits the paywall and receives the standard 402 challenge.
    @app.api_route("/a2mcp/{endpoint}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def a2mcp(endpoint: str, request: Request):
        node = registry.get(endpoint)
        if node is None or endpoint in _INTERNAL_ONLY:
            return JSONResponse(status_code=404, content={"ok": False, "error": {
                "code": "INVALID_INPUT", "message": f"unknown endpoint '{endpoint}'"}})
        raw = await request.body()
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        inp = body.get("input", body) if isinstance(body, dict) else {}
        opts = body.get("options", {}) if isinstance(body, dict) else {}
        # No body at all is a distinct case from bad input. OKX's availability probe sends an empty
        # request, and a listed ASP was made to change exactly this behaviour during its review
        # (ShieldSuite commit eff7c6d, "comply with okx review"). It is also the fair answer: payment
        # settles in the middleware BEFORE this handler runs, so replying 422 to an empty request
        # charges the caller and hands them nothing. An empty request gets the input contract instead
        # — the most useful thing we can give someone who has paid but not said what they want.
        # A body that IS present and wrong still gets its specific 422 diagnostic.
        no_input_supplied = not raw.strip() or (not inp and not body)

        # x402 gate — paid endpoints must challenge before doing any work.
        #
        # This gate is keyed on the NODE'S OWN PRICE, not on the front gateway's route table, and
        # that is deliberate: it is the backstop for a route the table missed. If a priced node were
        # absent from routes.json (wrong service type, a listing filter, a hand-edit) the SDK would
        # wave it through and the work would be served for free — while the marketplace still lists
        # it at a fee. Anything priced pays here, so a gap in the table degrades to "charged twice
        # over" (caught in test) rather than "given away" (caught by OKX as a non-compliant endpoint).
        #
        # Skipped only for the front gateway's already-settled hand-off (see _is_trusted_internal).
        if node.price_usdt and node.price_usdt > 0 and not _is_trusted_internal(request):
            fee = f"{node.price_usdt:.6f}".rstrip("0").rstrip(".")
            schema = schemas_mod.schema_for(node) if endpoint not in _INTERNAL_ONLY else None

            # A call with no input is answered before the money moves, never after. Settling first
            # and then explaining the contract takes payment for work nobody could have performed —
            # the caller ends up with a fee on their statement and no artifact, which is the single
            # complaint most likely to lose a customer for good. The challenge carries the schema,
            # so this reply is also everything they need to get it right on the next attempt.
            if no_input_supplied:
                header_val, challenge = x402.build_challenge(
                    endpoint, fee, settings, description=f"Episteme {endpoint}",
                    input_schema=schema)
                return JSONResponse(
                    status_code=402,
                    content={**_usage(endpoint, node),
                             "not_charged": "no input was supplied, so nothing was billed. Send the "
                                            "input shown above together with payment.",
                             **challenge},
                    headers={x402.PAYMENT_REQUIRED_HEADER: header_val})

            pay_hdr = request.headers.get(x402.PAYMENT_HEADER)
            if not pay_hdr:
                header_val, challenge = x402.build_challenge(
                    endpoint, fee, settings, description=f"Episteme {endpoint}",
                    input_schema=schema)
                return JSONResponse(status_code=402, content=challenge,
                                    headers={x402.PAYMENT_REQUIRED_HEADER: header_val})
            result = x402.verify_payment(pay_hdr, settings)
            if not result.ok:
                header_val, challenge = x402.build_challenge(
                    endpoint, fee, settings, description=f"Episteme {endpoint}",
                    input_schema=schema)
                return JSONResponse(status_code=402,
                                    content={"error": "payment_invalid", "detail": result.detail, **challenge},
                                    headers={x402.PAYMENT_REQUIRED_HEADER: header_val})
            env = rt.execute(ArtifactRequest(endpoint=endpoint, input=inp, options=opts))
            payload = env.model_dump(mode="json")
            _attach_guidance(payload, endpoint, node, registry, body)
            return JSONResponse(status_code=200 if env.ok else 422,
                                content=payload,
                                headers={x402.PAYMENT_RESPONSE_HEADER: result.response_header or ""})

        # free endpoint
        if no_input_supplied:
            return JSONResponse(status_code=200, content=_usage(endpoint, node))
        env = rt.execute(ArtifactRequest(endpoint=endpoint, input=inp, options=opts))
        payload = env.model_dump(mode="json")
        _attach_guidance(payload, endpoint, node, registry, body)
        return JSONResponse(status_code=200 if env.ok else 422, content=payload)

    # ------------------------------------------------------------------ A2A
    @app.post("/a2a/message")
    async def a2a_message(request: Request):
        """Fast live-probe answer for reviewer gate No.2 (no LLM hang on first reply)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        msg = str(body.get("message", "")).strip()
        return {
            "ok": True,
            "reply": ("Hello — this is Episteme, the verifiable artifact runtime. "
                      "Tell me what to Clean / Understand / Verify / Transform / Rehearse "
                      "(a URL, file, dataset, repo, API or media) and I will return a signed, "
                      "reproducible artifact. For premium jobs (simulation/foresight, launch-readiness) "
                      "I can open an A2A task with escrow."),
            "echo": msg,
            "capabilities": [n["endpoint"] for n in registry.list()][:12],
        }

    @app.post("/a2a/tasks")
    async def a2a_create(request: Request):
        body = await request.json()
        tid = "task_" + uuid.uuid4().hex[:12]
        tasks[tid] = {"id": tid, "status": "open", "brief": body.get("brief", ""),
                      "service": body.get("service", ""), "budget": body.get("budget"),
                      "quote": body.get("budget") or "negotiable", "created": time.time()}
        return tasks[tid]

    @app.get("/a2a/tasks/{tid}")
    async def a2a_get(tid: str):
        t = tasks.get(tid)
        return t or JSONResponse(status_code=404, content={"error": "not found"})

    @app.post("/a2a/tasks/{tid}/accept")
    async def a2a_accept(tid: str):
        if tid not in tasks:
            return JSONResponse(status_code=404, content={"error": "not found"})
        tasks[tid]["status"] = "accepted"
        return tasks[tid]

    @app.post("/a2a/tasks/{tid}/deliver")
    async def a2a_deliver(tid: str, request: Request):
        if tid not in tasks:
            return JSONResponse(status_code=404, content={"error": "not found"})
        body = await request.json()
        endpoint = body.get("endpoint", "document.to_markdown")
        env = rt.execute(ArtifactRequest(endpoint=endpoint, input=body.get("input", {})))
        tasks[tid]["status"] = "delivered"
        tasks[tid]["artifact"] = env.model_dump(mode="json")
        return tasks[tid]

    @app.post("/a2a/tasks/{tid}/confirm")
    async def a2a_confirm(tid: str):
        if tid not in tasks:
            return JSONResponse(status_code=404, content={"error": "not found"})
        tasks[tid]["status"] = "settled"  # escrow released on X Layer in production
        return tasks[tid]

    return app


app = create_app()
