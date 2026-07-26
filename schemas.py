"""Input schemas for every node, derived from the code that reads the input.

Why derive instead of hand-writing 47 schemas: a hand-maintained table drifts the moment someone
adds `ctx.input.get("depth")` and forgets the doc, and a schema that lies is worse than none — a
buying agent constructs a call from it, pays, and gets a 422. So the contract is read out of the
implementation itself (which keys are read, which raise when absent), then refined by an explicit
overlay for the handful of nodes whose real contract cannot be seen syntactically.

Published two ways:
  - `outputSchema.input` inside every 402 challenge  → an agent knows the body BEFORE paying
  - free `GET /schema/{endpoint}`                     → discoverable without a wallet
Peers that do this (Quiver's outputSchema, ScoutGate's MCP inputSchema) are the ones an agent can
actually call unattended; it is the single highest-value gap we had.
"""
from __future__ import annotations

import ast
import functools
import inspect
from typing import Any

# Keys whose meaning is fixed across the catalog. Anything not here still gets published (as an
# untyped property) — omitting a key we know is read would be the one unrecoverable mistake.
_KNOWN: dict[str, dict[str, Any]] = {
    "text": {"type": "string", "description": "UTF-8 text input."},
    "content_b64": {"type": "string", "description": "Base64-encoded bytes (alternative to 'text')."},
    "url": {"type": "string", "format": "uri", "description": "Public http(s) URL. Private/loopback addresses are refused."},
    "csv": {"type": "string", "description": "CSV document including its header row."},
    "html": {"type": "string", "description": "Raw HTML document."},
    "json": {"description": "A JSON value."},
    "spec": {"description": "An OpenAPI document, as an object or a JSON/YAML string."},
    "schema": {"type": "object", "description": "JSON Schema describing the desired output shape."},
    "envelope": {"type": "object", "description": "A complete Episteme result envelope to verify."},
    "rows": {"type": "array", "items": {"type": "object"}, "description": "Records, one object per row."},
    "a": {"type": "string", "description": "First input to compare."},
    "b": {"type": "string", "description": "Second input to compare."},
    "op": {"type": "string", "description": "Operation to perform."},
    "format": {"type": "string", "description": "Output format."},
    "to": {"type": "string", "description": "Target unit / format."},
    "value": {"type": "number", "description": "Numeric value."},
    "expected_sha256": {"type": "string", "description": "Hex sha256 to compare against (with or without the 'sha256:' prefix)."},
    "key": {"type": "string", "description": "Column that identifies a row, enabling per-field change detection."},
    "max_chars": {"type": "integer", "description": "Cap on returned characters."},
    "numparse": {"type": "boolean", "description": "Allow numeric cells to be reformatted. Default false — values are preserved verbatim."},
    "user_agent": {"type": "string", "description": "User-Agent to present when fetching."},
    "base_url": {"type": "string", "description": "Base URL to resolve relative links against."},
    "degrees": {"type": "number", "description": "Rotation in degrees."},
    "width": {"type": "integer", "description": "Target width in pixels."},
    "x": {"type": "string", "description": "Field for the x axis."},
    "y": {"type": "string", "description": "Field for the y axis."},
    "steps": {"type": "array", "items": {"type": "object"}, "description": "Ordered steps to execute."},
    "group_by": {"type": "array", "items": {"type": "string"}, "description": "Columns to group by."},
    "agg": {"type": "object", "description": "Aggregations as {column: function}."},
    "sql": {"type": "string", "description": "Read-only SQL SELECT over the supplied rows."},
    "seed": {"type": "integer", "description": "Seed — the same seed reproduces the same run exactly."},
}

# Nodes whose real contract is not visible syntactically (dispatch on op, alternative key sets, or a
# semantic requirement the code cannot express). Overlay wins over the derived result.
# Parameter groups shared by the helper-based nodes. Mirrors _csv_bytes, _rows_from_input and _files.
_CSV_PROPS: dict[str, Any] = {
    "csv": {"type": "string", "description": "CSV content, including the header row."},
    "text": {"type": "string", "description": "Same as 'csv': CSV content as plain text."},
    "content_b64": {"type": "string", "description": "Base64-encoded CSV file bytes."},
}
_TABLE_PROPS: dict[str, Any] = {
    "rows": {"type": "array", "items": {"type": "object"},
             "description": "The table as a list of row objects."},
    "json": {"description": "The table as a JSON array of objects, or that array as a string."},
    "jsonl": {"type": "string", "description": "One JSON object per line."},
    "csv": {"type": "string", "description": "The table as CSV, including the header row."},
}
_FILES_PROPS: dict[str, Any] = {
    "files": {"type": "object", "description": "Map of file path to file content, e.g. "
                                              '{"src/app.py": "import os\\n..."}.'},
    "text": {"type": "string", "description": "A single file's content; pair with 'path'."},
    "path": {"type": "string", "description": "Path to attribute 'text' to, e.g. \"src/app.py\"."},
}

_HTML_PROPS: dict[str, Any] = {
    "html": {"type": "string", "description": "Raw HTML to process."},
    "url": {"type": "string", "description": "Public https URL to fetch and process instead of 'html'."},
    "base_url": {"type": "string",
                 "description": "Base for resolving relative links; defaults to 'url' when fetching."},
}
_IMAGE_PROPS: dict[str, Any] = {
    "content_b64": {"type": "string", "description": "Base64-encoded image bytes."},
    "op": {"type": "string", "description": "Transform to apply, e.g. \"resize\"."},
    "width": {"type": "integer", "description": "Target width in pixels."},
    "height": {"type": "integer", "description": "Target height in pixels."},
    "format": {"type": "string", "description": "Output format, e.g. \"PNG\" or \"JPEG\"."},
}

_OVERLAY: dict[str, dict[str, Any]] = {
    "artifact.verify": {"required": ["expected_sha256"], "one_of": [["text", "content_b64"]]},
    "receipt.verify": {"required": ["envelope"]},
    "hash.compute": {"one_of": [["text", "content_b64"]]},
    "file.inspect": {"one_of": [["text", "content_b64"]]},
    "data.diff": {"required": ["a", "b"]},
    "text.diff": {"required": ["a", "b"]},
    "object.diff": {"required": ["a", "b"]},
    "document.compare": {"required": ["a", "b"]},
    # Reads `old` and `new`, not `a`/`b` — the previous entry named parameters this node has never
    # accepted, so anything built from the schema would have been rejected as INVALID_INPUT.
    "openapi.diff": {"required": ["old", "new"]},
    # These four are the only services backed by a language model, and none of the registered
    # descriptions says so. A buyer paying for "summarizes long text" has no way to know the result
    # depends on a third-party model that can be unavailable, non-deterministic between calls, or
    # subject to its own content policy — an undisclosed hard dependency. The registered copy cannot be
    # changed without an `agent update` (which re-triggers QA), so the disclosure goes everywhere a
    # caller can reach without one: the published schema, and the reply to a request with no input.
    "document.extract_json": {"required": ["text"], "ai_backed": True},
    "text.summarize": {"required": ["text"], "ai_backed": True},
    "workflow.compose": {"required": ["steps"]},

    # --- nodes whose primary input arrives through a helper -------------------------------------
    # These read their data via _csv_bytes / _files / _rows_from_input / _rows rather than a direct
    # ctx.input.get(), so the AST walker cannot see the parameter at all. The effect was that
    # csv.profile, data.stats, repo.map and repo.lint advertised an EMPTY schema — a buyer had no way
    # to discover how to send the data they were paying to have processed. `props` below states the
    # real contract; it is the only place these can be declared, so keep it in step with the helpers.
    "csv.profile": {"props": _CSV_PROPS, "one_of": [["csv", "text", "content_b64"]]},
    "csv.to_table": {"props": _CSV_PROPS, "one_of": [["csv", "text", "content_b64"]]},
    "data.query_sql": {"props": _CSV_PROPS, "required": ["sql"],
                       "one_of": [["csv", "text", "content_b64"]]},
    "data.stats": {"props": _TABLE_PROPS, "one_of": [["rows", "json", "jsonl", "csv"]]},
    "data.convert": {"props": _TABLE_PROPS, "required": ["to"],
                     "one_of": [["rows", "json", "jsonl", "csv"]]},
    "data.dedupe": {"props": _TABLE_PROPS, "one_of": [["rows", "json", "jsonl", "csv"]]},
    "data.validate": {"props": _TABLE_PROPS, "required": ["schema"],
                      "one_of": [["rows", "json", "jsonl", "csv"]]},
    "chart.spec": {"props": _TABLE_PROPS, "required": ["x", "y"],
                   "one_of": [["rows", "json", "jsonl", "csv"]]},
    "repo.map": {"props": _FILES_PROPS, "one_of": [["files", "text"]]},
    "repo.lint": {"props": _FILES_PROPS, "one_of": [["files", "text"]]},
    "repo.scan_secrets": {"props": _FILES_PROPS, "one_of": [["files", "text"]]},
    # url OR path, never both — listing both as required told a caller to send something the node
    # ignores, and the node used to default silently to "/" and report a false "allowed".
    "robots.check": {"required": ["robots"], "one_of": [["url", "path"]]},
    # Derivation marked all of rows/group_by/agg/value/values/field required, because each one raises
    # INVALID_INPUT somewhere in the function — but `agg` defaults to a count, and value/values/field
    # are three names for the same optional argument, needed only with the shorthand form. It also
    # typed `value` as a number when it is a COLUMN NAME. A caller obeying that schema sent nonsense.
    "data.pivot": {
        "props": {
            "value": {"type": "string",
                      "description": "Column to aggregate, for the shorthand form "
                                     '(agg="sum", value="amount"). Alias: "values".'},
            "agg": {"description": 'Either a function name — "sum", "count", "avg", "min", "max" — '
                                   'used with "value", or a mapping of column to function such as '
                                   '{"amount": "sum"}. Defaults to a row count.'},
        },
        "required": ["rows", "group_by"],
    },
    "page.links": {"props": _HTML_PROPS, "one_of": [["html", "url"]]},
    "page.extract": {"props": _HTML_PROPS, "required": ["selectors"],
                     "one_of": [["html", "url"]]},
    "url.to_markdown": {"props": _HTML_PROPS, "one_of": [["html", "url"]]},
    "document.to_markdown": {
        "props": {
            "text": {"type": "string", "description": "Document content as plain text."},
            "content_b64": {"type": "string",
                            "description": "Base64-encoded document bytes (PDF or text)."},
            "name": {"type": "string", "description": "Optional original filename, for reporting."},
        },
        "one_of": [["text", "content_b64"]],
    },
    "image.inspect": {"props": _IMAGE_PROPS, "required": ["content_b64"]},
    "image.transform": {"props": _IMAGE_PROPS, "required": ["content_b64", "op"]},
    "pdf.manipulate": {
        "props": {
            "content_b64": {"type": "string", "description": "Base64-encoded PDF bytes."},
            "op": {"type": "string", "enum": ["info", "extract_pages", "rotate",
                                              "remove_metadata", "split_first"],
                   "description": "The operation to perform."},
            "pages": {"description": 'Page selection for extract_pages: "1-3", "1,3,5", "2-" '
                                     "(to the end), a single number, or a list of numbers. "
                                     "Pages are 1-indexed."},
            "degrees": {"type": "integer", "description": "Rotation for op=rotate, e.g. 90."},
        },
        "required": ["content_b64", "op"],
    },
    "sim.run": {
        "props": {
            "topic": {"type": "string",
                      "description": "The scenario to simulate. A situation, not a real person."},
            "population": {"type": "integer", "description": "Number of agents (default 200)."},
            "rounds": {"type": "integer", "description": "Rounds to run, 1-200 (default 20)."},
            "seed": {"type": "integer", "description": "Seed, so a run is reproducible (default 42)."},
            "intervention_strength": {"type": "number",
                                      "description": "0.0-1.0 nudge applied during the run."},
        },
        "required": ["topic"],
        "ai_backed": True,      # personas and the narrative summary come from a language model
    },
}

# Stated once, so every surface says the same thing. `deterministic: false` on the node already hints
# at it, but "not deterministic" and "depends on a third-party model that may be unavailable" are
# different warnings, and only the second one tells a buyer what can actually go wrong.
AI_DISCLOSURE = (
    "This service is backed by a language model, so its output is not byte-reproducible between "
    "calls and it can return ENGINE_UNAVAILABLE if the model provider is unreachable. Every other "
    "Episteme service is deterministic."
)


def _keys_read(fn) -> tuple[list[str], list[str]]:
    """(all keys read, keys whose absence raises INVALID_INPUT) from a node's run() source."""
    try:
        tree = ast.parse(inspect.getsource(fn).lstrip())
    except (OSError, TypeError, SyntaxError, IndentationError):
        return [], []

    read: list[str] = []
    required: list[str] = []

    class V(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            # ctx.input.get("k") / ctx.input.get("k", default)
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "get"
                    and isinstance(f.value, ast.Attribute) and f.value.attr == "input"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                k = node.args[0].value
                if k not in read:
                    read.append(k)
                # no default supplied => the node expects it to be there
                if len(node.args) == 1 and not node.keywords and k not in required:
                    required.append(k)
            self.generic_visit(node)

        def visit_Compare(self, node: ast.Compare) -> None:
            # "k" in ctx.input  — a presence test, so the key is part of the contract
            if (len(node.ops) == 1 and isinstance(node.ops[0], ast.In)
                    and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)):
                comp = node.comparators[0]
                if isinstance(comp, ast.Attribute) and comp.attr == "input":
                    k = node.left.value
                    if k not in read:
                        read.append(k)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            # ctx.input["k"] — required by construction (KeyError otherwise)
            if (isinstance(node.value, ast.Attribute) and node.value.attr == "input"
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                k = node.slice.value
                if k not in read:
                    read.append(k)
                if k not in required:
                    required.append(k)
            self.generic_visit(node)

    V().visit(tree)
    return read, required


def schema_for(node) -> dict[str, Any]:
    """JSON Schema for one node's `input` object."""
    read, derived_required = _keys_read(node.run)
    overlay = _OVERLAY.get(node.name, {})

    props: dict[str, Any] = {}
    for k in read:
        props[k] = dict(_KNOWN.get(k, {"description": f"See the service description for '{k}'."}))

    # Parameters the AST cannot see because they are consumed inside a helper, plus corrections to
    # ones it inferred wrongly. The overlay is the authoritative statement of the contract, so it
    # OVERRIDES a derived guess rather than deferring to it — `_KNOWN` typed data.pivot's `value` as
    # a number from the name alone, when it is the name of the column to aggregate.
    for k, spec in (overlay.get("props") or {}).items():
        props[k] = dict(spec)

    one_of = overlay.get("one_of") or []
    for group in one_of:
        for k in group:
            props.setdefault(k, dict(_KNOWN.get(k, {"description": f"Input '{k}'."})))

    if "required" in overlay:
        required = [k for k in overlay["required"] if k in props]
    else:
        # A key in a one_of group is an alternative, so it is never unconditionally required.
        alt = {k for g in one_of for k in g}
        required = [k for k in derived_required if k in props and k not in alt]

    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if overlay.get("ai_backed"):
        schema["x-ai-backed"] = True
        schema["x-ai-disclosure"] = AI_DISCLOSURE
    if required:
        schema["required"] = required
    if one_of:
        schema["anyOf"] = [{"required": [k]} for g in one_of for k in g]
        schema["description"] = ("Provide exactly one of: "
                                 + " or ".join("'" + k + "'" for g in one_of for k in g) + ".")
    return schema


@functools.lru_cache(maxsize=1)
def _cache(registry_id: int) -> dict[str, dict[str, Any]]:  # pragma: no cover - keyed by identity
    return {}


def all_schemas(registry) -> dict[str, dict[str, Any]]:
    """{endpoint: input schema} for every node in the registry."""
    out: dict[str, dict[str, Any]] = {}
    for meta in registry.list():
        ep = meta["endpoint"]
        node = registry.get(ep)
        if node is None:
            continue
        try:
            out[ep] = schema_for(node)
        except Exception:
            # A schema we cannot derive is published as permissive rather than omitted or wrong:
            # "we don't know" is honest, "additionalProperties: false with no properties" is a lie.
            out[ep] = {"type": "object", "description": "Schema unavailable — see the service description."}
    return out


def request_schema(node) -> dict[str, Any]:
    """The full POST body shape: {"input": {...}, "options": {...}}."""
    return {
        "type": "object",
        "properties": {
            "input": schema_for(node),
            "options": {"type": "object", "description": "Optional execution options."},
        },
        "required": ["input"],
        "additionalProperties": False,
    }
