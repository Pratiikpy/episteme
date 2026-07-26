"""Batch 2 endpoints: document.redact_pii, email.validate, data.join, data.clean,
object.diff, site.map, api.to_mcp, mcp.validate. All deterministic + offline."""
from __future__ import annotations

import json
import re
from collections import Counter

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

# ------------------------------------------------------------------ documents
_PII = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("phone", re.compile(r"\b(?:\+?\d{1,3}[ -]?)?(?:\(?\d{3}\)?[ -]?)\d{3}[ -]?\d{4}\b")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


class DocumentRedactPiiNode(Node):
    name = "document.redact_pii"
    price_usdt = 0.02
    deterministic = True
    engine = "episteme-pii"

    def run(self, ctx: NodeContext) -> dict:
        text = ctx.input.get("text")
        if not isinstance(text, str) or not text:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide non-empty 'text'")
        counts = {}
        redacted = text
        for label, pat in _PII:
            found = pat.findall(redacted)
            if found:
                counts[label] = counts.get(label, 0) + len(found)
                redacted = pat.sub(f"[{label.upper()}]", redacted)
        return {"redacted_text": redacted, "pii_counts": counts,
                "total_redactions": sum(counts.values()), "found_pii": bool(counts)}

    def validate(self, result, ctx):
        # no raw email should survive in the redacted output
        leftover = _PII[0][1].search(result.get("redacted_text", ""))
        return [ValidationCheck(name="no_leftover_email", passed=leftover is None)]


# ------------------------------------------------------------------ identity
# Disposable/throwaway providers. A five-entry list catches almost nothing in practice — these are
# the high-volume services plus the alias domains they rotate through, which is what actually shows
# up in signup abuse.
_DISPOSABLE = {
    "mailinator.com", "tempmail.com", "temp-mail.org", "10minutemail.com", "10minutemail.net",
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "sharklasers.com",
    "grr.la", "guerrillamailblock.com", "trashmail.com", "trashmail.de", "wegwerfmail.de",
    "yopmail.com", "yopmail.net", "cool.fr.nf", "jetable.org", "throwawaymail.com",
    "maildrop.cc", "mailnesia.com", "mytrashmail.com", "dispostable.com", "fakeinbox.com",
    "getairmail.com", "mohmal.com", "emailondeck.com", "tempinbox.com", "spamgourmet.com",
    "mailcatch.com", "inboxbear.com", "tempr.email", "discard.email", "1secmail.com",
    "1secmail.net", "1secmail.org", "burnermail.io", "anonaddy.me", "mail-temp.com",
    "moakt.com", "luxusmail.org", "tmpmail.org", "tmpmail.net", "minuteinbox.com",
    "vomoto.com", "spam4.me", "byom.de", "harakirimail.com", "nowmymail.com", "trbvm.com",
}
_ROLE = {
    "admin", "administrator", "info", "support", "sales", "contact", "noreply", "no-reply",
    "donotreply", "do-not-reply", "webmaster", "postmaster", "hostmaster", "abuse", "billing",
    "help", "helpdesk", "marketing", "office", "team", "hello", "enquiries", "inquiries",
    "careers", "jobs", "hr", "legal", "privacy", "security", "root", "mail", "news", "orders",
}
# Deliberately not a full RFC 5322 parser — that grammar admits addresses no mail system in
# production will accept. This is the practical subset every provider actually allows.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# Cache MX answers for the lifetime of the process: a batch validating a customer list hits the same
# handful of domains repeatedly, and re-querying each time is slow and needlessly noisy for the
# resolver. Keyed by domain; value is the (has_mx, hosts, error) triple.
_MX_CACHE: dict[str, tuple[bool, list[str], str | None]] = {}


# The providers that own the overwhelming majority of consumer addresses, and therefore the ones
# whose misspellings actually occur in signup data.
_POPULAR_DOMAINS = (
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "hotmail.co.uk",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "proton.me", "gmx.com", "gmx.de", "mail.com", "zoho.com", "yandex.com", "qq.com", "163.com",
)


def _edit_distance(a: str, b: str, cap: int = 2) -> int:
    """Levenshtein distance, abandoned once it provably exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _typo_suggestion(domain: str) -> str | None:
    """Nearest popular provider within one or two edits, or None.

    Deliberately excludes an exact match, and requires the domain to be long enough that a
    two-edit radius is not most of the string (otherwise short legitimate domains get flagged)."""
    if domain in _POPULAR_DOMAINS or len(domain) < 6:
        return None
    best, best_d = None, 3
    for cand in _POPULAR_DOMAINS:
        d = _edit_distance(domain, cand)
        # One edit is a strong signal at any length; allow two only on longer domains, where the
        # ratio still makes a typo far likelier than a coincidence.
        if d < best_d and (d == 1 or (d == 2 and len(domain) >= 9)):
            best, best_d = cand, d
    return best


def _resolve_mx(domain: str, timeout: float = 3.0) -> tuple[bool | None, list[str], str | None]:
    """Look up mail exchangers for `domain`. Returns (has_mx, hosts, note).

    `has_mx` is deliberately THREE-STATE, and the distinction is the whole point:

      True  — a mail exchanger exists (or an A record does; RFC 5321 §5.1's implicit MX rule means a
              host with an address record and no MX is still a valid mail destination, so treating
              "no MX record" as undeliverable would reject real addresses)
      False — the domain provably cannot receive mail: it does not exist (NXDOMAIN) or publishes an
              RFC 7505 null MX
      None  — the question could not be answered: no resolver installed, a timeout, a network block

    Collapsing None into False is a correctness bug with real consequences. It shipped: with
    dnspython absent from the container, every lookup returned "resolver_unavailable" and the node
    reported gmail.com as UNDELIVERABLE — a false negative that would have someone delete a perfectly
    good address from their list. Inability to check is not evidence of absence.
    """
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    result: tuple[bool | None, list[str], str | None]
    try:
        import dns.resolver  # noqa: PLC0415 — optional dep; syntax checks must work without it
        import dns.exception  # noqa: PLC0415
    except ImportError:
        # NOT cached: a resolver could be installed later, and this is an environment fault rather
        # than a fact about the domain.
        return (None, [], "resolver_unavailable")

    def _mk() -> "dns.resolver.Resolver":
        r = dns.resolver.Resolver()
        r.timeout = timeout
        r.lifetime = timeout
        return r

    try:
        answers = _mk().resolve(domain, "MX")
        hosts = sorted(str(r.exchange).rstrip(".") for r in answers)
        # A single "." exchange is the RFC 7505 null MX: the domain explicitly accepts no mail.
        result = (False, [], "null_mx") if hosts == [""] else (True, hosts, None)
    except dns.resolver.NXDOMAIN:
        result = (False, [], "NXDOMAIN")            # the domain does not exist: real evidence
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        # No MX record. Under the implicit MX rule an address record still makes it deliverable.
        try:
            _mk().resolve(domain, "A")
            result = (True, [], "implicit_mx_via_a_record")
        except dns.resolver.NXDOMAIN:
            result = (False, [], "NXDOMAIN")
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            result = (False, [], "no_mx_and_no_address_record")
        except Exception as e:  # noqa: BLE001 — timeout or transport failure: unknown, not absent
            result = (None, [], f"lookup_failed:{type(e).__name__}")
    except Exception as e:  # noqa: BLE001 — timeout, transport failure, malformed response
        result = (None, [], f"lookup_failed:{type(e).__name__}")
    # Only cache an actual answer about the domain, never an environment failure.
    if result[0] is not None:
        _MX_CACHE[domain] = result
    return result


class EmailValidateNode(Node):
    name = "email.validate"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-emailvalidate"

    def run(self, ctx: NodeContext) -> dict:
        email = str(ctx.input.get("email", "")).strip()
        if not email:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'email'")
        # MX resolution is on by default. The previous version hardcoded mx_checked=False and never
        # queried anything, so `deliverable_guess` was "the regex matched" — it passed every
        # misspelled domain (gmial.com, exmaple.co) and every domain that does not exist at all.
        # That is the one question a buyer is actually paying to have answered.
        check_mx = bool(ctx.input.get("check_mx", True))
        syntax_ok = bool(_EMAIL_RE.match(email)) and email.count("@") == 1 and ".." not in email
        local, _, domain = email.partition("@")
        domain = domain.lower()
        local_l = local.lower()
        is_disposable = domain in _DISPOSABLE
        is_role = local_l in _ROLE
        # Gmail ignores dots and everything after a '+', so two different strings can be one mailbox.
        # Deduplicating a list without this produces double-sends to the same person.
        # Only meaningful for a syntactically valid address — normalizing "bad@@syntax.com" would
        # emit something that is not an address at all.
        normalized = None
        if syntax_ok:
            normalized = email.lower()
            if domain in {"gmail.com", "googlemail.com"}:
                normalized = local_l.split("+", 1)[0].replace(".", "") + "@gmail.com"
            elif "+" in local_l:
                normalized = local_l.split("+", 1)[0] + "@" + domain

        # A domain one keystroke away from a major provider almost always means a typo — and it
        # frequently DOES resolve, because typosquatters register exactly these. MX alone therefore
        # calls it deliverable, which is true and useless: the mail reaches a stranger.
        typo_of = _typo_suggestion(domain) if syntax_ok and domain else None

        has_mx: bool | None = None
        mx_hosts: list[str] = []
        mx_note: str | None = None
        if check_mx and syntax_ok and domain:
            has_mx, mx_hosts, mx_note = _resolve_mx(domain)

        reasons = []
        if not syntax_ok:
            reasons.append("invalid syntax")
        if is_disposable:
            reasons.append("disposable domain")
        if is_role:
            reasons.append("role account")
        if has_mx is False:
            reasons.append("null MX: domain refuses mail" if mx_note == "null_mx"
                           else ("domain does not exist" if mx_note == "NXDOMAIN"
                                 else f"domain has no mail exchanger ({mx_note})"))
        elif has_mx is None and check_mx and syntax_ok:
            # Say plainly that the check did not happen, rather than implying a result either way.
            reasons.append(f"deliverability not checked ({mx_note})")
        if typo_of:
            reasons.append(f"domain looks like a typo of {typo_of}")

        # Honest three-state verdict. "unknown" is a real answer when the resolver could not be
        # reached — reporting a confident "deliverable" on no evidence is what makes a validator
        # useless, because the buyer cannot tell a checked pass from an unchecked one.
        if not syntax_ok or has_mx is False:
            verdict = "undeliverable"
        elif has_mx is True:
            verdict = "deliverable"
        else:
            verdict = "unknown"

        return {
            "email": email,
            "normalized": normalized,
            "syntax_ok": syntax_ok,
            "domain": domain or None,
            "local_part": local or None,
            "is_disposable": is_disposable,
            "is_role": is_role,
            "mx_checked": has_mx is not None,
            "has_mx": has_mx,
            "mx_hosts": mx_hosts[:10],
            "mx_note": mx_note,
            "likely_typo_of": typo_of,
            "verdict": verdict,
            # Kept for callers built against the old field, now backed by an actual MX result
            # instead of a regex match.
            "deliverable_guess": verdict == "deliverable" and not is_disposable,
            "risk": "high" if (is_disposable or verdict == "undeliverable" or typo_of)
                    else ("medium" if is_role or verdict == "unknown" else "low"),
            "reasons": reasons,
            "disposable_domains_known": len(_DISPOSABLE),
            "role_accounts_known": len(_ROLE),
        }

    def validate(self, result, ctx):
        verdict = result.get("verdict")
        return [
            ValidationCheck(name="has_verdict", passed=verdict in {"deliverable", "undeliverable", "unknown"}),
            # A verdict must never contradict the evidence it was derived from.
            ValidationCheck(
                name="verdict_consistent_with_evidence",
                passed=not (verdict == "deliverable" and (not result.get("syntax_ok") or result.get("has_mx") is False)),
                detail="'deliverable' requires valid syntax and a resolvable mail exchanger",
            ),
            # The claim of having checked must match whether a check actually happened.
            ValidationCheck(
                name="mx_claim_is_truthful",
                passed=(result.get("mx_checked") is (result.get("has_mx") is not None)),
                detail="mx_checked is true only when a resolver actually answered",
            ),
            ValidationCheck(
                name="normalized_is_single_address",
                passed=(result.get("normalized") is None
                        or str(result["normalized"]).count("@") == 1),
                detail="present only for syntactically valid input, and then exactly one '@'",
            ),
        ]


# ------------------------------------------------------------------ data
def _rows(v, name):
    if isinstance(v, list):
        return v
    raise NodeError(ErrorCode.INVALID_INPUT, f"'{name}' must be a list of objects")


class DataJoinNode(Node):
    name = "data.join"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-join"

    def run(self, ctx: NodeContext) -> dict:
        left = _rows(ctx.input.get("left"), "left")
        right = _rows(ctx.input.get("right"), "right")
        on = ctx.input.get("on")
        how = str(ctx.input.get("how", "inner"))
        if not on:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'on' (join key)")
        if how not in ("inner", "left"):
            raise NodeError(ErrorCode.INVALID_INPUT, "how must be inner|left")
        ridx = {}
        for r in right:
            ridx.setdefault(r.get(on), []).append(r)
        out = []
        for l in left:
            matches = ridx.get(l.get(on), [])
            if matches:
                for m in matches:
                    merged = dict(l)
                    merged.update({f"right_{k}" if k in l and k != on else k: v for k, v in m.items()})
                    out.append(merged)
            elif how == "left":
                out.append(dict(l))
        return {"how": how, "on": on, "left_rows": len(left), "right_rows": len(right),
                "joined_rows": len(out), "rows": out[:5000]}

    def validate(self, result, ctx):
        return [ValidationCheck(name="joined", passed="joined_rows" in result)]


class DataCleanNode(Node):
    name = "data.clean"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-clean"

    # Every operation this node performs, with the spellings a caller might reasonably send.
    #
    # This map exists because of a real, signed lie: the code tested `if "lower" in ops` while a caller
    # asked for `"lowercase"`. The operation silently did nothing, the response echoed the requested
    # ops back verbatim as though all had run, the validation check passed (it only asserted that a
    # `rows` key existed), and the artifact was cryptographically signed. The buyer received an
    # attested claim that their data had been lowercased when it had not been touched.
    #
    # So: aliases are accepted, and anything NOT in this map is now a hard INVALID_INPUT rather than a
    # silent no-op. Signing "I did what you asked" is only defensible if unknown instructions fail loudly.
    _OPS = {
        "trim": "trim", "strip": "trim",
        "collapse_ws": "collapse_ws", "collapse_whitespace": "collapse_ws", "squeeze_ws": "collapse_ws",
        "lower": "lower", "lowercase": "lower", "to_lower": "lower",
        "upper": "upper", "uppercase": "upper", "to_upper": "upper",
        "title": "title", "titlecase": "title",
        "drop_empty": "drop_empty", "remove_empty": "drop_empty",
    }

    def run(self, ctx: NodeContext) -> dict:
        rows = _rows(ctx.input.get("rows"), "rows")
        requested = ctx.input.get("ops") or ["trim", "collapse_ws", "drop_empty"]
        if isinstance(requested, str):
            requested = [requested]
        if not isinstance(requested, list):
            raise NodeError(ErrorCode.INVALID_INPUT, "'ops' must be a list of operation names")

        canon: list[str] = []
        unknown: list[str] = []
        for op in requested:
            key = str(op).strip().lower()
            if key in self._OPS:
                if self._OPS[key] not in canon:
                    canon.append(self._OPS[key])
            else:
                unknown.append(str(op))
        if unknown:
            raise NodeError(
                ErrorCode.INVALID_INPUT,
                f"unsupported op(s): {unknown}. Supported: {sorted(set(self._OPS.values()))} "
                f"(aliases accepted: {sorted(self._OPS)}). Nothing was changed.")

        changes = 0
        cleaned = []
        for r in rows:
            nr = {}
            for k, v in r.items():
                nv = v
                if isinstance(v, str):
                    if "trim" in canon:
                        nv = nv.strip()
                    if "collapse_ws" in canon:
                        nv = re.sub(r"\s+", " ", nv)
                    if "lower" in canon:
                        nv = nv.lower()
                    if "upper" in canon:
                        nv = nv.upper()
                    if "title" in canon:
                        nv = nv.title()
                if nv != v:
                    changes += 1
                nr[k] = nv
            if "drop_empty" in canon:
                nr = {k: v for k, v in nr.items() if v not in ("", None)}
            cleaned.append(nr)
        # `ops_applied` is the canonical list that actually ran; `ops_requested` is what was asked for.
        # Reporting only the request is what allowed the silent no-op to look like success.
        return {"ops_requested": requested, "ops_applied": canon, "ops": canon,
                "input_rows": len(rows), "cells_changed": changes, "rows": cleaned[:5000]}

    def validate(self, result, ctx):
        """Assert the work was actually DONE, not merely that a key exists.

        The old check (`"rows" in result`) passed for output that had been left completely untouched,
        which is how a no-op earned a signed receipt. These checks verify the requested transforms are
        observable in the returned rows.
        """
        rows = result.get("rows") or []
        applied = set(result.get("ops_applied") or [])
        checks = [ValidationCheck(name="cleaned", passed=bool(result.get("rows") is not None))]

        def _strings():
            return [v for r in rows if isinstance(r, dict) for v in r.values() if isinstance(v, str)]

        if "lower" in applied:
            checks.append(ValidationCheck(
                name="lowercase_applied",
                passed=all(v == v.lower() for v in _strings()),
                detail="every returned string is lower-case"))
        if "upper" in applied:
            checks.append(ValidationCheck(
                name="uppercase_applied",
                passed=all(v == v.upper() for v in _strings()),
                detail="every returned string is upper-case"))
        if "trim" in applied:
            checks.append(ValidationCheck(
                name="trim_applied",
                passed=all(v == v.strip() for v in _strings()),
                detail="no returned string has leading or trailing whitespace"))
        if "collapse_ws" in applied:
            checks.append(ValidationCheck(
                name="whitespace_collapsed",
                passed=all("  " not in v for v in _strings()),
                detail="no returned string contains a double space"))
        if "drop_empty" in applied:
            checks.append(ValidationCheck(
                name="empty_fields_dropped",
                passed=all(v not in ("", None) for r in rows if isinstance(r, dict) for v in r.values()),
                detail="no returned field is empty"))
        return checks


class ObjectDiffNode(Node):
    name = "object.diff"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-objdiff"

    def run(self, ctx: NodeContext) -> dict:
        import json
        a = ctx.input.get("a")
        b = ctx.input.get("b")
        if isinstance(a, str):
            try: a = json.loads(a)
            except Exception: pass
        if isinstance(b, str):
            try: b = json.loads(b)
            except Exception: pass
        added, removed, changed = [], [], []
        self._diff(a, b, "$", added, removed, changed)
        return {"added": added, "removed": removed, "changed": changed,
                "change_count": len(added) + len(removed) + len(changed),
                "identical": not (added or removed or changed)}

    def _diff(self, a, b, path, added, removed, changed):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in b.keys() - a.keys():
                added.append(f"{path}.{k}")
            for k in a.keys() - b.keys():
                removed.append(f"{path}.{k}")
            for k in a.keys() & b.keys():
                self._diff(a[k], b[k], f"{path}.{k}", added, removed, changed)
        elif isinstance(a, list) and isinstance(b, list):
            if a != b:
                changed.append({"path": path, "from_len": len(a), "to_len": len(b)})
        elif a != b:
            changed.append({"path": path, "from": a, "to": b})

    def validate(self, result, ctx):
        return [ValidationCheck(name="diffed", passed="change_count" in result)]


# ------------------------------------------------------------------ web
class SiteMapNode(Node):
    name = "site.map"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-sitemap"

    def run(self, ctx: NodeContext) -> dict:
        sitemap = ctx.input.get("sitemap")
        html = ctx.input.get("html")
        urls = []
        if sitemap:
            urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", str(sitemap))
        elif html:
            from urllib.parse import urljoin
            base = ctx.input.get("base_url", "")
            for href in re.findall(r"href=[\"']([^\"']+)[\"']", str(html)):
                urls.append(urljoin(base, href) if base else href)
        else:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'sitemap' (xml) or 'html'")
        seen, uniq = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u); uniq.append(u)
        return {"url_count": len(uniq), "urls": uniq[:5000]}

    def validate(self, result, ctx):
        return [ValidationCheck(name="mapped", passed="url_count" in result)]


# ------------------------------------------------------------------ api/mcp
_METHODS = {"get", "post", "put", "delete", "patch"}

# --- mcp.validate rule support -------------------------------------------------------------------
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_JSON_TYPES = {"string", "number", "integer", "boolean", "object", "array", "null"}
_PLACEHOLDER_DESC = re.compile(r"(?i)\b(?:todo|tbd|fixme|xxx|placeholder|description here|"
                               r"lorem ipsum|no description|n/?a)\b")
# Parameters the model is expected to fill in with a live credential. Anything the model writes is
# also written into its transcript, so this is worth naming even though it is not a spec violation.
_SECRETISH_PARAM = re.compile(r"(?i)^(?:api[_-]?key|apikey|secret|token|password|passwd|"
                              r"access[_-]?token|auth[_-]?token|private[_-]?key|credentials?)$")
_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}
# Every rule mcp.validate can report. Kept as an explicit list so the node can state how deep the
# check goes — "valid: true" from a four-rule linter and from a twenty-rule one mean very different
# things, and the buyer has no way to tell them apart otherwise.
_MCP_RULES = (
    "no-tools", "not-an-object",
    "name-missing", "name-duplicate", "name-invalid-chars", "name-too-long",
    "description-missing", "description-too-short", "description-placeholder",
    "description-duplicate",
    "schema-not-object", "schema-no-properties", "schema-properties-not-object",
    "schema-contains-ref",
    "required-not-array", "required-unknown-property",
    "property-not-schema", "property-untyped", "property-bad-type",
    "array-without-items", "object-without-properties",
    "enum-empty", "enum-duplicates", "default-type-mismatch",
    "property-no-description", "property-accepts-credential",
)


class _RefResolver:
    """Resolves internal OpenAPI `$ref` pointers.

    Real specs are almost entirely `$ref` — schemas, parameters and request bodies all live under
    `#/components` and are referenced by pointer. Without resolution the converter saw `{"$ref": …}`,
    found no `type` and no `properties`, and emitted a tool with an empty argument list: a tool that
    is syntactically valid, describes itself confidently, and cannot make a single working call.

    External refs (another file or a URL) are recorded in `unresolved` rather than fetched — reaching
    out to a caller-supplied URL from inside a paid node would be an SSRF vector.
    """

    def __init__(self, root: dict, max_depth: int = 32):
        self.root = root
        self.max_depth = max_depth
        self.resolved_count = 0
        self.unresolved: set[str] = set()

    def _lookup(self, pointer: str) -> dict | None:
        # RFC 6901 JSON pointer, with the ~0/~1 escapes that appear in paths like /users/{id}.
        node: object = self.root
        for raw in pointer.lstrip("#/").split("/"):
            if not raw:
                continue
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict):
                if token not in node:
                    return None
                node = node[token]
            elif isinstance(node, list):
                try:
                    node = node[int(token)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return node if isinstance(node, dict) else None

    def resolve(self, obj, _depth: int = 0):
        """Follow `$ref` chains on `obj` until a concrete object is reached. Bounded by max_depth so
        a self-referential schema (a Node with a children: [Node] field — entirely legal and common)
        terminates instead of recursing forever."""
        if not isinstance(obj, dict) or _depth >= self.max_depth:
            return obj if isinstance(obj, dict) else {}
        ref = obj.get("$ref")
        if not isinstance(ref, str):
            return obj
        if not ref.startswith("#"):
            self.unresolved.add(ref)
            return {k: v for k, v in obj.items() if k != "$ref"}
        target = self._lookup(ref)
        if target is None:
            self.unresolved.add(ref)
            return {k: v for k, v in obj.items() if k != "$ref"}
        self.resolved_count += 1
        merged = dict(self.resolve(target, _depth + 1))
        # Sibling keys alongside a $ref (description, nullable, …) are overrides and must survive.
        for k, v in obj.items():
            if k != "$ref":
                merged[k] = v
        return merged


# JSON Schema keywords worth carrying into an MCP tool schema. Type alone is not enough: without
# enum the model invents values, without description it guesses intent, and without items it cannot
# tell what belongs in an array.
_SCHEMA_KEYS = ("type", "description", "enum", "default", "format", "pattern",
                "minimum", "maximum", "minLength", "maxLength", "example")


def _json_schema_subset(schema: dict, resolver: "_RefResolver", _depth: int = 0) -> dict:
    """Project an OpenAPI schema onto the JSON Schema subset MCP clients understand, recursively."""
    schema = resolver.resolve(schema, _depth)
    if not isinstance(schema, dict) or _depth > 12:
        return {"type": "string"}
    # allOf is how OpenAPI expresses composition; flatten it so the fields are actually visible.
    if isinstance(schema.get("allOf"), list):
        merged: dict = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            sub = _json_schema_subset(part, resolver, _depth + 1)
            merged["properties"].update(sub.get("properties") or {})
            merged["required"].extend(sub.get("required") or [])
        for k in _SCHEMA_KEYS:
            if k in schema and k != "type":
                merged[k] = schema[k]
        if not merged["required"]:
            merged.pop("required")
        return merged
    # oneOf/anyOf cannot be represented as a single concrete type; take the first branch and say so
    # rather than silently emitting an untyped field.
    for combinator in ("oneOf", "anyOf"):
        if isinstance(schema.get(combinator), list) and schema[combinator]:
            out = _json_schema_subset(schema[combinator][0], resolver, _depth + 1)
            out["description"] = (str(schema.get("description") or out.get("description") or "")
                                  + f" (one of {len(schema[combinator])} accepted shapes)").strip()
            return out

    out = {k: schema[k] for k in _SCHEMA_KEYS if k in schema}
    out.setdefault("type", "object" if "properties" in schema else "string")
    if out.get("type") == "object" and isinstance(schema.get("properties"), dict):
        out["properties"] = {
            pname: _json_schema_subset(pschema, resolver, _depth + 1)
            for pname, pschema in schema["properties"].items()
        }
        if isinstance(schema.get("required"), list):
            out["required"] = [r for r in schema["required"] if isinstance(r, str)]
    if out.get("type") == "array":
        out["items"] = _json_schema_subset(schema.get("items") or {}, resolver, _depth + 1)
    return out


def _sanitize_tool_name(raw: str) -> str:
    """MCP tool names must be a plain identifier: an operationId of "users/{id}:get" would be
    rejected by the client, so the tool would never load."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw)).strip("_")
    return cleaned[:64]


class ApiToMcpNode(Node):
    name = "api.to_mcp"
    price_usdt = 0.02
    deterministic = True
    engine = "episteme-api2mcp"

    def run(self, ctx: NodeContext) -> dict:
        spec = ctx.input.get("spec")
        if isinstance(spec, str):
            import json
            try: spec = json.loads(spec)
            except Exception as e: raise NodeError(ErrorCode.INVALID_INPUT, f"spec JSON: {e}")
        if not isinstance(spec, dict):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'spec'")

        resolver = _RefResolver(spec)
        # The server base URL is what makes the generated tools callable at all. Without it the
        # caller has a list of paths and no host to send them to.
        servers = [s.get("url") for s in (spec.get("servers") or []) if isinstance(s, dict) and s.get("url")]
        base_url = servers[0] if servers else None

        tools: list[dict] = []
        used_names: set[str] = set()
        skipped: list[dict] = []
        for path, item in (spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            item = resolver.resolve(item)
            # OpenAPI allows parameters on the path item, shared by every method under it — path
            # parameters are very commonly declared here. Ignoring them dropped the one argument
            # without which the URL cannot even be constructed.
            shared_params = [p for p in (item.get("parameters") or []) if isinstance(p, dict)]
            for m, op in item.items():
                if m.lower() not in _METHODS or not isinstance(op, dict):
                    continue
                op = resolver.resolve(op)
                props: dict = {}
                required: list[str] = []
                location: dict[str, str] = {}   # param name -> query | path | header | cookie | body

                for p in shared_params + [p for p in (op.get("parameters") or []) if isinstance(p, dict)]:
                    p = resolver.resolve(p)
                    pname = p.get("name")
                    if not pname:
                        continue
                    schema = resolver.resolve(p.get("schema") or {})
                    entry = _json_schema_subset(schema, resolver)
                    desc = p.get("description") or schema.get("description")
                    if desc:
                        entry["description"] = str(desc)[:300]
                    props[pname] = entry
                    location[pname] = p.get("in") or "query"
                    if p.get("required"):
                        required.append(pname)

                # requestBody is where the entire payload of a POST/PUT/PATCH lives. Skipping it
                # produced tools that looked complete and could not send a single field — calling
                # one would hit the API with an empty body and get a 400 every time.
                body = resolver.resolve(op.get("requestBody") or {})
                body_required = bool(body.get("required"))
                content = body.get("content") or {}
                media = (
                    "application/json" if "application/json" in content
                    else next((k for k in content if "json" in k.lower()), None)
                    or next(iter(content), None)
                )
                if media:
                    bschema = resolver.resolve((content.get(media) or {}).get("schema") or {})
                    bschema = _json_schema_subset(bschema, resolver)
                    if bschema.get("type") == "object" and isinstance(bschema.get("properties"), dict):
                        # Lift the body's own fields to the top level so the tool takes a flat
                        # argument list, which is what an LLM fills in reliably. A field colliding
                        # with a query parameter keeps the query one and is nested under `body`.
                        for bname, bprop in bschema["properties"].items():
                            if bname in props:
                                props.setdefault("body", {"type": "object", "properties": {}})
                                props["body"]["properties"][bname] = bprop
                                location.setdefault("body", "body")
                            else:
                                props[bname] = bprop
                                location[bname] = "body"
                        for rname in bschema.get("required") or []:
                            if rname in props and rname not in required:
                                required.append(rname)
                    elif bschema:
                        props["body"] = bschema
                        location["body"] = "body"
                        if body_required:
                            required.append("body")

                raw_name = op.get("operationId") or (
                    f"{m.lower()}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"
                )
                name = _sanitize_tool_name(raw_name)
                if not name:
                    skipped.append({"path": path, "method": m.upper(), "reason": "no usable tool name"})
                    continue
                # Two operations resolving to the same name would silently shadow each other, so the
                # second tool would be unreachable. Suffix instead of overwrite.
                if name in used_names:
                    n = 2
                    while f"{name}_{n}"[:64] in used_names:
                        n += 1
                    name = f"{name}_{n}"[:64]
                used_names.add(name)

                schema: dict = {"type": "object", "properties": props}
                if required:
                    schema["required"] = sorted(set(required))
                tools.append({
                    "name": name,
                    "description": (op.get("summary") or op.get("description")
                                    or f"{m.upper()} {path}")[:200],
                    "inputSchema": schema,
                    "_method": m.upper(),
                    "_path": path,
                    "_base_url": base_url,
                    "_param_location": location,   # how to place each argument when calling
                    "_has_body": any(v == "body" for v in location.values()),
                })
        return {
            "tool_count": len(tools),
            "tools": tools,
            "base_url": base_url,
            "servers": servers,
            "openapi_version": spec.get("openapi") or spec.get("swagger"),
            "refs_resolved": resolver.resolved_count,
            "unresolved_refs": sorted(resolver.unresolved)[:20],
            "skipped_operations": skipped,
            "tools_with_body": sum(1 for t in tools if t["_has_body"]),
        }

    def validate(self, result, ctx):
        tools = result.get("tools", [])
        # Any tool whose method carries a payload must expose somewhere to put it, or it cannot
        # perform the call it claims to. This is the exact defect the check was added to prevent.
        body_methods = {"POST", "PUT", "PATCH"}
        spec_in = ctx.input.get("spec")
        expects_body = []
        if isinstance(spec_in, dict):
            for path, item in (spec_in.get("paths") or {}).items():
                if not isinstance(item, dict):
                    continue
                for m, op in item.items():
                    if m.upper() in body_methods and isinstance(op, dict) and op.get("requestBody"):
                        expects_body.append((m.upper(), path))
        got_body = {(t["_method"], t["_path"]) for t in tools if t.get("_has_body")}
        return [
            ValidationCheck(name="generated", passed="tool_count" in result),
            ValidationCheck(name="every_tool_has_object_schema",
                            passed=all(t.get("inputSchema", {}).get("type") == "object" for t in tools)),
            ValidationCheck(name="tool_names_unique",
                            passed=len({t["name"] for t in tools}) == len(tools),
                            detail="a duplicate name makes the shadowed tool permanently uncallable"),
            ValidationCheck(name="request_bodies_captured",
                            passed=all(k in got_body for k in expects_body),
                            detail=f"{len(expects_body)} operation(s) declare a requestBody; "
                                   f"{len(got_body)} tool(s) accept one"),
            ValidationCheck(name="all_refs_resolved",
                            passed=not result.get("unresolved_refs"),
                            detail="an unresolved $ref leaves the tool with no usable parameters"),
        ]


class McpValidateNode(Node):
    name = "mcp.validate"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-mcpvalidate"

    def run(self, ctx: NodeContext) -> dict:
        tools = ctx.input.get("tools")
        if not isinstance(tools, list):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'tools' (MCP tools/list array)")
        findings: list[dict] = []
        names: dict[str, int] = {}
        descriptions: dict[str, int] = {}

        def add(idx: int, rule: str, severity: str, issue: str, hint: str = "") -> None:
            findings.append({"tool": idx, "rule": rule, "severity": severity,
                             "issue": issue, "hint": hint,
                             "tool_name": (tools[idx].get("name") if isinstance(tools[idx], dict) else None)})

        if not tools:
            findings.append({"tool": -1, "rule": "no-tools", "severity": "error",
                             "issue": "the tool list is empty", "hint": "a server exposing no tools "
                             "advertises nothing an agent can call", "tool_name": None})

        for i, t in enumerate(tools):
            if not isinstance(t, dict):
                add(i, "not-an-object", "error", "entry is not an object")
                continue

            # --- name ---
            name = t.get("name")
            if not name or not isinstance(name, str):
                add(i, "name-missing", "error", "missing name",
                    "a tool with no name cannot be invoked")
            else:
                if name in names:
                    add(i, "name-duplicate", "error", f"duplicate name '{name}' (also at index {names[name]})",
                        "the later tool is permanently shadowed and can never be called")
                names.setdefault(name, i)
                if not _MCP_NAME_RE.match(name):
                    add(i, "name-invalid-chars", "error",
                        f"name '{name}' contains characters outside [A-Za-z0-9_-]",
                        "most clients reject the tool outright, so it never loads")
                if len(name) > 64:
                    add(i, "name-too-long", "error", f"name is {len(name)} chars (limit 64)")

            # --- description ---
            desc = t.get("description")
            if not desc or not isinstance(desc, str) or not desc.strip():
                add(i, "description-missing", "error", "missing description",
                    "the model selects tools by description; without one it will not be chosen")
            else:
                if len(desc.strip()) < 12:
                    add(i, "description-too-short", "warning",
                        f"description is {len(desc.strip())} chars",
                        "too terse to distinguish this tool from a similar one")
                if _PLACEHOLDER_DESC.search(desc):
                    add(i, "description-placeholder", "warning",
                        "description looks like a placeholder", f"found: {desc.strip()[:40]!r}")
                key = desc.strip().lower()
                if key in descriptions:
                    add(i, "description-duplicate", "warning",
                        f"description is identical to tool at index {descriptions[key]}",
                        "the model cannot tell the two tools apart")
                descriptions.setdefault(key, i)

            # --- inputSchema ---
            sch = t.get("inputSchema")
            if not isinstance(sch, dict) or sch.get("type") != "object":
                add(i, "schema-not-object", "error", "inputSchema must be an object schema",
                    'MCP requires {"type": "object", ...}')
                continue
            props = sch.get("properties")
            if props is None:
                add(i, "schema-no-properties", "warning",
                    "inputSchema has no 'properties' key",
                    'use "properties": {} to declare a tool that takes no arguments')
                props = {}
            if not isinstance(props, dict):
                add(i, "schema-properties-not-object", "error", "'properties' must be an object")
                props = {}
            if "$ref" in json.dumps(sch):
                add(i, "schema-contains-ref", "error", "inputSchema contains a $ref",
                    "clients do not resolve refs in tool schemas; inline the definition")

            required = sch.get("required")
            if required is not None and not isinstance(required, list):
                add(i, "required-not-array", "error", "'required' must be an array")
            elif isinstance(required, list):
                for r in required:
                    if r not in props:
                        add(i, "required-unknown-property", "error",
                            f"'required' lists '{r}', which is not in properties",
                            "no argument set can satisfy the schema, so every call fails validation")

            for pname, pschema in props.items():
                where = f"property '{pname}'"
                if not isinstance(pschema, dict):
                    add(i, "property-not-schema", "error", f"{where} is not a schema object")
                    continue
                ptype = pschema.get("type")
                if ptype is None and not any(k in pschema for k in ("enum", "oneOf", "anyOf", "allOf", "const")):
                    add(i, "property-untyped", "error", f"{where} has no 'type'",
                        "the model has nothing to infer the value shape from")
                elif ptype is not None and ptype not in _JSON_TYPES and not isinstance(ptype, list):
                    add(i, "property-bad-type", "error", f"{where} has invalid type {ptype!r}",
                        f"valid types: {sorted(_JSON_TYPES)}")
                if ptype == "array" and "items" not in pschema:
                    add(i, "array-without-items", "error", f"{where} is an array with no 'items'",
                        "the model cannot tell what to put in the array")
                if ptype == "object" and "properties" not in pschema:
                    add(i, "object-without-properties", "warning",
                        f"{where} is an object with no 'properties'",
                        "an unconstrained object invites malformed arguments")
                enum = pschema.get("enum")
                if enum is not None:
                    if not isinstance(enum, list) or not enum:
                        add(i, "enum-empty", "error", f"{where} has an empty or non-list enum",
                            "no value can satisfy it")
                    elif len(set(map(str, enum))) != len(enum):
                        add(i, "enum-duplicates", "warning", f"{where} enum contains duplicates")
                if "default" in pschema and ptype in _TYPE_CHECKS:
                    if not _TYPE_CHECKS[ptype](pschema["default"]):
                        add(i, "default-type-mismatch", "error",
                            f"{where} default {pschema['default']!r} is not of declared type '{ptype}'")
                if not pschema.get("description"):
                    add(i, "property-no-description", "warning", f"{where} has no description",
                        "the model guesses the meaning from the key name alone")
                if _SECRETISH_PARAM.match(pname):
                    add(i, "property-accepts-credential", "warning",
                        f"{where} looks like it accepts a credential",
                        "an argument the model fills in is written into transcripts and logs")

        errors = [f for f in findings if f["severity"] == "error"]
        warnings = [f for f in findings if f["severity"] == "warning"]
        return {
            "tool_count": len(tools),
            "finding_count": len(findings),
            "findings": findings,
            "error_count": len(errors),
            "warning_count": len(warnings),
            # `valid` stays strict on errors only — a warning is advice, not a blocker, and failing
            # a server for a missing property description would make the verdict useless.
            "valid": not errors,
            "findings_by_rule": dict(Counter(f["rule"] for f in findings)),
            "tools_with_errors": sorted({f["tool"] for f in errors if f["tool"] >= 0}),
            "rules_applied": len(_MCP_RULES),
            "clean_tools": [t.get("name") for i, t in enumerate(tools)
                            if isinstance(t, dict) and i not in {f["tool"] for f in findings}],
        }

    def validate(self, result, ctx):
        findings = result.get("findings", [])
        return [
            ValidationCheck(name="validated", passed="valid" in result),
            ValidationCheck(name="verdict_matches_findings",
                            passed=result.get("valid") is (result.get("error_count", 0) == 0),
                            detail="'valid' is true exactly when no error-severity finding exists"),
            ValidationCheck(name="counts_reconcile",
                            passed=result.get("finding_count", -1)
                            == result.get("error_count", 0) + result.get("warning_count", 0)),
            ValidationCheck(name="every_finding_actionable",
                            passed=all(f.get("rule") and f.get("severity") in {"error", "warning"}
                                       for f in findings),
                            detail="each finding names the rule it broke and how serious it is"),
        ]
