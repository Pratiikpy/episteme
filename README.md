# Episteme

**The result, and the evidence that it is correct.**

Forty-eight deterministic services an agent can call mid-task — parse a document, profile a dataset, scan
a repository for secrets, redact PII — each returning the deliverable together with the named checks that
vouch for it and an Ed25519 signature over the whole artifact.

![License](https://img.shields.io/badge/license-MIT-green)
![Network](https://img.shields.io/badge/network-X_Layer_(eip155%3A196)-black)
![Payments](https://img.shields.io/badge/payments-x402_·_USD₮0-9A6E1E)
![OKX.AI](https://img.shields.io/badge/OKX.AI-agent_%239165-2E7A57)

**Live:** [episteme.blacksky-e393132e.centralindia.azurecontainerapps.io](https://episteme.blacksky-e393132e.centralindia.azurecontainerapps.io)
· **Proof — real outputs and settlement hashes:** [/proof](https://episteme.blacksky-e393132e.centralindia.azurecontainerapps.io/proof)
· **OKX.AI:** agent #9165
· **Product page:** [Notion](https://comfortable-goal-205.notion.site/Episteme-3a99c0ce787681c898c4de2bf1134890) · **All three:** [Hub](https://comfortable-goal-205.notion.site/OKX-AI-Genesis-Hackathon-Aletheia-Reach-Episteme-3a99c0ce78768104958be46465e840dd)

---

## The problem

An agent doing real work spends most of its time on unglamorous tasks: parse this document, profile this
CSV, scan this repository for secrets, redact the personal data before it reaches a model.

Each is small, and each is a place to be quietly wrong. A parser drops a column. A cleaner reports it
lower-cased your strings and did not. The agent does not notice — it takes the output and builds on it,
and by the time anything looks wrong the error is three steps upstream and invisible.

There is no shortage of tools for these jobs. What is missing is any way for the caller to know the job
was actually done.

## The idea

**ἐπιστήμη** is Plato's word for knowledge that is *justified*, as against opinion that happens to be
right. That distinction is the product.

Every service returns the same envelope, whatever it does — so an agent writes **one** verification
routine and it works for all forty-eight tools:

```jsonc
{
  "result":       { … },                    // the deliverable
  "validation": {
    "level":  "L4_INDEPENDENTLY_VERIFIED",   // how far it was checked
    "tests":  [ { "name": "lowercase_applied", "passed": true, "detail": "…" } ]
  },
  "input_hashes": ["sha256:…"],              // content-addressed
  "output_hash":  "sha256:…",
  "replay":       { "environment": "python-3.11.15", "tool_versions": { "pillow": "11.0.0" } },
  "receipt":      { "algo": "ed25519", "signature": "…", "public_key": "…" }
}
```

**The levels are a claim about rigour, not a rating.**

| Level | What it means |
| --- | --- |
| `L1_PRODUCED` | A result was produced |
| `L2_SCHEMA_VALIDATED` | It conforms to the declared shape |
| `L3_REPRODUCED` | Re-running the same input produced the same digest |
| `L4_INDEPENDENTLY_VERIFIED` | A second, independent engine produced an agreeing result |

## The decision that shaped everything

**A check must assert the work, not its shape.**

`data.clean` was once asked to lower-case some strings. It silently didn't, echoed the operation back as
though it had, passed its validation check — which only confirmed a `rows` key existed — and signed the
whole thing. The buyer received an attested claim that work had been done which had not.

That is the worst failure a service like this can have, and it was invisible from the outside. So the
checks changed: ask for lower-case and the check fails unless every returned string really *is*
lower-case. An operation we do not recognise is now a hard error rather than a silent skip, because
signing "I did what you asked" is only defensible if unknown instructions fail loudly.

The same principle removed two more quiet lies: the replay capsule reported a placeholder version for
every dependency — including `pillow: "1.0"`, a version that has never existed — and one service derived
its column order from an unordered set, so the same input could produce a different signed digest between
runs on a service declared deterministic.

## Try it

```bash
# Unpaid call → 402 with the challenge in the PAYMENT-REQUIRED header
curl -i -X POST https://episteme.blacksky-e393132e.centralindia.azurecontainerapps.io/a2mcp/text.stats \
  -H 'Content-Type: application/json' -d '{}'

# Sign the challenge (EIP-3009, USD₮0 on X Layer) and replay it
curl -X POST .../a2mcp/text.stats \
  -H 'PAYMENT-SIGNATURE: <signed authorization>' \
  -H 'Content-Type: application/json' -d '{"input":{"text":"hello world"}}'
```

Two behaviours worth knowing:

- An **empty body** returns that endpoint's input contract with a worked example, not an error.
- A **wrong** request returns what the endpoint actually does, a request that would work, and
  `did_you_mean` siblings. Buyers land on the wrong endpoint — usually whichever is listed first — and
  payment has already settled by the time they find out. Ask a file-hashing tool to pivot a CSV and it
  points you at `data.pivot`.

Machine-readable schemas for every service: [`/nodes`](https://episteme.blacksky-e393132e.centralindia.azurecontainerapps.io/nodes)
and `/nodes/<endpoint>/schema`.

## Services — 48 paid, plus 1 A2A

**Documents and text** — `document.to_markdown` · `document.chunk` · `document.compare` ·
`document.extract_json` · `document.redact_pii` · `page.extract` · `page.links` · `pdf.manipulate` ·
`text.diff` · `text.stats` · `text.summarize`

**Data and tables** — `csv.profile` · `csv.to_table` · `data.clean` · `data.convert` · `data.dedupe` ·
`data.diff` · `data.join` · `data.pivot` · `data.query_sql` · `data.stats` · `data.transform_json` ·
`data.validate` · `chart.spec`

**Files and integrity** — `file.inspect` · `hash.compute` · `artifact.verify` · `receipt.verify` ·
`image.inspect` · `image.transform`

**Code and repositories** — `repo.map` · `repo.lint` · `repo.scan_secrets` · `mcp.validate` ·
`api.to_mcp` · `openapi.inspect` · `openapi.lint` · `openapi.diff` · `schema.generate`

**Web and network** — `url.inspect` · `url.to_markdown` · `site.map` · `robots.check` · `email.validate`

**Reasoning and workflows** — `workflow.compose` · `sim.run` · `unit.convert`

Prices run **$0.001 – $0.20**. Two internal differential verifiers (`file.inspect.verify`,
`csv.profile.alt`) exist only to push results to L4 and are never callable directly.

## How it is built

```
Node gateway (gateway/server.ts) — OKX x402 seller SDK
  verifies + settles payment, then hands off over a constant-time internal secret
        │                    (so a paid call is never challenged twice)
        ▼
Python runtime (runtime.py)
  run node → validate (L2) → reproduce (L3) → differential verify (L4)
           → replay capsule → Ed25519 signed receipt
        │
  nodes/*  — each: primary engine + its own validation checks (+ optional independent verifier)
```

- `contract.py` — the Universal Artifact Contract, verification levels, error codes
- `runtime.py` — canonical hashing, signing, the node registry and executor
- `x402.py` — the exact x402 v2 challenge shape, verification, single-use replay protection
- `listing.py` — the OKX-compliant listing manifest
- `selfprobe.py` — reviewer simulation: unpaid → 402 shape, paid → 200 deliverable

AI-backed services are flagged `x-ai-backed` in their schema, carry an explicit disclosure, and are capped
below the deterministic tiers — a language model cannot promise reproducibility and we do not pretend
otherwise.

## Verify a receipt yourself

```bash
# The published signing key — compare it against receipt.public_key
curl .../.well-known/episteme-signing-key
```

Then verify `receipt.signature` over `receipt.manifest_sha256`. Signature validity alone is a weak claim:
it only says the receipt is self-consistent with whatever key the receipt itself carries, and anyone can
generate a keypair. **Attribution requires comparing the key against the one published out of band** —
which is why that endpoint exists. `receipt.verify` does both checks for you.

## Security

Every input is treated as hostile: SSRF and DNS-rebind guards on anything that fetches, size and time
caps, MIME sniffing, read-only SQL, no code evaluation. Secrets are referenced by name and never logged.
Payment is fail-closed — a priced node pays at the runtime gate even if the route table missed it, so a
configuration gap degrades to "charged twice" rather than "given away".

## What it does not do

- **A receipt proves provenance, not truth.** It says this output came from this input through this
  engine, unaltered. It does not say the answer is wise.
- **AI-backed services cannot reach L3 or L4**, and are marked as such in their schema.
- **Artifacts expire.** Produced files are retained for a bounded period; the digest inside the receipt is
  the durable part.
- **Differential disagreement is surfaced as a warning**, never hidden or averaged away.

## Development

```bash
pip install -r requirements.txt
python -m pytest -q                              # 230 tests
uvicorn gateway:app --host 0.0.0.0 --port 8080   # serve

# reviewer simulation — must be True before submitting
python -c "from fastapi.testclient import TestClient; from gateway import create_app; \
import selfprobe; print(selfprobe.run_all(TestClient(create_app()))['passed'])"
```

x402 constants, verified canonical: `network=eip155:196` · `asset=0x779ded0c9e1022225f8e0630b35a9b54be713736`
(USD₮0) · `x402Version=2` · `scheme=exact` · amount = fee × 10⁶. See `AUDIT.md`.

MIT licensed.
