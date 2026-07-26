# Episteme x402 correctness audit (vs official + listed ASPs)

Verified 2026-07-24 via GitHub code search. Goal: confirm our x402 constants/shape are not wrong before submitting for review (the #1 reject reason).

## Constants — CONFIRMED canonical
Our `x402.py` / `config.py` use `network = eip155:196` and `asset = 0x779ded0c9e1022225f8e0630b35a9b54be713736` (USDT0). Cross-check (code search hit counts):
- **USDT0 `0x779Ded…3736`** → 1058 hits, including **okx/xlayer-docs**, **x402-foundation/x402** (the spec), **MetaMask/contract-metadata** at path `metadata/eip155:196/erc20:0x779Ded0c9e1022225f8E0630b35a9b54bE713736.json` (this file literally binds the address to eip155:196 = X Layer), Uniswap XLayer config, tether `wdk-docs/ai/x402.mdx`, Everdawn USDT0 deployment reports.
- **`eip155:196`** → 1188 hits, including **okx/onchainos-skills `cli/src/payment_cache.rs`** (OFFICIAL OKX skills), nansen-cli `x402.js`, quiknode `x402-rails/chains.rb`, OpenFacilitator, nuwa-protocol/x402-exec.

**Conclusion:** our chain + token constants match the official OKX skill code, the x402 foundation, and MetaMask's canonical registry. No constant error.

## Shape — follows x402 v2
Our challenge emits: `402` + **`PAYMENT-REQUIRED` header** whose base64 value decodes to `{x402Version:2, resource:{url,description,mimeType}, accepts:[{scheme:"exact", network:"eip155:196", asset, amount(min-units, 6dec), payTo, maxTimeoutSeconds, extra:{name,version}}], nonce}`. This matches the shape in the OKX A2MCP guide and x402-foundation/x402. Self-probe (`selfprobe.py`) asserts every one of these fields; `tests/test_x402.py` + `tests/test_gateway.py` assert the exact values.

## Minor note (non-blocking)
Canonical address is often shown checksummed (`0x779Ded0c9e1022225f8E0630b35a9b54bE713736`); we store lowercase (as OKX's own listing doc specified). Ethereum addresses are case-insensitive; both resolve to the same token. If a reviewer does a case-sensitive exact match, switch to the checksummed form — trivial config change (`EPISTEME_ASSET`).

## Remaining real-submit gaps (environmental, not code)
1. `payTo` must be set to a real X Layer receive wallet (`EPISTEME_PAYTO`).
2. `public_base_url` must be the real deployed https host (`EPISTEME_PUBLIC_BASE_URL`).
3. Production settlement: set `EPISTEME_X402_MODE=facilitator` + the OKX seller SDK / facilitator URL (local uses cryptographic dev-signature verification).
4. Deploy on a public https host kept **online during review** (gate No.2).
