"""OKX-compliant listing manifest generator.

Emits camelCase service objects (serviceName/serviceDescription/serviceType/fee/
endpoint) with 2-part descriptions, digit-string fees, and public https endpoints,
per OKX-ASP-LISTING-COMPLIANCE.md. Excludes internal verifier nodes.
"""
from __future__ import annotations

from config import Settings
from runtime import NodeRegistry

AGENT_NAME = "Episteme"  # EN 3-25, brand, no test/celebrity
AGENT_DESCRIPTION = (
    "Episteme turns any URL, file, dataset, repository, API or media into a clean, "
    "structured, independently verified artifact with a signed receipt."
)  # <=500, one sentence

# Curated, compliant service metadata (serviceName 5-30 noun phrase; 2-part description).
# Kept in sync with gateway._INTERNAL_ONLY: the only nodes excluded from the catalog are the
# second engines used for differential (L4) verification. Excluding by name rather than by a
# `.verify` suffix — the suffix match also hid `artifact.verify` and `receipt.verify`, which are
# real priced services (and `receipt.verify` is what lets a buyer check our own signatures).
_INTERNAL_ONLY = frozenset({"file.inspect.verify", "csv.profile.alt"})

_META: dict[str, dict] = {
    "file.inspect": {
        "serviceName": "File Inspect and Hash",
        "line1": "Detects file type, size and content hashes for any agent handling untrusted files.",
        "line2": "Provide: 1. file content (base64) or text.",
    },
    "hash.compute": {
        "serviceName": "Content Hash Compute",
        "line1": "Computes cryptographic digests of content for integrity and deduplication.",
        "line2": "Provide: 1. content (base64 or text) 2. algorithms.",
    },
    "artifact.verify": {
        "serviceName": "Artifact Hash Verify",
        "line1": "Verifies content against an expected hash and returns a match verdict.",
        "line2": "Provide: 1. content (base64) 2. expected sha256.",
    },
    "text.diff": {
        "serviceName": "Text Difference Report",
        "line1": "Produces a unified diff and similarity score between two texts.",
        "line2": "Provide: 1. text a 2. text b.",
    },
    "unit.convert": {
        "serviceName": "Unit Conversion",
        "line1": "Converts length, mass, time, data and temperature units deterministically.",
        "line2": "Provide: 1. value 2. from unit 3. to unit.",
    },
    "csv.profile": {
        "serviceName": "CSV Data Profile",
        "line1": "Profiles a CSV dataset with types, null counts, statistics and quality alerts.",
        "line2": "Provide: 1. csv content.",
    },
    "data.query_sql": {
        "serviceName": "CSV SQL Query",
        "line1": "Runs a read-only SQL query over a CSV dataset and returns rows.",
        "line2": "Provide: 1. csv content 2. sql select.",
    },
    "data.diff": {
        "serviceName": "Dataset Difference",
        "line1": "Compares two CSV datasets and reports added, removed and schema changes.",
        "line2": "Provide: 1. csv a 2. csv b.",
    },
    "url.to_markdown": {
        "serviceName": "Web Page to Markdown",
        "line1": "Extracts the main content of a web page as clean markdown for agents.",
        "line2": "Provide: 1. url or html.",
    },
    "page.links": {
        "serviceName": "Web Page Links",
        "line1": "Extracts and resolves all hyperlinks and anchor text from a web page.",
        "line2": "Provide: 1. url or html.",
    },
    # Priced, so it must be listed AND paywalled. Without curated copy here it was dropped from the
    # catalog, which also dropped it from the paywall table — so it served real work for free.
    "url.inspect": {
        "serviceName": "URL Reachability Check",
        # Avoid the literal "http" — the listing QA rule bans URL-looking text in descriptions.
        "line1": "Resolves a link and reports its final address, response code, content type, size and redirect count, without downloading the body.",
        "line2": "Provide: 1. url.",
    },
    "page.extract": {
        "serviceName": "Web Page Extract",
        "line1": "Extracts structured fields from a web page using caller CSS selectors.",
        "line2": "Provide: 1. url or html 2. selectors.",
    },
    "text.summarize": {
        "serviceName": "Text Summarize",
        "line1": "Summarizes long text into a concise summary for downstream agents.",
        "line2": "Provide: 1. text 2. maximum words.",
    },
    "document.to_markdown": {
        "serviceName": "Document to Markdown",
        "line1": "Converts PDF or text documents into clean markdown for downstream agents.",
        "line2": "Provide: 1. document content (base64) or text.",
    },
    "document.extract_json": {
        "serviceName": "Document to JSON",
        "line1": "Extracts caller-defined structured fields from a document as validated JSON.",
        "line2": "Provide: 1. document text 2. field schema.",
    },
    "document.chunk": {
        "serviceName": "Document Chunker",
        "line1": "Splits a document into overlapping, retrieval-ready chunks for grounded question answering.",
        "line2": "Provide: 1. text 2. chunk size 3. overlap.",
    },
    "openapi.inspect": {
        "serviceName": "OpenAPI Spec Inspect",
        "line1": "Inventories operations, paths and security schemes from an OpenAPI specification.",
        "line2": "Provide: 1. openapi spec (JSON).",
    },
    "data.transform_json": {
        "serviceName": "JSON Transform",
        "line1": "Applies deterministic transforms such as pick, select, flatten and keys to JSON data.",
        "line2": "Provide: 1. data (JSON) 2. operation.",
    },
    "openapi.lint": {
        "serviceName": "OpenAPI Lint",
        "line1": "Lints an OpenAPI specification for missing titles, operation ids, responses and descriptions.",
        "line2": "Provide: 1. openapi spec (JSON or YAML).",
    },
    "openapi.diff": {
        "serviceName": "OpenAPI Diff",
        "line1": "Compares two OpenAPI specs and flags added, removed and breaking operations.",
        "line2": "Provide: 1. old spec 2. new spec.",
    },
    "schema.generate": {
        "serviceName": "JSON Schema Generate",
        "line1": "Infers a JSON Schema from an example JSON value for validation and contracts.",
        "line2": "Provide: 1. example (JSON value).",
    },
    "image.inspect": {
        "serviceName": "Image Inspect",
        "line1": "Reports image format, dimensions, mode and EXIF metadata with a content hash.",
        "line2": "Provide: 1. image content (base64).",
    },
    "image.transform": {
        "serviceName": "Image Transform",
        "line1": "Resizes, converts, rotates or grayscales an image and returns a hashed artifact.",
        "line2": "Provide: 1. image (base64) 2. operation 3. target format.",
    },
    "pdf.manipulate": {
        "serviceName": "PDF Manipulate",
        "line1": "Reads PDF info or extracts, rotates and strips metadata from pages into a new document.",
        "line2": "Provide: 1. pdf content (base64) 2. operation 3. pages.",
    },
    "document.compare": {
        "serviceName": "Document Compare",
        "line1": "Produces a redline and change statistics between two document texts.",
        "line2": "Provide: 1. text a 2. text b.",
    },
    "data.convert": {
        "serviceName": "Tabular Data Convert",
        "line1": "Converts tabular data between CSV, JSON and JSONL formats deterministically.",
        "line2": "Provide: 1. csv or json or jsonl 2. target format.",
    },
    "data.validate": {
        "serviceName": "Data Schema Validate",
        "line1": "Validates rows against a JSON Schema and returns per-row errors and a pass rate.",
        "line2": "Provide: 1. rows or csv 2. json schema.",
    },
    "data.stats": {
        "serviceName": "Numeric Data Stats",
        "line1": "Computes count, min, max, mean, standard deviation and median per numeric column.",
        "line2": "Provide: 1. rows or csv.",
    },
    "data.dedupe": {
        "serviceName": "Row Deduplication",
        "line1": "Finds and removes duplicate rows by key or full match and returns duplicate clusters.",
        "line2": "Provide: 1. rows or csv 2. key fields.",
    },
    "chart.spec": {
        "serviceName": "Chart Spec Builder",
        "line1": "Builds a valid Vega-Lite chart specification from tabular data and field mappings.",
        "line2": "Provide: 1. rows 2. x field 3. y field 4. mark.",
    },
    "repo.map": {
        "serviceName": "Repository Map",
        "line1": "Maps a codebase into languages, lines of code, symbols and dependencies for agents.",
        "line2": "Provide: 1. files as a path to content map.",
    },
    "repo.lint": {
        "serviceName": "Repository Lint",
        "line1": "Checks source files for syntax errors, long lines, tabs and unresolved markers.",
        "line2": "Provide: 1. files as a path to content map.",
    },
    "repo.scan_secrets": {
        "serviceName": "Secret Scanner",
        "line1": "Scans code and text for leaked keys and tokens and returns redacted findings only.",
        "line2": "Provide: 1. files as a path to content map, or text.",
    },
    "workflow.compose": {
        "serviceName": "Verified Workflow Compose",
        "line1": "Chains multiple verified nodes into one artifact graph with per node signed receipts.",
        "line2": "Provide: 1. steps as an ordered list of endpoint and input.",
    },
    "sim.run": {
        "serviceName": "Scenario Foresight Sim",
        "line1": "Runs a synthetic multi-agent scenario simulation with optional counterfactual divergence.",
        "line2": "Provide: 1. topic 2. population 3. rounds 4. intervention strength.",
    },
    "document.redact_pii": {
        "serviceName": "PII Redaction",
        "line1": "Detects and redacts emails, phones, SSNs, cards and IPs from text for safe agent use.",
        "line2": "Provide: 1. text.",
    },
    "email.validate": {
        "serviceName": "Email Validate",
        "line1": "Checks email syntax, disposable and role addresses and gives a deliverability guess.",
        "line2": "Provide: 1. email address.",
    },
    "data.join": {
        "serviceName": "Dataset Join",
        "line1": "Joins two record sets on a shared key with inner or left semantics.",
        "line2": "Provide: 1. left rows 2. right rows 3. join key 4. how.",
    },
    "data.clean": {
        "serviceName": "Data Clean",
        "line1": "Trims, collapses whitespace, lowercases and drops empty fields with a change log.",
        "line2": "Provide: 1. rows 2. operations.",
    },
    "object.diff": {
        "serviceName": "JSON Object Diff",
        "line1": "Computes added, removed and changed paths between two JSON values.",
        "line2": "Provide: 1. object a 2. object b.",
    },
    "site.map": {
        "serviceName": "Sitemap Extract",
        "line1": "Extracts and de-duplicates URLs from a sitemap or web page for crawl planning.",
        "line2": "Provide: 1. sitemap xml or html.",
    },
    "api.to_mcp": {
        "serviceName": "OpenAPI to MCP",
        "line1": "Generates typed MCP tool definitions from an OpenAPI specification.",
        "line2": "Provide: 1. openapi spec.",
    },
    "mcp.validate": {
        "serviceName": "MCP Tools Validate",
        "line1": "Validates an MCP tools list for names, descriptions and input schemas.",
        "line2": "Provide: 1. tools list.",
    },
    "text.stats": {
        "serviceName": "Text Statistics",
        "line1": "Counts characters, words, sentences and lines and estimates reading time.",
        "line2": "Provide: 1. text.",
    },
    "csv.to_table": {
        "serviceName": "CSV to Table",
        "line1": "Renders a CSV dataset as a clean Markdown or grid table for reports and agents.",
        "line2": "Provide: 1. csv 2. table format.",
    },
    "receipt.verify": {
        "serviceName": "Receipt Verify",
        "line1": "Independently verifies an Episteme signed receipt and its manifest for tamper detection.",
        "line2": "Provide: 1. envelope object.",
    },
    "data.pivot": {
        "serviceName": "Data Pivot Aggregate",
        "line1": "Groups rows by fields and aggregates with count, sum, average, min and max.",
        "line2": "Provide: 1. rows 2. group by fields 3. aggregations.",
    },
    "robots.check": {
        "serviceName": "Robots Policy Check",
        "line1": "Parses a robots.txt and decides whether a path is crawlable for a user agent.",
        "line2": "Provide: 1. robots.txt content 2. path 3. user agent.",
    },
}


def _fee_str(price: float) -> str:
    if not price:
        return "0"
    return f"{price:.6f}".rstrip("0").rstrip(".")


def build_listing(registry: NodeRegistry, settings: Settings) -> dict:
    base = settings.public_base_url.rstrip("/")
    services = []
    for n in registry.list():
        ep = n["endpoint"]
        if ep in _INTERNAL_ONLY:
            continue  # second-engine differential verifiers — never sold, never routable
        meta = _META.get(ep)
        if not meta:
            continue  # only list curated, compliant services
        services.append({
            "serviceName": meta["serviceName"],
            "serviceDescription": f"{meta['line1']}\n{meta['line2']}",
            "serviceType": n.get("asp_type", "A2MCP"),
            "fee": _fee_str(n["price_usdt"]),
            "endpoint": f"{base}/a2mcp/{ep}",
        })
    return {
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "role": "asp",
        "avatarNote": "Upload a brand logo image FILE (<=1MB, PNG/JPEG/WebP, non-face) — required for ASP.",
        "services": services,
    }



_FORBIDDEN = ("http://", "https://", "github", "e.g.", "example:", "disclaimer",
              "not financial advice", "```")


def validate_listing(manifest: dict) -> dict:
    """Mirror OKX `validate-listing` QA locally. Returns {pass, issues:[{severity,field,issue}]}. """
    issues = []

    def bad(field, issue, severity="block"):
        issues.append({"severity": severity, "field": field, "issue": issue})

    name = manifest.get("name", "")
    if not (3 <= len(name) <= 25):
        bad("name", "EN name must be 3-25 chars")
    if any(w in name.lower() for w in ("test", "dev", "beta", "staging", "demo")):
        bad("name", "no environment/test markers in name")

    desc = manifest.get("description", "")
    if not desc or len(desc) > 500:
        bad("description", "required, <=500 chars")

    services = manifest.get("services", [])
    if not services:
        bad("services", "at least one service required")
    seen_names = set()
    for i, s in enumerate(services):
        need = {"serviceName", "serviceDescription", "serviceType", "fee", "endpoint"}
        missing = need - set(s)
        if missing:
            bad(f"services[{i}]", f"missing keys {sorted(missing)}")
            continue
        sn = s["serviceName"]
        if not (5 <= len(sn) <= 30):
            bad(f"services[{i}].serviceName", "5-30 chars")
        if sn == name:
            bad(f"services[{i}].serviceName", "must differ from agent name")
        if sn in seen_names:
            bad(f"services[{i}].serviceName", "duplicate service name")
        seen_names.add(sn)
        if s["serviceType"] not in ("A2MCP", "A2A"):
            bad(f"services[{i}].serviceType", "must be A2MCP or A2A")
        fee = str(s["fee"])
        if not fee.replace(".", "", 1).isdigit():
            bad(f"services[{i}].fee", "digits-only string (no currency word)")
        elif "." in fee and len(fee.split(".")[1]) > 6:
            bad(f"services[{i}].fee", "max 6 decimals")
        sd = s["serviceDescription"]
        parts = sd.split("\n")
        if len(parts) < 2:
            bad(f"services[{i}].serviceDescription", "must be 2-part (capability + inputs)")
        else:
            if len(parts[0]) > 200 or len(parts[1]) > 200:
                bad(f"services[{i}].serviceDescription", "each part <=200 chars")
        if len(sd) > 400:
            bad(f"services[{i}].serviceDescription", "total <=400 chars")
        low = sd.lower()
        for f in _FORBIDDEN:
            if f in low:
                bad(f"services[{i}].serviceDescription", f"remove '{f.strip()}' (links/examples/disclaimers)")
        if s["serviceType"] == "A2MCP":
            ep = s["endpoint"]
            if not ep.startswith("https://") or len(ep) > 512:
                bad(f"services[{i}].endpoint", "A2MCP needs public https:// endpoint <=512 chars")
            for bad_host in ("localhost", "127.0.0.1", "://10.", "://192.168.", "://172.16.",
                             ".local", ".internal", "://0.0.0.0"):
                if bad_host in ep:
                    bad(f"services[{i}].endpoint", f"reject pattern '{bad_host}'")

    blocks = [x for x in issues if x["severity"] == "block"]
    return {"pass": len(blocks) == 0, "block_count": len(blocks),
            "issue_count": len(issues), "issues": issues}
