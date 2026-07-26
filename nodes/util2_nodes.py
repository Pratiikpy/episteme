"""Utility batch 3: text.stats, csv.to_table, receipt.verify."""
from __future__ import annotations

import io
import re

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError, Signer, sha256_hex


# A sentence ends at .!? followed by whitespace and something that starts a sentence — not at every
# period. Splitting on a bare `[.!?]+` counted "jane.doe@acme.com" as three sentences and reported 8
# for a text containing 6. Emails, URLs, decimals, ellipses and abbreviations all carry internal
# periods, and this node is priced and signed as a deterministic measurement, so the number has to be
# right rather than roughly right.
_MASK_PATTERNS = re.compile(r"\S+@\S+\.\S+|https?://\S+")
_SENTENCE_END = re.compile(
    r"(?<=[.!?])"               # immediately after a terminator
    r"\s+"                      # followed by whitespace — this alone rules out decimals like 3.14,
                                # whose period is never followed by a space
    # What follows must look like a new sentence. Tested as "NOT a lower-case letter" rather than
    # "an A-Z capital": [A-Z] is ASCII-only, so "Ça va?" and "你好世界!" were not recognised as sentence
    # starts and non-English text was undercounted. The negative form covers accented capitals,
    # non-cased scripts, digits and opening quotes alike.
    r"(?=[\"'(\[]?(?![a-z]))"
)
# Abbreviations that end in a period and are normally followed by a capitalised word.
_ABBREVIATIONS = ("mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "jr.", "sr.", "vs.", "etc.",
                  "i.e.", "e.g.", "no.", "inc.", "ltd.", "fig.", "approx.")


def _split_sentences(text: str) -> list[str]:
    """Sentence boundaries, with internal periods masked so they cannot create false splits."""
    if not text or not text.strip():
        return []
    # Replace emails/URLs with same-length filler containing no terminators, so every offset in the
    # masked copy still lines up with the original text.
    masked = _MASK_PATTERNS.sub(lambda m: "x" * len(m.group(0)), text)
    parts: list[str] = []
    last = 0
    for m in _SENTENCE_END.finditer(masked):
        head = masked[last:m.start()]
        # Compare the last WHOLE WORD, not a suffix: endswith() made "fast." match the "st."
        # abbreviation and silently swallowed a real sentence boundary.
        last_word = head.rstrip().rsplit(None, 1)[-1].lower() if head.strip() else ""
        if last_word in _ABBREVIATIONS:
            continue  # "Dr. Smith" is one sentence, not two
        parts.append(text[last:m.start()])
        last = m.end()
    parts.append(text[last:])
    return [p for p in (x.strip() for x in parts) if p]


class TextStatsNode(Node):
    name = "text.stats"
    price_usdt = 0.001
    deterministic = True
    engine = "episteme-textstats"

    def run(self, ctx: NodeContext) -> dict:
        text = ctx.input.get("text")
        if not isinstance(text, str):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'text'")
        words = re.findall(r"\b\w+\b", text)
        sentences = _split_sentences(text)
        return {
            "chars": len(text),
            "words": len(words),
            "sentences": len(sentences),
            "lines": text.count("\n") + (1 if text else 0),
            "avg_word_length": round(sum(len(w) for w in words) / len(words), 3) if words else 0,
            "reading_time_min": round(len(words) / 200, 3),
        }

    def validate(self, result, ctx):
        return [ValidationCheck(name="counted", passed="words" in result)]


class CsvToTableNode(Node):
    name = "csv.to_table"
    price_usdt = 0.005
    deterministic = True
    engine = "tabulate"

    def engine_available(self) -> bool:
        try:
            import tabulate  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import csv as _csv
        from tabulate import tabulate
        data = ctx.input.get("csv")
        if not isinstance(data, str) or not data.strip():
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'csv'")
        rows = list(_csv.reader(io.StringIO(data)))
        if not rows:
            raise NodeError(ErrorCode.UNSUPPORTED_FORMAT, "empty csv")
        header, *body = rows
        fmt = str(ctx.input.get("format", "github"))
        if fmt not in ("github", "grid", "simple", "pipe", "html"):
            fmt = "github"
        # numparse is OFF by default: tabulate would otherwise "helpfully" reformat numeric-looking
        # cells, turning 1284500.50 into 1.2845e+06 and 0.000015 into 1.5e-05. Rendering a table is
        # a formatting job, so the caller's bytes must survive it verbatim; opt in explicitly if you
        # actually want alignment-by-parsing.
        numparse = bool(ctx.input.get("numparse", False))
        table = tabulate(body, headers=header, tablefmt=fmt, disable_numparse=not numparse)
        return {"rows": len(body), "cols": len(header), "format": fmt,
                "numparse": numparse, "table": table}

    def validate(self, result, ctx):
        table = result.get("table") or ""
        checks = [ValidationCheck(name="rendered", passed=bool(table))]
        # Prove no cell was silently rewritten. "It produced a string" is not evidence of
        # correctness — this is the check that catches a formatter mangling the data it was given.
        if not result.get("numparse"):
            import csv as _csv
            import io as _io
            rows = list(_csv.reader(_io.StringIO(str(ctx.input.get("csv") or ""))))
            cells = [c.strip() for r in rows[1:] for c in r if c and c.strip()]
            missing = [c for c in cells if c not in table]
            checks.append(ValidationCheck(
                name="cells_preserved",
                passed=not missing,
                detail=None if not missing else f"{len(missing)} cell(s) altered by rendering, e.g. {missing[:3]}",
            ))
        return checks


class ReceiptVerifyNode(Node):
    name = "receipt.verify"
    price_usdt = 0.001
    deterministic = True
    engine = "episteme-receipt"

    def run(self, ctx: NodeContext) -> dict:
        env = ctx.input.get("envelope")
        if not isinstance(env, dict):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'envelope' (a full Episteme result object)")
        receipt = env.get("receipt")
        if not isinstance(receipt, dict):
            return {"receipt_present": False, "receipt_valid": False,
                    "reason": "no receipt in envelope"}
        # reconstruct the signed manifest exactly as runtime.Runtime._manifest does
        tool = env.get("tool")
        level = (env.get("validation") or {}).get("level")
        manifest = {
            "endpoint": env.get("endpoint"),
            "input_hashes": env.get("input_hashes"),
            "output_hash": env.get("output_hash"),
            "tool": tool,
            "level": level,
            "job_id": env.get("job_id"),
        }
        recomputed = sha256_hex(manifest)
        manifest_matches = (recomputed == receipt.get("manifest_sha256"))
        sig_valid = Signer.verify(receipt.get("manifest_sha256", ""),
                                  receipt.get("signature", ""),
                                  receipt.get("public_key", ""))
        # Signature validity alone is a weak claim: it only says the receipt is self-consistent with
        # whatever public key the receipt itself carries. Anyone can generate a keypair, sign a
        # forged envelope with it, and pass that check. Attribution requires comparing the key
        # against one obtained out of band — Episteme publishes its own at
        # /.well-known/episteme-signing-key, and the caller may pin any expected key here.
        presented_key = (receipt.get("public_key") or "").lower()
        expected = str(ctx.input.get("expected_public_key") or "").lower().strip()
        # Attribution is checked against the identity this service is actually running under —
        # whether that key came from the configured secret, the key file, or was generated. Narrowing
        # it to the secret-only case would leave Episteme unable to attribute its own receipts in any
        # deployment that had not been given a secret yet.
        signed_by_this_service = None
        try:
            from runtime import Signer as _S  # noqa: PLC0415 — avoids a circular import at module load

            live = _S(ctx.settings.signing_key_path,
                      getattr(ctx.settings, "_signing_key_hex", None))
            signed_by_this_service = presented_key == live.public_hex.lower()
        except Exception:  # noqa: BLE001 — inability to load the service key is not a verdict
            signed_by_this_service = None

        key_matches_expected = (presented_key == expected) if expected else None
        attributed = key_matches_expected if expected else signed_by_this_service

        if not (manifest_matches and sig_valid):
            verdict = "INVALID_OR_TAMPERED"
        elif attributed is False:
            # Cryptographically sound and issued by someone else — the most dangerous case, because
            # every individual check passes and only the identity is wrong.
            verdict = "VALID_SIGNATURE_UNKNOWN_ISSUER"
        elif attributed is True:
            verdict = "VALID"
        else:
            verdict = "VALID_UNATTRIBUTED"

        return {
            "receipt_present": True,
            "manifest_matches": manifest_matches,
            "signature_valid": sig_valid,
            # Kept as signature+manifest integrity, which is what the field has always meant.
            "receipt_valid": manifest_matches and sig_valid,
            "public_key": receipt.get("public_key"),
            "expected_public_key": expected or None,
            "key_matches_expected": key_matches_expected,
            "signed_by_this_service": signed_by_this_service,
            "attributed": attributed,
            "verification_level": level,
            "verdict": verdict,
            "note": "A valid signature proves integrity, not origin. Compare public_key against "
                    "/.well-known/episteme-signing-key (or pass expected_public_key) to establish "
                    "who issued the receipt.",
        }

    def validate(self, result, ctx):
        return [
            ValidationCheck(name="checked", passed="receipt_valid" in result),
            # A verdict of plain VALID must never be returned when the issuer is unverified — that
            # would be exactly the overclaim this node exists to prevent.
            ValidationCheck(
                name="verdict_does_not_overclaim",
                passed=not (result.get("verdict") == "VALID" and result.get("attributed") is not True),
                detail="'VALID' requires the signing key to be confirmed as the expected issuer",
            ),
            ValidationCheck(
                name="tampering_detected_when_manifest_differs",
                passed=(result.get("manifest_matches") is not False
                        or result.get("verdict") == "INVALID_OR_TAMPERED"),
            ),
        ]
