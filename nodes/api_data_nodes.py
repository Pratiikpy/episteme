"""More deterministic nodes: openapi.inspect, data.transform_json, document.chunk."""
from __future__ import annotations

import json
import re

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

# Separators tried in order, coarsest first: keep a paragraph whole if it fits, else a line, else a
# sentence, else a word. Only when a single word exceeds chunk_size do we cut inside it. This is the
# standard recursive-splitter ordering and it is what stops chunks from ending mid-token.
_CHUNK_SEPARATORS = [
    re.compile(r"\n\s*\n"),      # paragraph
    re.compile(r"\n"),           # line
    re.compile(r"(?<=[.!?])\s+"),  # sentence
    re.compile(r"\s+"),          # word
]


def _snap_back_to_word(text: str, pos: int, floor: int) -> int:
    """Move `pos` back to the start of the token it lands inside, never below `floor`.

    Snapping backwards rather than forwards is deliberate: rounding forward to the next token would
    systematically shrink the overlap, and under-delivering it is the contract violation this whole
    rewrite exists to fix. The delivered overlap can still land a few characters under the request
    when the leading whitespace of the token is skipped — bounded by one token, and reported
    per-chunk in `overlap_applied_chars` so the caller can see exactly what they got."""
    pos = max(floor, min(pos, len(text)))
    limit = max(floor, pos - 200)  # never walk back further than a long word
    while pos > limit and not text[pos - 1].isspace():
        pos -= 1
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return max(floor, pos)


def _split_recursive(text: str, size: int, _depth: int = 0) -> list[tuple[int, int]]:
    """Split `text` into (start, end) spans that each fit within `size`, preferring the coarsest
    separator that achieves it. Spans are returned in document order and tile the whole input, so
    a chunk built from consecutive spans can always be recovered as a plain slice of the source."""
    spans: list[tuple[int, int]] = []

    def _emit(start: int, end: int, depth: int) -> None:
        # Trim surrounding whitespace off the span itself rather than the string, so offsets stay
        # true to the original document.
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            return
        if end - start <= size:
            spans.append((start, end))
            return
        if depth >= len(_CHUNK_SEPARATORS):
            # No separator left: a single unbroken token longer than chunk_size. Hard-cut it, which
            # is the only option, and the one case where a mid-word split is unavoidable.
            for i in range(start, end, size):
                spans.append((i, min(i + size, end)))
            return
        sep = _CHUNK_SEPARATORS[depth]
        cursor = start
        found = False
        for m in sep.finditer(text, start, end):
            if m.start() <= cursor:  # separator at the very beginning — nothing to emit yet
                cursor = max(cursor, m.end())
                continue
            found = True
            _emit(cursor, m.start(), depth + 1)
            cursor = m.end()
        if not found:
            _emit(start, end, depth + 1)  # this separator does not occur; try a finer one
            return
        _emit(cursor, end, depth + 1)

    _emit(0, len(text), _depth)
    return spans


class OpenApiInspectNode(Node):
    name = "openapi.inspect"
    price_usdt = 0.002
    deterministic = True
    engine = "episteme-openapi"

    def run(self, ctx: NodeContext) -> dict:
        spec = ctx.input.get("spec")
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception as e:
                raise NodeError(ErrorCode.INVALID_INPUT, f"spec must be JSON: {e}")
        if not isinstance(spec, dict):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'spec' (OpenAPI JSON object or string)")
        version = spec.get("openapi") or spec.get("swagger") or "unknown"
        paths = spec.get("paths", {}) or {}
        ops = []
        methods = {"get", "post", "put", "delete", "patch", "head", "options"}
        for path, item in paths.items():
            if not isinstance(item, dict):
                continue
            for m, op in item.items():
                if m.lower() in methods and isinstance(op, dict):
                    ops.append({"method": m.upper(), "path": path,
                                "operationId": op.get("operationId"),
                                "summary": op.get("summary")})
        comps = spec.get("components", {}) or {}
        schemes = list((comps.get("securitySchemes") or {}).keys()) or list((spec.get("securityDefinitions") or {}).keys())
        return {
            "openapi_version": version,
            "title": (spec.get("info") or {}).get("title"),
            "operation_count": len(ops),
            "operations": ops[:500],
            "path_count": len(paths),
            "security_schemes": schemes,
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="counted_ops", passed="operation_count" in result)]


class DataTransformJsonNode(Node):
    name = "data.transform_json"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-jsontransform"

    def run(self, ctx: NodeContext) -> dict:
        data = ctx.input.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as e:
                raise NodeError(ErrorCode.INVALID_INPUT, f"'data' must be JSON: {e}")
        if data is None:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'data' (JSON)")
        op = str(ctx.input.get("op", "identity"))
        result = self._apply(op, data, ctx.input)
        return {"op": op, "result": result,
                "result_type": type(result).__name__}

    def _apply(self, op, data, inp):
        if op == "identity":
            return data
        if op == "keys":
            return list(data.keys()) if isinstance(data, dict) else []
        if op == "values":
            return list(data.values()) if isinstance(data, dict) else []
        if op == "length":
            return len(data) if isinstance(data, (list, dict, str)) else None
        if op == "pick":
            fields = inp.get("fields", [])
            if isinstance(data, dict):
                return {k: data.get(k) for k in fields}
            if isinstance(data, list):
                return [{k: (row.get(k) if isinstance(row, dict) else None) for k in fields} for row in data]
        if op == "flatten":
            return self._flatten(data)
        if op == "select":  # list of dicts where field == value
            field, value = inp.get("field"), inp.get("value")
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict) and r.get(field) == value]
        raise NodeError(ErrorCode.INVALID_INPUT, f"unknown op '{op}'")

    def _flatten(self, obj, prefix=""):
        out = {}
        if isinstance(obj, dict):
            if not obj:
                # An empty container is a fact about the data, not an absence of data. Recursing
                # into it emitted nothing at all, so `{"tags": [], "meta": {}}` flattened to `{}`
                # and both keys disappeared with no warning — and flatten stopped being invertible,
                # because nothing in the output records that the keys ever existed.
                out[prefix.rstrip(".")] = {}
            for k, v in obj.items():
                out.update(self._flatten(v, f"{prefix}{k}."))
        elif isinstance(obj, list):
            if not obj:
                out[prefix.rstrip(".")] = []
            for i, v in enumerate(obj):
                out.update(self._flatten(v, f"{prefix}{i}."))
        else:
            out[prefix.rstrip(".")] = obj
        return out

    def _unflatten(self, flat: dict):
        """Inverse of _flatten: rebuild nesting from dotted keys, restoring lists where the key
        segments are contiguous integers from 0."""
        root: dict = {}
        for key, value in flat.items():
            parts = str(key).split(".") if key else [""]
            cur = root
            for part in parts[:-1]:
                nxt = cur.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[part] = nxt
                cur = nxt
            cur[parts[-1]] = value

        def _listify(node):
            if not isinstance(node, dict):
                return node
            node = {k: _listify(v) for k, v in node.items()}
            if node and all(k.isdigit() for k in node):
                idx = sorted(int(k) for k in node)
                if idx == list(range(len(idx))):
                    return [node[str(i)] for i in idx]
            return node

        return _listify(root)

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        checks = [ValidationCheck(name="has_result", passed="result" in result)]
        # Flatten claims to be a lossless restructuring. Prove it on every call by inverting the
        # output and comparing against the input — the check that would have caught empty
        # containers being dropped.
        if str(ctx.input.get("op", "")) == "flatten" and isinstance(result.get("result"), dict):
            try:
                round_tripped = self._unflatten(result["result"])
                lossless = round_tripped == ctx.input.get("data")
            except Exception:  # noqa: BLE001
                lossless = False
            checks.append(ValidationCheck(
                name="flatten_is_lossless", passed=lossless,
                detail="un-flattening the output reproduces the input exactly",
            ))
        return checks


class DocumentChunkNode(Node):
    name = "document.chunk"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-chunk"

    def run(self, ctx: NodeContext) -> dict:
        text = str(ctx.input.get("text", ""))
        if not text.strip():
            raise NodeError(ErrorCode.INVALID_INPUT, "provide non-empty 'text'")
        size = int(ctx.input.get("chunk_size", 800))
        overlap = int(ctx.input.get("overlap", 100))
        if size <= 0 or overlap < 0 or overlap >= size:
            raise NodeError(ErrorCode.INVALID_INPUT, "require chunk_size>0 and 0<=overlap<chunk_size")

        # Recursive-separator split, then greedy pack with REAL overlap between every adjacent pair.
        #
        # The previous implementation applied `overlap` only when a single paragraph exceeded
        # chunk_size. On ordinary prose — every paragraph shorter than chunk_size, which is the normal
        # case — adjacent chunks overlapped by zero characters despite the caller asking and paying
        # for overlap. Overlap is the whole reason this node exists: without it a retrieved chunk can
        # begin mid-argument with its referent stranded in the previous chunk, which is exactly the
        # failure mode overlap prevents in a RAG pipeline. It also split mid-word (`c[i:i+size]`),
        # cutting tokens in half.
        # Two-stage: pack NON-OVERLAPPING windows of (size - overlap), then extend each window's
        # start backwards by `overlap`. Overlap is therefore added context on top of a fixed stride,
        # which is what makes the algorithm terminate cleanly and honour both parameters at once.
        #
        # The obvious alternative — begin each chunk `overlap` chars before the previous end and pack
        # greedily from there — looks right and is not: whenever a chunk comes out shorter than the
        # overlap (easy, since chunks end on paragraph boundaries), the next start cannot advance by a
        # full stride without leaving a gap, so it advances a single character and the document
        # shatters into dozens of near-identical chunks. Measured: a 600-char document with
        # size=200/overlap=60 produced 51 chunks, most of them 1 char apart.
        effective = size - overlap  # > 0: validated above (overlap < size)
        pieces = _split_recursive(text, effective)
        if not pieces:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide non-empty 'text'")

        windows: list[tuple[int, int]] = []
        ws, we = pieces[0]
        for ps, pe in pieces[1:]:
            if pe - ws <= effective:
                we = pe
            else:
                windows.append((ws, we))
                ws, we = ps, pe
        windows.append((ws, we))

        chunks: list[dict] = []
        for i, (s, e) in enumerate(windows):
            if i == 0 or overlap == 0:
                cs = s
            else:
                # Reach back for the requested overlap, on a word boundary, and never so far that
                # the chunk would exceed chunk_size.
                cs = _snap_back_to_word(text, max(0, s - overlap), max(0, e - size))
            chunks.append({"index": i, "start": cs, "end": e, "text": text[cs:e]})

        # Measure the overlap actually delivered rather than assuming the loop above worked.
        overlaps = [chunks[i - 1]["end"] - chunks[i]["start"] for i in range(1, len(chunks))]
        return {
            "chunk_count": len(chunks),
            "chunks": [c["text"] for c in chunks],          # unchanged shape for existing callers
            "chunks_detailed": chunks,                       # offsets, so a citation can point back
            "total_chars": len(text),
            "chunk_size": size,
            "overlap_requested": overlap,
            "overlap_applied_chars": overlaps,
            "min_overlap_applied": min(overlaps) if overlaps else 0,
            "max_chunk_chars": max((len(c["text"]) for c in chunks), default=0),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        chunks = result.get("chunks_detailed", [])
        size = result.get("chunk_size", 0)
        want = result.get("overlap_requested", 0)
        overlaps = result.get("overlap_applied_chars", [])
        # Full coverage: no region of the document may be dropped. A gap is only acceptable if it
        # contains nothing but whitespace — that is the separator between two chunks, which is
        # trimmed by design. A gap holding any actual character is silent data loss, and the buyer
        # would have no way to know a paragraph had vanished from their index.
        src = str(ctx.input.get("text", ""))
        no_gaps = all(
            chunks[i]["start"] <= chunks[i - 1]["end"]
            or not src[chunks[i - 1]["end"]:chunks[i]["start"]].strip()
            for i in range(1, len(chunks))
        )
        covers_start = (chunks[0]["start"] == 0) if chunks else False
        covers_end = (chunks[-1]["end"] == result.get("total_chars", -1)) if chunks else False
        return [
            ValidationCheck(name="has_chunks", passed=result.get("chunk_count", 0) >= 1),
            ValidationCheck(name="no_gaps_between_chunks", passed=no_gaps,
                            detail="every chunk starts at or before the previous chunk's end"),
            ValidationCheck(name="covers_whole_document", passed=covers_start and covers_end,
                            detail="chunk offsets span the entire input, first char to last"),
            ValidationCheck(name="respects_chunk_size",
                            passed=result.get("max_chunk_chars", 0) <= size if size else False),
            # The contract the old code broke: if overlap was requested and there is more than one
            # chunk, real overlap must be present between every adjacent pair.
            ValidationCheck(name="overlap_honoured",
                            passed=(want == 0 or not overlaps or min(overlaps) > 0),
                            detail=f"requested {want} chars; applied {overlaps[:5] or 'n/a (single chunk)'}"),
        ]
