"""Code pack: repo.map, repo.lint, repo.scan_secrets. Input: {"files": {path: content}}."""
from __future__ import annotations

import ast
import math
import re
from collections import Counter

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

_EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".cs": "C#", ".php": "PHP",
    ".sh": "Shell", ".sql": "SQL", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".md": "Markdown", ".html": "HTML", ".css": "CSS", ".kt": "Kotlin", ".swift": "Swift",
}


def _files(inp: dict) -> dict:
    files = inp.get("files")
    if isinstance(files, dict) and files:
        return {str(k): str(v) for k, v in files.items()}
    if "text" in inp and "path" in inp:
        return {str(inp["path"]): str(inp["text"])}
    raise NodeError(ErrorCode.INVALID_INPUT, "provide 'files' {path: content}")


def _lang(path: str) -> str:
    for ext, lang in _EXT_LANG.items():
        if path.lower().endswith(ext):
            return lang
    return "Other"


class RepoMapNode(Node):
    name = "repo.map"
    price_usdt = 0.02
    deterministic = True
    engine = "episteme-repomap"

    def run(self, ctx: NodeContext) -> dict:
        files = _files(ctx.input)
        langs: dict[str, dict] = {}
        total_loc = 0
        symbols = {"functions": 0, "classes": 0}
        imports: set[str] = set()
        file_rows = []
        for path, content in files.items():
            lang = _lang(path)
            loc = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            total_loc += loc
            d = langs.setdefault(lang, {"files": 0, "loc": 0})
            d["files"] += 1
            d["loc"] += loc
            fn = cl = 0
            if lang == "Python":
                try:
                    tree = ast.parse(content)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            fn += 1
                        elif isinstance(node, ast.ClassDef):
                            cl += 1
                        elif isinstance(node, ast.Import):
                            for a in node.names:
                                imports.add(a.name.split(".")[0])
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            imports.add(node.module.split(".")[0])
                except SyntaxError:
                    fn += len(re.findall(r"^\s*def\s+\w+", content, re.M))
                    cl += len(re.findall(r"^\s*class\s+\w+", content, re.M))
            else:
                fn += len(re.findall(r"\b(function|func|def|fn)\s+\w+", content))
                cl += len(re.findall(r"\bclass\s+\w+", content))
            symbols["functions"] += fn
            symbols["classes"] += cl
            file_rows.append({"path": path, "language": lang, "loc": loc,
                              "functions": fn, "classes": cl})
        return {
            "file_count": len(files), "total_loc": total_loc,
            "languages": langs, "symbols": symbols,
            "dependencies": sorted(imports)[:200],
            "files": file_rows[:1000],
        }

    def validate(self, result, ctx):
        return [ValidationCheck(name="mapped", passed=result.get("file_count", 0) >= 1)]


class RepoLintNode(Node):
    name = "repo.lint"
    price_usdt = 0.02
    deterministic = True
    engine = "episteme-lint(ast)"

    def run(self, ctx: NodeContext) -> dict:
        files = _files(ctx.input)
        findings = []

        def add(sev, path, line, rule, msg):
            findings.append({"severity": sev, "path": path, "line": line, "rule": rule, "message": msg})

        for path, content in files.items():
            if path.endswith(".py"):
                try:
                    ast.parse(content)
                except SyntaxError as e:
                    add("error", path, e.lineno or 0, "python-syntax", e.msg)
            for i, line in enumerate(content.splitlines(), 1):
                if len(line) > 120:
                    add("warn", path, i, "line-too-long", f"{len(line)} > 120 chars")
                if line != line.rstrip():
                    add("info", path, i, "trailing-whitespace", "trailing whitespace")
                if "\t" in line:
                    add("info", path, i, "tab-indent", "tab character")
                if re.search(r"\b(TODO|FIXME|XXX)\b", line):
                    add("info", path, i, "todo", "unresolved marker")
        errors = sum(1 for f in findings if f["severity"] == "error")
        return {"file_count": len(files), "finding_count": len(findings),
                "error_count": errors,
                "warning_count": sum(1 for f in findings if f["severity"] == "warn"),
                "info_count": sum(1 for f in findings if f["severity"] == "info"),
                "findings": findings[:1000], "passed": errors == 0}

    def validate(self, result, ctx):
        return [ValidationCheck(name="linted", passed="findings" in result)]


# secret patterns — findings are REDACTED (never expose the secret value)
# Secret-detection rules, most specific first. A scanner is only worth paying for if it does not
# MISS things, so the provider-prefix rules below are deliberately exhaustive and the generic rules
# below them catch unknown providers by shape and entropy.
#
# Three real false negatives this rule set exists to close:
#   1. `sk-[A-Za-z0-9]{20,}` cannot match `sk-proj-…` or `sk-ant-…` — the hyphen is not in the
#      character class, so every modern OpenAI/Anthropic key walked straight past the scanner.
#   2. The generic rule required the value to be QUOTED, so `API_KEY=abc123def456` — i.e. the
#      entire contents of a typical .env file, the single most common way credentials leak — matched
#      nothing at all.
#   3. Matching stopped at the first hit per rule per line, so two keys on one line reported one.
_SECRET_PATTERNS = [
    # --- cloud / infrastructure ---
    ("aws-access-key", re.compile(r"(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}")),
    ("aws-secret-key", re.compile(r"(?i)aws_?secret_?access_?key\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})")),
    ("gcp-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("gcp-service-account", re.compile(r'"type"\s*:\s*"service_account"')),
    ("azure-storage-key", re.compile(r"(?i)AccountKey\s*=\s*[A-Za-z0-9/+=]{64,}")),
    ("digitalocean-token", re.compile(r"do[oprs]_v1_[a-f0-9]{64}")),
    # --- version control ---
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("github-fine-grained", re.compile(r"github_pat_[A-Za-z0-9_]{22,}")),
    ("gitlab-token", re.compile(r"glpat-[A-Za-z0-9_-]{16,}")),
    # --- AI providers (the sk- family; hyphens and underscores ARE part of these keys) ---
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-project-key", re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"sk-(?!ant-|proj-|live_|test_)[A-Za-z0-9_-]{20,}")),
    ("huggingface-token", re.compile(r"hf_[A-Za-z0-9]{30,}")),
    ("openrouter-key", re.compile(r"sk-or-v1-[a-f0-9]{40,}")),
    # --- payments (highest blast radius) ---
    ("stripe-live-secret", re.compile(r"[sr]k_live_[A-Za-z0-9]{16,}")),
    ("stripe-test-secret", re.compile(r"[sr]k_test_[A-Za-z0-9]{16,}")),
    # --- messaging / email ---
    ("slack-token", re.compile(r"xox[baprse]-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"hooks\.slack\.com/services/T[A-Za-z0-9_/+-]{20,}")),
    ("sendgrid-key", re.compile(r"SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")),
    ("twilio-account-sid", re.compile(r"AC[a-f0-9]{32}")),
    ("discord-bot-token", re.compile(r"[MNO][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{32,}")),
    # --- package registries ---
    ("npm-token", re.compile(r"npm_[A-Za-z0-9]{36}")),
    ("pypi-token", re.compile(r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}")),
    # --- keys and credentials by structure ---
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Any variable holding 0x + exactly 64 hex is a 32-byte secret — an EVM private key or a raw
    # signing key. The name is not the signal and must not be required: `KEY=0x3da0…` was missed
    # because the rule insisted on seeing the word "private". A bare hash literal with no assignment
    # is deliberately NOT matched, since transaction and block hashes have the same shape and are
    # public — the `name = value` form is what makes it a stored credential.
    ("hex-private-key",
     re.compile(r"(?i)[A-Za-z0-9_]*(?:key|secret|mnemonic|seed|pk|priv)[A-Za-z0-9_]*"
                r"\s*[=:]\s*['\"]?(0x[a-f0-9]{64})\b")),
    # Connection strings carry the password inline and are routinely pasted into configs and logs.
    ("db-uri-with-password",
     re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)://"
                r"[^\s:@/]+:[^\s:@/]{4,}@[^\s/]+")),
    # --- generic: named assignment, QUOTED OR BARE (bare is the .env case) ---
    # The name part must tolerate a prefix: `DO_TOKEN`, `AWS_SECRET` and `MY_API_KEY` are the normal
    # way people name these, and a leading `\b` never matches inside them because `_` is itself a
    # word character — so the single most common naming convention defeated the rule entirely.
    ("generic-secret-assignment",
     re.compile(r"(?i)(?:^|[^A-Za-z0-9])(?:[A-Za-z0-9]+_)*"
                r"(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|"
                r"client[_-]?secret|private[_-]?token|passwd|password|secret|token|credential)"
                r"[A-Za-z0-9_]*\s*[=:]\s*(?:['\"]([^'\"\s]{8,})['\"]|([A-Za-z0-9_./+=~-]{12,}))")),
    ("authorization-header",
     re.compile(r"(?i)authorization\s*[=:]\s*['\"]?(?:bearer|basic)\s+[A-Za-z0-9_./+=-]{12,}")),
]

# Values that look like secrets structurally but are obviously not real. Reporting these as findings
# trains a buyer to ignore the tool, which is its own kind of failure.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{4,}|y{4,}|\.{3,}|<[^>]*>|\{\{?[^}]*\}?\}|\$\{[^}]*\}|%[a-z_]+%|"
    r"(?:your|my|the|some|test|fake|dummy|sample|example|placeholder|changeme|change_me|redacted|"
    r"insert|replace|todo|tbd|none|null|nil|empty|not_?set|xxx)"
    r"[-_a-z0-9]*(?:key|token|secret|password|here|value)?[-_a-z0-9]*|"
    r"[a-f0-9]{0}|0+|1234567890\d*)$"
)


def _shannon_entropy(s: str) -> float:
    """Bits of entropy per character. Random credentials sit high (>3.5); English prose and
    identifiers sit low (<3.0). Used only to grade generic assignments, never to reject a
    provider-prefix match — those are certain regardless of how the value scores."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_placeholder(value: str) -> bool:
    v = value.strip().strip("'\"")
    if not v or len(v) < 8:
        return True
    if _PLACEHOLDER.match(v):
        return True
    # A single repeated character ("aaaaaaaaaaaa") is never a real credential.
    return len(set(v)) <= 2


def _redact(m: str) -> str:
    m = m.strip("'\"")
    if len(m) <= 8:
        return "****"
    return m[:4] + "…" + m[-4:]


class RepoScanSecretsNode(Node):
    name = "repo.scan_secrets"
    price_usdt = 0.02
    deterministic = True
    engine = "episteme-secretscan"

    def run(self, ctx: NodeContext) -> dict:
        try:
            files = _files(ctx.input)
        except NodeError:
            if "text" in ctx.input:
                files = {"input.txt": str(ctx.input["text"])}
            else:
                raise
        findings: list[dict] = []
        skipped_placeholders = 0
        for path, content in files.items():
            for i, line in enumerate(content.splitlines(), 1):
                # Track the spans already claimed on this line so a specific provider rule wins over
                # the generic assignment rule instead of reporting the same credential twice.
                claimed: list[tuple[int, int]] = []
                for rule, pat in _SECRET_PATTERNS:
                    # finditer, not search: a config line can legitimately hold two credentials, and
                    # stopping at the first match silently dropped the rest.
                    for m in pat.finditer(line):
                        start, end = m.span()
                        if any(start < ce and cs < end for cs, ce in claimed):
                            continue
                        # Prefer the captured value over the whole match so the rule name and the
                        # surrounding `KEY=` text are not mistaken for part of the secret.
                        value = next((g for g in m.groups() if g), m.group(0))
                        generic = rule.startswith("generic-")
                        if generic:
                            if _looks_placeholder(value):
                                skipped_placeholders += 1
                                continue
                            # Only entropy separates a real generic credential from a config word
                            # like `password: postgres`. Provider-prefix rules skip this check —
                            # `AKIA…` is a secret whatever it scores.
                            if _shannon_entropy(value) < 3.0:
                                skipped_placeholders += 1
                                continue
                        claimed.append((start, end))
                        findings.append({
                            "path": path, "line": i, "rule": rule,
                            "redacted": _redact(value),  # the value itself is never returned
                            "confidence": "medium" if generic else "high",
                            "entropy_bits_per_char": round(_shannon_entropy(value), 2),
                        })
        by_rule = Counter(f["rule"] for f in findings)
        return {
            "file_count": len(files),
            "secret_count": len(findings),
            "findings": findings[:500],
            "has_secrets": bool(findings),
            "findings_by_rule": dict(by_rule),
            "high_confidence_count": sum(1 for f in findings if f["confidence"] == "high"),
            "rules_applied": len(_SECRET_PATTERNS),
            "placeholders_ignored": skipped_placeholders,
            "truncated": len(findings) > 500,
        }

    def validate(self, result, ctx):
        findings = result.get("findings", [])
        return [
            # Never leak: assert no full-length secret appears anywhere in the output.
            ValidationCheck(name="findings_redacted",
                            passed=all("…" in f["redacted"] or f["redacted"] == "****"
                                       for f in findings)),
            # A count that disagrees with the list it summarises makes the whole report untrustworthy.
            ValidationCheck(name="counts_reconcile",
                            passed=(result.get("secret_count", 0) == sum(result.get("findings_by_rule", {}).values())),
                            detail="secret_count equals the sum of per-rule counts"),
            ValidationCheck(name="every_finding_locatable",
                            passed=all(f.get("path") and isinstance(f.get("line"), int) and f["line"] >= 1
                                       for f in findings),
                            detail="every finding carries a file path and a 1-indexed line number"),
        ]
