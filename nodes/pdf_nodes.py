"""PDF + document nodes: pdf.manipulate (pypdf), document.compare (redline)."""
from __future__ import annotations

import base64
import difflib
import io

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError


def _parse_pages(spec: object, n: int) -> list[int]:
    """Parse a page selection into a list of 1-indexed page numbers.

    Accepts "1-3", "1,3,5-7", "2-" (to the end), a list of numbers, or a single number.

    A range is how anyone naturally says which pages they want, and it was not supported at all:
    the old code iterated the *string* "1-2", so it called int('-') and crashed with a raw ValueError
    surfaced as ENGINE_FAILED. Ranges are the primary use case for extracting pages.
    """
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, list):
        out: list[int] = []
        for p in spec:
            try:
                out.append(int(p))
            except (TypeError, ValueError):
                raise NodeError(ErrorCode.INVALID_INPUT,
                                f"page selection contains a non-number: {p!r}")
        return out
    if not isinstance(spec, str):
        raise NodeError(ErrorCode.INVALID_INPUT,
                        f"'pages' must be a string like \"1-3\" or \"1,3,5\", a list of numbers, or a "
                        f"single number; got {type(spec).__name__}")

    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):  # a range, not a negative number
            lo_s, _, hi_s = part.partition("-")
            lo_s, hi_s = lo_s.strip(), hi_s.strip()
            try:
                lo = int(lo_s) if lo_s else 1
                hi = int(hi_s) if hi_s else n          # "2-" means from 2 to the end
            except ValueError:
                raise NodeError(ErrorCode.INVALID_INPUT,
                                f"invalid page range {part!r}; use forms like \"1-3\", \"2-\" or \"1,3,5\"")
            if lo > hi:
                raise NodeError(ErrorCode.INVALID_INPUT,
                                f"page range {part!r} runs backwards ({lo} > {hi})")
            pages.extend(range(lo, hi + 1))
        else:
            try:
                pages.append(int(part))
            except ValueError:
                raise NodeError(ErrorCode.INVALID_INPUT,
                                f"invalid page number {part!r}; use forms like \"1-3\", \"2-\" or \"1,3,5\"")
    if not pages:
        raise NodeError(ErrorCode.INVALID_INPUT, f"'pages' selected nothing: {spec!r}")
    return pages


class PdfManipulateNode(Node):
    name = "pdf.manipulate"
    price_usdt = 0.01
    deterministic = True
    engine = "pypdf"

    def engine_available(self) -> bool:
        try:
            import pypdf  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import pypdf
        if "content_b64" not in ctx.input:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide pdf 'content_b64'")
        data = base64.b64decode(ctx.input["content_b64"])
        if data[:5] != b"%PDF-":
            raise NodeError(ErrorCode.UNSUPPORTED_FORMAT, "not a PDF")
        op = str(ctx.input.get("op", "info"))
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
        except Exception as e:
            raise NodeError(ErrorCode.ENGINE_FAILED, f"pdf read failed: {e}")
        n = len(reader.pages)

        if op == "info":
            meta = {k[1:] if k.startswith("/") else k: str(v)
                    for k, v in (reader.metadata or {}).items()}
            return {"op": "info", "pages": n, "metadata": meta,
                    "encrypted": reader.is_encrypted}

        writer = pypdf.PdfWriter()
        if op == "extract_pages":
            spec = ctx.input.get("pages")
            wanted = _parse_pages(spec, n) if spec not in (None, "") else list(range(1, n + 1))
            # An out-of-range page used to be skipped in silence, so asking for pages 5-7 of a
            # three-page document returned an EMPTY pdf on a paid call with nothing to indicate the
            # request had not been honoured.
            out_of_range = [p for p in wanted if not 1 <= p <= n]
            if out_of_range:
                raise NodeError(ErrorCode.INVALID_INPUT,
                                f"page(s) {out_of_range} are out of range; the document has {n} page"
                                f"{'s' if n != 1 else ''} (pages are 1-indexed)")
            for p in wanted:
                writer.add_page(reader.pages[p - 1])
        elif op == "rotate":
            deg = int(ctx.input.get("degrees", 90))
            for page in reader.pages:
                page.rotate(deg)
                writer.add_page(page)
        elif op == "remove_metadata":
            for page in reader.pages:
                writer.add_page(page)
            writer.add_metadata({})  # strip
        elif op == "split_first":
            writer.add_page(reader.pages[0])
        else:
            raise NodeError(ErrorCode.INVALID_INPUT, f"unknown op '{op}'")

        buf = io.BytesIO()
        writer.write(buf)
        out = buf.getvalue()
        import hashlib
        ref = ctx.add_artifact("output.pdf", out, "application/pdf")
        return {"op": op, "in_pages": n, "out_pages": len(writer.pages),
                "out_bytes": len(out), "out_sha256": hashlib.sha256(out).hexdigest(),
                "artifact_uri": ref.uri}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="pages_present", passed=result.get("pages", result.get("in_pages", 0)) >= 1)]


class DocumentCompareNode(Node):
    name = "document.compare"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-redline"

    def run(self, ctx: NodeContext) -> dict:
        a = str(ctx.input.get("a", ""))
        b = str(ctx.input.get("b", ""))
        if not a and not b:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'a' and/or 'b'")
        a_lines = a.splitlines()
        b_lines = b.splitlines()
        sm = difflib.SequenceMatcher(None, a_lines, b_lines)
        segments, added, removed, changed = [], 0, 0, 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            seg = {"op": tag, "a": a_lines[i1:i2], "b": b_lines[j1:j2]}
            segments.append(seg)
            if tag == "insert":
                added += (j2 - j1)
            elif tag == "delete":
                removed += (i2 - i1)
            elif tag == "replace":
                changed += max(i2 - i1, j2 - j1)
        redline = "".join(difflib.unified_diff(a.splitlines(keepends=True),
                                               b.splitlines(keepends=True), "a", "b"))
        return {
            "similarity": round(sm.ratio(), 6),
            "added_lines": added, "removed_lines": removed, "changed_lines": changed,
            "segments": segments[:500],
            "redline": redline,
            "identical": a == b,
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="similarity_ok", passed=0.0 <= result.get("similarity", -1) <= 1.0)]
