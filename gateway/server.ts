/**
 * Episteme ASP — x402 pay-per-call gateway.
 *
 * A thin front for the Episteme Python runtime. It reuses OKX's official Agent-Payments (x402) SDK —
 * the exact integration OKX's marketplace validates against, and the same one Aletheia and Reach
 * passed — then proxies the paid call through to the FastAPI app on localhost. Nothing about payments
 * is re-implemented here.
 *
 * Public → Caddy (TLS, api.zerithfi.tech) → THIS gateway (127.0.0.1:8891) → Episteme FastAPI (127.0.0.1:8890).
 *
 * Two hard-won details baked in (verified with OKX's own `onchainos agent x402-check` on the sibling
 * ASPs): route keys carry no method prefix so the paywall answers 402 on OKX's bare-GET availability
 * probe (not 404/405), and each accepts entry publishes `decimals:6` inside `extra` so OKX can resolve
 * the USDT0 price (it isn't in OKX's token list, and a top-level `decimals` is dropped by OKX's
 * canonical re-serialization).
 */
import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { OKXFacilitatorClient } from "@okxweb3/app-x402-core";
import { x402ResourceServer, x402HTTPResourceServer, paymentMiddlewareFromHTTPServer } from "@okxweb3/app-x402-hono";
import { ExactEvmScheme } from "@okxweb3/app-x402-evm/exact/server";
import type { MiddlewareHandler } from "hono";
import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

const NETWORK = "eip155:196" as const; // X Layer mainnet
const PAY_TO = process.env.X402_PAY_TO || process.env.EPISTEME_PAYTO || "";
const OKX_API_KEY = process.env.OKX_API_KEY || "";
const OKX_SECRET_KEY = process.env.OKX_SECRET_KEY || "";
const OKX_PASSPHRASE = process.env.OKX_PASSPHRASE || "";
const BACKEND = (process.env.EPISTEME_BACKEND || "http://127.0.0.1:8890").replace(/\/$/, "");
const INTERNAL_SECRET = process.env.EPISTEME_INTERNAL_SECRET || "";

const OKX_PAY_ENABLED = !!(
  process.env.X402_ENABLED === "1" && PAY_TO && OKX_API_KEY && OKX_SECRET_KEY && OKX_PASSPHRASE
);

/** Paid A2MCP services, generated from the Python listing (single source of truth):
 *    python scripts/export_routes.py  ->  gateway/routes.json
 *  Keys carry NO HTTP-method prefix on purpose (spaceless key = verb "*"), so OKX's method-agnostic
 *  bare-GET probe receives the standard 402 challenge instead of falling through to 405. */
type RouteSpec = { price: string; maxTimeoutSeconds: number; description: string; mimeType: string };
const RAW: Record<string, RouteSpec> = JSON.parse(
  readFileSync(new URL("./routes.json", import.meta.url), "utf-8")
);

const ROUTES = Object.fromEntries(
  Object.entries(RAW).map(([path, r]) => [
    path,
    {
      accepts: {
        scheme: "exact" as const,
        network: NETWORK,
        payTo: PAY_TO,
        price: r.price,
        maxTimeoutSeconds: r.maxTimeoutSeconds,
        extra: { decimals: 6 },
      },
      description: r.description,
      mimeType: r.mimeType,
    },
  ])
);

let resourceServer: any = null;

/** Nonces already spent on a delivery. In-memory is the honest scope for a single-replica
 *  deployment (minReplicas=1); with several replicas this must move to shared storage, since a
 *  per-process cache would let a replay land on a different replica. */
const CONSUMED_NONCES = new Map<string, number>();
const NONCE_TTL_MS = 24 * 60 * 60 * 1000; // outlives any route's maxTimeoutSeconds (max 300s)
const NONCE_CAP = 50_000;                 // bound memory; a flood evicts oldest-first

function rememberNonce(nonce: string): void {
  const now = Date.now();
  CONSUMED_NONCES.set(nonce, now);
  // Amortised sweep — cheap, and keeps the map from growing without bound.
  if (CONSUMED_NONCES.size > NONCE_CAP || CONSUMED_NONCES.size % 512 === 0) {
    for (const [k, t] of CONSUMED_NONCES) {
      if (now - t > NONCE_TTL_MS) CONSUMED_NONCES.delete(k);
    }
    while (CONSUMED_NONCES.size > NONCE_CAP) {
      const oldest = CONSUMED_NONCES.keys().next().value;
      if (oldest === undefined) break;
      CONSUMED_NONCES.delete(oldest);
    }
  }
}

/** Pull the EIP-3009 authorization nonce out of a base64 PAYMENT-SIGNATURE header.
 *  Returns "" when it cannot be read, and the caller then lets the SDK reject the payment —
 *  an unparseable header must never be treated as a fresh, unused authorization. */
function readPaymentNonce(header: string): string {
  if (!header) return "";
  try {
    const decoded = JSON.parse(Buffer.from(header, "base64").toString("utf-8"));
    const auth = decoded?.payload?.authorization ?? decoded?.authorization;
    const nonce = auth?.nonce ?? decoded?.payload?.nonce;
    // Scope the key to the paid resource: the SDK already rejects cross-endpoint reuse, and scoping
    // keeps one endpoint's bookkeeping from ever masking another's.
    const scope = decoded?.accepted?.payTo ?? decoded?.resource?.url ?? "";
    return nonce ? `${String(scope)}|${String(nonce)}` : "";
  } catch {
    return "";
  }
}

/** Mirror the x402 challenge into the 402 response BODY via the SDK's documented `unpaidResponseBody`
 *  hook. The SDK emits the PAYMENT-REQUIRED header itself, so header- and body-reading clients — and
 *  OKX's `x402-check` — all see a complete challenge. Also injects `decimals: 6` (USDT0 isn't in OKX's
 *  token list). */
function mirrorChallenge(accepts: any, description: string, mimeType: string) {
  return async (context: any) => {
    const rs: any = resourceServer;
    const requirements = await rs.buildPaymentRequirementsFromOptions([accepts], context);
    const url = context?.adapter?.getUrl?.() ?? "";
    const paymentRequired = await rs.createPaymentRequiredResponse(
      requirements, { url, description, mimeType }, "Payment required"
    );
    if (Array.isArray(paymentRequired?.accepts)) {
      for (const a of paymentRequired.accepts) {
        if (a && typeof a === "object") a.extra = { ...(a.extra ?? {}), decimals: 6 };
      }
    }
    return { contentType: "application/json", body: paymentRequired };
  };
}

function buildOkxPayMiddleware(): MiddlewareHandler {
  const facilitatorClient = new OKXFacilitatorClient({
    apiKey: OKX_API_KEY, secretKey: OKX_SECRET_KEY, passphrase: OKX_PASSPHRASE,
    baseUrl: process.env.OKX_BASE_URL || "https://web3.okx.com",
    syncSettle: false,
  });
  const rs = new x402ResourceServer(facilitatorClient).register(NETWORK, new ExactEvmScheme());
  resourceServer = rs;
  const withMirror = Object.fromEntries(
    Object.entries(ROUTES).map(([k, v]: any) => [
      k, { ...v, unpaidResponseBody: mirrorChallenge(v.accepts, v.description, v.mimeType) },
    ])
  );
  const httpServer = new x402HTTPResourceServer(rs, withMirror as any);
  httpServer.onProtectedRequest(async (ctx) => {
    const presented = ctx.adapter.getHeader("x-episteme-internal") || "";
    // Fail-closed: the ONLY unpaid bypass is a server-only secret (our own A2A daemon fulfilling a task
    // already paid via escrow), constant-time compared. Browser-forgeable headers (Origin / Referer /
    // Sec-Fetch-Site / Host) are NOT trusted — any HTTP client can forge them, which would hand the
    // OKX validator a paid result for free and fail x402 review.
    if (INTERNAL_SECRET.length > 0 && presented.length === INTERNAL_SECRET.length &&
        timingSafeEqual(Buffer.from(INTERNAL_SECRET), Buffer.from(presented))) {
      return { grantAccess: true };
    }

    // One authorization buys one delivery.
    //
    // EIP-3009 nonces stop the same authorization being SETTLED twice on-chain, but nothing stops a
    // buyer re-presenting an already-settled authorization to get the work again. Without this a
    // single $0.20 sim.run pays for unlimited sim.runs — verified against this deployment: one
    // authorization, three different inputs, three HTTP 200s.
    const nonce = readPaymentNonce(
      ctx.paymentHeader || ctx.adapter.getHeader("payment-signature") || ""
    );
    if (nonce) {
      if (CONSUMED_NONCES.has(nonce)) {
        // The SDK's hook contract allows only grantAccess or abort, so a spent authorization is
        // aborted with an explicit reason (403) rather than served. The client's remedy is to
        // request a fresh challenge and pay again, which the reason states.
        return {
          abort: true,
          reason: "payment_already_used: this authorization was already spent on a delivery. Each authorization is valid for exactly one call — request a new challenge and pay again.",
        };
      }
      // Marked before delivery: a crash mid-delivery must not hand out a free retry. The remedy for
      // a genuine server error is the error response, not an unmetered second attempt.
      rememberNonce(nonce);
    }
    return; // pay (standard 402)
  });

  /**
   * Turn a THROW during payment verification into a clean 402 re-challenge.
   *
   * A structurally valid but forged PAYMENT-SIGNATURE (correct base64 JSON, bogus signature) makes the
   * facilitator's verify step throw, and an uncaught throw reaches Hono as a bare
   * `500 Internal Server Error` — measured on all three of our ASPs before this guard. It fails CLOSED
   * (no free work is ever served), but 500 is the wrong answer to "your payment did not verify".
   *
   * Rather than hand-roll a challenge and risk drifting from the SDK's shape, re-enter the middleware
   * with the payment headers stripped: to the SDK the call is simply unpaid, so it emits its own
   * canonical 402 — PAYMENT-REQUIRED header and mirrored body both.
   *
   * A throw from the PAID handler downstream is rethrown untouched: that caller has paid, and a 402
   * would tell them to pay twice. Note this runs OUTSIDE the nonce bookkeeping above, so a forged
   * signature that never verified also never consumes a nonce.
   */
  const PAYMENT_HEADERS = ["payment-signature", "x-payment", "payment"];
  const guard = (inner: MiddlewareHandler): MiddlewareHandler => async (c, next) => {
    const method = c.req.method;
    const body = method === "GET" || method === "HEAD" ? undefined : await c.req.raw.clone().arrayBuffer();
    let entered = false;
    try {
      return await inner(c, async () => { entered = true; await next(); });
    } catch (e) {
      if (entered) throw e;
      console.warn(`[episteme-gateway] payment verification threw on ${new URL(c.req.url).pathname}: ${(e as Error)?.message ?? e}`);
      try {
        const headers = new Headers(c.req.raw.headers);
        for (const h of PAYMENT_HEADERS) headers.delete(h);
        c.req.raw = new Request(c.req.url, { method, headers, body });
        return await inner(c, async () => {
          throw new Error("payment middleware granted access to an unpaid re-challenge");
        });
      } catch {
        return c.json({ error: "invalid_payment",
                        detail: "PAYMENT-SIGNATURE did not verify. Retry with no payment header to get a fresh challenge." },
                      402);
      }
    }
  };
  return guard(paymentMiddlewareFromHTTPServer(httpServer));
}

const app = new Hono();

/**
 * CORS, mounted FIRST so the preflight short-circuits ahead of the paywall.
 *
 * A preflight carries no payment by definition, so a paywall that sees OPTIONS answers 402 — which
 * fails the preflight and makes the real request impossible from any browser-based or cross-origin
 * agent. Worse, without `Access-Control-Expose-Headers` a browser client cannot READ the
 * PAYMENT-REQUIRED header even on a successful 402, so it cannot construct a payment at all: the whole
 * x402 flow is invisible to it.
 *
 * Verified against a listed, transacting ASP (ShieldSuite #4959) which answers OPTIONS 204 and exposes
 * PAYMENT-REQUIRED / PAYMENT-RESPONSE. All three of ours returned 402 to a preflight with zero
 * access-control headers.
 */
const CORS_HEADERS: Record<string, string> = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "access-control-allow-headers":
    "Content-Type,Authorization,X-PAYMENT,PAYMENT-SIGNATURE,X-Request-Id",
  // Without this a browser can see the 402 but not the challenge inside it.
  "access-control-expose-headers":
    "PAYMENT-REQUIRED,PAYMENT-RESPONSE,X-PAYMENT-RESPONSE,X-Artifact-SHA256,Content-Disposition",
  "access-control-max-age": "86400",
};

app.use("*", async (c, next) => {
  if (c.req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  await next();
  for (const [k, v] of Object.entries(CORS_HEADERS)) {
    if (!k.startsWith("access-control-allow-methods") && !k.startsWith("access-control-max-age")) {
      c.res.headers.set(k, v);
    }
  }
});

if (OKX_PAY_ENABLED) {
  app.use("*", async (c, next) => {
    // Behind Caddy the request arrives as http (TLS terminated at the edge). Rewrite to https when the
    // edge saw https BEFORE the payment middleware reads the URL, so the challenge's resource.url is
    // https — as OKX requires.
    try {
      const proto = c.req.header("x-forwarded-proto") || "";
      if (proto.includes("https") && c.req.url.startsWith("http://")) {
        c.req.raw = new Request(c.req.url.replace(/^http:\/\//, "https://"), c.req.raw);
      }
    } catch { /* leave untouched on any edge case */ }
    await next();
  });
  app.use("*", buildOkxPayMiddleware());
}

/** Once access is granted (paid, free route, or internal), proxy straight to the Python runtime.
 *  Free/among-unprotected paths (/healthz, /nodes, /listing, /validate-listing, /a2a/*) are not in
 *  ROUTES, so they pass through without a paywall — /healthz and /a2a/message keep Gate No.2 fast. */
app.all("*", async (c) => {
  const url = new URL(c.req.url);
  const target = BACKEND + url.pathname + url.search;
  const method = c.req.method;
  // Bound the forward call so a slow/hung backend can never hang the paywall past the advertised x402
  // window (aborts → 502 instead of hanging OKX's test). 10s transit margin.
  const windowSec = (RAW as Record<string, RouteSpec>)[url.pathname]?.maxTimeoutSeconds ?? 300;
  const forwardTimeoutMs = Math.max(5_000, windowSec * 1000 - 10_000);
  // The Python runtime has its own x402 gate. By this point the OKX SDK has already verified AND
  // settled the payment, so we must tell the runtime "this one is paid" — otherwise it issues a
  // SECOND 402 and the caller is charged but never served (the exact failure OKX rejects for).
  // The secret is server-only and never forwarded from the client.
  const fwd: Record<string, string> = {
    "content-type": c.req.header("content-type") || "application/json",
  };
  if (INTERNAL_SECRET) fwd["x-episteme-internal"] = INTERNAL_SECRET;
  const init: RequestInit = {
    method,
    headers: fwd,
    body: method === "GET" || method === "HEAD" ? undefined : await c.req.arrayBuffer(),
    signal: AbortSignal.timeout(forwardTimeoutMs),
  };
  try {
    const r = await fetch(target, init);
    const headers = new Headers();
    // Forward the headers a caller actually needs. content-type alone is not enough: /artifact/:digest
    // serves a bought binary and its integrity digest, filename and cache policy all live in headers.
    // Dropping them turned a real download into an unnamed opaque blob with no way to verify it
    // matches the sha256 in the signed receipt.
    for (const h of ["content-type", "content-disposition", "cache-control",
                     "x-artifact-sha256", "content-length"]) {
      const v = r.headers.get(h);
      if (v) headers.set(h, v);
    }
    return new Response(r.body, { status: r.status, headers });
  } catch (e: any) {
    return c.json({ error: "backend_unreachable", detail: e?.message ?? String(e) }, 502);
  }
});

// With x402 ON, the paid hand-off to the Python runtime REQUIRES the shared secret. Without it the
// runtime re-challenges after we already settled the payment — the caller pays and still gets 402.
// That is worse than not starting, so refuse to boot rather than charge without delivering.
if (OKX_PAY_ENABLED && !INTERNAL_SECRET) {
  console.error("[episteme-gateway] REFUSING TO START — x402 is ON but EPISTEME_INTERNAL_SECRET is unset. " +
    "The Python runtime would issue a second 402 after payment settles (charged but not served). Set EPISTEME_INTERNAL_SECRET on BOTH processes.");
  process.exit(1);
}

const DEV_OPEN = process.env.EPISTEME_DEV_OPEN === "1";
if (!OKX_PAY_ENABLED && !DEV_OPEN) {
  // Fail-closed: never serve paid routes for free in production.
  console.error("[episteme-gateway] REFUSING TO START — x402 not fully configured and EPISTEME_DEV_OPEN is not set. " +
    "Set X402_ENABLED=1 + OKX_API_KEY/OKX_SECRET_KEY/OKX_PASSPHRASE + X402_PAY_TO for production, or EPISTEME_DEV_OPEN=1 for local dev.");
  process.exit(1);
}
const port = Number(process.env.GATEWAY_PORT || 8891);
// Default to loopback (correct when Caddy terminates TLS on the same host). In a container the
// platform routes to the pod IP, so GATEWAY_HOST=0.0.0.0 must be set or requests never arrive.
const hostname = process.env.GATEWAY_HOST || "127.0.0.1";
serve({ fetch: app.fetch, port, hostname });
console.log(`[episteme-gateway] listening on ${hostname}:${port} -> ${BACKEND} | x402=${OKX_PAY_ENABLED ? "ON" : "DEV-OPEN"} | routes=${Object.keys(ROUTES).length}`);
