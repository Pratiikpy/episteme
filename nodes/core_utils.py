"""Deterministic, dependency-free endpoint nodes (top-tier, fully testable)."""
from __future__ import annotations

import base64
import difflib
import hashlib
from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

_MAGIC = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"RIFF", "image/webp"),
    (b"{", "application/json"),
    (b"<!DOCTYPE html", "text/html"),
    (b"<html", "text/html"),
]


def _decode_input(inp: dict) -> bytes:
    if "content_b64" in inp:
        try:
            return base64.b64decode(inp["content_b64"], validate=True)
        except Exception as e:
            raise NodeError(ErrorCode.INVALID_INPUT, f"invalid base64: {e}")
    if "text" in inp:
        return str(inp["text"]).encode("utf-8")
    raise NodeError(ErrorCode.INVALID_INPUT, "provide 'text' or 'content_b64'")


def _sniff_mime(data: bytes) -> str:
    for sig, mime in _MAGIC:
        if data.startswith(sig):
            return mime
    try:
        data.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


class FileInspectNode(Node):
    name = "file.inspect"
    price_usdt = 0.001
    deterministic = True
    engine = "episteme-inspect"

    def run(self, ctx: NodeContext) -> dict:
        data = _decode_input(ctx.input)
        if len(data) > ctx.settings.max_input_bytes:
            raise NodeError(ErrorCode.LIMIT_EXCEEDED, "input exceeds max size")
        mime = _sniff_mime(data)
        is_text = mime.startswith("text/") or mime == "application/json"
        digest = hashlib.sha256(data).hexdigest()
        return {
            "size_bytes": len(data),
            "mime_type": mime,
            "is_text": is_text,
            "sha256": digest,
            "md5": hashlib.md5(data).hexdigest(),
            # differential claim: the content digest, recomputed independently by verifier
            "verify_key": digest,
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="has_sha256", passed=len(result.get("sha256", "")) == 64),
            ValidationCheck(name="size_nonnegative", passed=result.get("size_bytes", -1) >= 0),
            ValidationCheck(name="mime_detected", passed=bool(result.get("mime_type"))),
        ]


class FileInspectVerifier(Node):
    """Independent engine: recompute the content digest via a chunked/streaming path."""

    name = "file.inspect.verify"
    engine = "episteme-inspect-alt"
    deterministic = True

    def run(self, ctx: NodeContext) -> dict:
        data = _decode_input(ctx.input)
        h = hashlib.sha256()
        for i in range(0, len(data), 4096):  # independent chunked method
            h.update(data[i : i + 4096])
        return {"verify_key": h.hexdigest()}


class HashComputeNode(Node):
    name = "hash.compute"
    price_usdt = 0.001
    deterministic = True
    engine = "episteme-hash"
    _ALGOS = {"md5", "sha1", "sha256", "sha512", "blake2b"}

    def run(self, ctx: NodeContext) -> dict:
        data = _decode_input(ctx.input)
        algos = ctx.input.get("algos") or ["sha256"]
        bad = [a for a in algos if a not in self._ALGOS]
        if bad:
            raise NodeError(ErrorCode.INVALID_INPUT, f"unsupported algos: {bad}")
        digests = {a: hashlib.new(a, data).hexdigest() for a in algos}
        return {"digests": digests, "size_bytes": len(data)}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="has_digests", passed=bool(result.get("digests")))]


class ArtifactVerifyNode(Node):
    name = "artifact.verify"
    price_usdt = 0.001
    deterministic = True
    engine = "episteme-verify"

    def run(self, ctx: NodeContext) -> dict:
        data = _decode_input(ctx.input)
        expected = str(ctx.input.get("expected_sha256", "")).replace("sha256:", "")
        actual = hashlib.sha256(data).hexdigest()
        matches = bool(expected) and (expected == actual)
        return {
            "expected_sha256": expected or None,
            "actual_sha256": actual,
            "matches": matches,
            "verdict": "MATCH" if matches else ("MISMATCH" if expected else "NO_EXPECTED_HASH"),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="computed_actual", passed=len(result.get("actual_sha256", "")) == 64)]


class TextDiffNode(Node):
    name = "text.diff"
    price_usdt = 0.001
    deterministic = True
    engine = "episteme-textdiff"

    def run(self, ctx: NodeContext) -> dict:
        a = str(ctx.input.get("a", ""))
        b = str(ctx.input.get("b", ""))
        # Split WITHOUT keepends and join with newlines, rather than keepends + "".join.
        #
        # With keepends, a final line that has no trailing newline comes back without one, so
        # "".join fused it onto the following diff line — the measured output ran the last removed
        # line and the last added line together on one line, which is not a valid unified diff and
        # cannot be fed to `patch`. lineterm="" tells difflib not to add its own terminators either.
        a_lines, b_lines = a.splitlines(), b.splitlines()
        udiff = "\n".join(difflib.unified_diff(a_lines, b_lines, "a", "b", lineterm=""))
        sm = difflib.SequenceMatcher(None, a, b)
        # Count from the line-level opcodes. The old `l not in a_lines` test asked "does this line
        # appear ANYWHERE in the other text", so a line moved from one place to another counted as
        # neither added nor removed, and a line duplicated three times counted once.
        lm = difflib.SequenceMatcher(None, a_lines, b_lines)
        added = removed = 0
        for tag, i1, i2, j1, j2 in lm.get_opcodes():
            if tag in ("insert", "replace"):
                added += j2 - j1
            if tag in ("delete", "replace"):
                removed += i2 - i1
        return {
            "unified_diff": udiff,
            "similarity": round(sm.ratio(), 6),
            "added_lines": added,
            "removed_lines": removed,
            "changed": a != b,
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        s = result.get("similarity", -1)
        return [ValidationCheck(name="similarity_in_range", passed=0.0 <= s <= 1.0)]


class UnitConvertNode(Node):
    name = "unit.convert"
    price_usdt = 0.002
    deterministic = True
    engine = "episteme-units"

    # base units: meter, gram, second, byte
    _FACTORS = {
        # length -> meter
        "m": ("length", 1.0), "km": ("length", 1000.0), "cm": ("length", 0.01),
        "mm": ("length", 0.001), "mi": ("length", 1609.344), "ft": ("length", 0.3048),
        "in": ("length", 0.0254),
        # mass -> gram
        "g": ("mass", 1.0), "kg": ("mass", 1000.0), "mg": ("mass", 0.001),
        "lb": ("mass", 453.59237), "oz": ("mass", 28.349523125),
        # time -> second
        "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0), "day": ("time", 86400.0),
        # data -> byte
        "b": ("data", 1.0), "kb": ("data", 1000.0), "mb": ("data", 1e6), "gb": ("data", 1e9),
        "kib": ("data", 1024.0), "mib": ("data", 1024.0 ** 2), "gib": ("data", 1024.0 ** 3),
    }

    # People write "celsius" and "meters", not "c" and "m". Rejecting the spelled-out name of a unit
    # this node already converts is a refusal with nothing behind it — the caller pays, is told the
    # unit is unknown, and has no way to discover that the symbol would have worked. Plurals are
    # stripped before lookup, so "kilometers" and "pounds" resolve without a table entry each.
    _ALIASES = {
        "celsius": "c", "centigrade": "c", "fahrenheit": "f", "kelvin": "k",
        "degc": "c", "degf": "f", "°c": "c", "°f": "f",
        "meter": "m", "metre": "m", "kilometer": "km", "kilometre": "km",
        "centimeter": "cm", "centimetre": "cm", "millimeter": "mm", "millimetre": "mm",
        "mile": "mi", "foot": "ft", "feet": "ft", "inch": "in", "inche": "in",
        "gram": "g", "gramme": "g", "kilogram": "kg", "kilo": "kg", "milligram": "mg",
        "pound": "lb", "lbs": "lb", "ounce": "oz",
        "second": "s", "sec": "s", "minute": "min", "hour": "h", "hr": "h", "days": "day",
        "byte": "b", "kilobyte": "kb", "megabyte": "mb", "gigabyte": "gb",
        "kibibyte": "kib", "mebibyte": "mib", "gibibyte": "gib",
    }

    @classmethod
    def _canonical(cls, unit: str) -> str:
        u = unit.strip().lower().rstrip(".")
        if u in cls._ALIASES or u in cls._FACTORS:
            return cls._ALIASES.get(u, u)
        singular = u[:-1] if u.endswith("s") else u
        return cls._ALIASES.get(singular, singular)

    def run(self, ctx: NodeContext) -> dict:
        try:
            value = float(ctx.input["value"])
        except (KeyError, ValueError, TypeError):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide numeric 'value'")
        src = self._canonical(str(ctx.input.get("from", "")))
        dst = self._canonical(str(ctx.input.get("to", "")))
        # temperature handled separately
        if src in {"c", "f", "k"} or dst in {"c", "f", "k"}:
            return self._temp(value, src, dst)
        if src not in self._FACTORS or dst not in self._FACTORS:
            unknown = [u for u in (src, dst) if u not in self._FACTORS]
            supported = ", ".join(sorted(self._FACTORS) + ["c", "f", "k"])
            raise NodeError(
                ErrorCode.INVALID_INPUT,
                f"unknown unit(s): {', '.join(unknown)}. Supported: {supported}. "
                f"Full names and plurals are accepted, e.g. 'celsius', 'kilometers'.")
        dim_s, fs = self._FACTORS[src]
        dim_d, fd = self._FACTORS[dst]
        if dim_s != dim_d:
            raise NodeError(ErrorCode.INVALID_INPUT, f"incompatible dimensions {dim_s}!={dim_d}")
        out = value * fs / fd
        return {"value": value, "from": src, "to": dst, "result": out, "dimension": dim_s}

    def _temp(self, value: float, src: str, dst: str) -> dict:
        if src not in {"c", "f", "k"} or dst not in {"c", "f", "k"}:
            raise NodeError(ErrorCode.INVALID_INPUT, "temperature needs c/f/k on both sides")
        c = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[src]
        out = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[dst]
        return {"value": value, "from": src, "to": dst, "result": round(out, 6), "dimension": "temperature"}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="numeric_result", passed=isinstance(result.get("result"), (int, float)))]
