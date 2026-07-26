"""API pack (2): openapi.lint, openapi.diff, schema.generate."""
from __future__ import annotations

import json

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _parse_spec(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return json.loads(s)
        except Exception:
            try:
                import yaml
                return yaml.safe_load(s)
            except Exception as e:
                raise NodeError(ErrorCode.INVALID_INPUT, f"spec not JSON/YAML: {e}")
    raise NodeError(ErrorCode.INVALID_INPUT, "provide 'spec'")


def _ops(spec):
    out = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for m, op in item.items():
            if m.lower() in _METHODS and isinstance(op, dict):
                out.append((m.upper(), path, op))
    return out


class OpenApiLintNode(Node):
    name = "openapi.lint"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-spectral-lite"

    def run(self, ctx: NodeContext) -> dict:
        spec = _parse_spec(ctx.input.get("spec"))
        findings = []

        def add(sev, rule, loc, msg):
            findings.append({"severity": sev, "rule": rule, "location": loc, "message": msg})

        info = spec.get("info") or {}
        if not info.get("title"):
            add("error", "info-title", "info.title", "API must have a title")
        if not info.get("version"):
            add("warn", "info-version", "info.version", "API should declare a version")
        if not (spec.get("openapi") or spec.get("swagger")):
            add("error", "oas-version", "openapi", "missing openapi/swagger version")
        for method, path, op in _ops(spec):
            loc = f"{method} {path}"
            if not op.get("operationId"):
                add("warn", "operation-operationId", loc, "operation should have operationId")
            if not op.get("responses"):
                add("error", "operation-responses", loc, "operation must define responses")
            if not (op.get("summary") or op.get("description")):
                add("warn", "operation-description", loc, "operation should have summary/description")
        errors = sum(1 for f in findings if f["severity"] == "error")
        warns = sum(1 for f in findings if f["severity"] == "warn")
        return {"operation_count": len(_ops(spec)), "error_count": errors,
                "warning_count": warns, "findings": findings, "passed": errors == 0}

    def validate(self, result, ctx):
        return [ValidationCheck(name="linted", passed="findings" in result)]


class OpenApiDiffNode(Node):
    name = "openapi.diff"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-oasdiff-lite"

    def run(self, ctx: NodeContext) -> dict:
        old = _parse_spec(ctx.input.get("old"))
        new = _parse_spec(ctx.input.get("new"))
        old_ops = {f"{m} {p}" for m, p, _ in _ops(old)}
        new_ops = {f"{m} {p}" for m, p, _ in _ops(new)}
        added = sorted(new_ops - old_ops)
        removed = sorted(old_ops - new_ops)
        # removed operations are breaking changes
        breaking = bool(removed)
        return {"added": added, "removed": removed,
                "added_count": len(added), "removed_count": len(removed),
                "breaking": breaking,
                "verdict": "BREAKING" if breaking else ("CHANGED" if added else "COMPATIBLE")}

    def validate(self, result, ctx):
        return [ValidationCheck(name="diffed", passed="verdict" in result)]


class SchemaGenerateNode(Node):
    name = "schema.generate"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-schemagen"

    def run(self, ctx: NodeContext) -> dict:
        example = ctx.input.get("example")
        if example is None:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'example' (JSON value)")
        if isinstance(example, str):
            try:
                example = json.loads(example)
            except Exception:
                pass
        schema = _infer(example)
        return {"schema": schema}

    def validate(self, result, ctx):
        return [ValidationCheck(name="has_type", passed="type" in result.get("schema", {}))]


def _infer(v):
    if isinstance(v, bool):
        return {"type": "boolean"}
    if isinstance(v, int):
        return {"type": "integer"}
    if isinstance(v, float):
        return {"type": "number"}
    if isinstance(v, str):
        return {"type": "string"}
    if v is None:
        return {"type": "null"}
    if isinstance(v, list):
        if not v:
            return {"type": "array", "items": {}}
        return {"type": "array", "items": _infer(v[0])}
    if isinstance(v, dict):
        props = {k: _infer(val) for k, val in v.items()}
        return {"type": "object", "properties": props, "required": sorted(v.keys())}
    return {}
