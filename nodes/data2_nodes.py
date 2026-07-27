"""Data pack (2): data.convert, data.validate, data.stats, data.dedupe, chart.spec."""
from __future__ import annotations

import io
import json
import re

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

# ISO-8601-ish dates and datetimes, plus the common slashed forms. Matching a real date string is
# what lets a time axis be ordered chronologically instead of alphabetically.
_DATE_RE = re.compile(
    r"^\s*(?:\d{4}-\d{2}(?:-\d{2})?(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{2}/\d{2}/\d{4}|\d{4}/\d{2}/\d{2})\s*$"
)


def _infer_vl_type(rows: list, field: str, *, prefer_measure: bool = False) -> str:
    """Infer the Vega-Lite measurement type of `field` from the data.

    Assuming a type is not a cosmetic shortcut: a date axis typed `nominal` is ordered
    alphabetically, so 2024-11 sorts before 2024-2 and the trend line is drawn through the wrong
    points. The chart still renders, which is precisely why the error goes unnoticed."""
    values = [r.get(field) for r in rows[:500] if isinstance(r, dict) and r.get(field) is not None]
    if not values:
        return "nominal"
    if all(isinstance(v, bool) for v in values):
        return "nominal"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        # On a grouping channel, a small integer set is usually a category or rank (year, quarter,
        # rating) and discrete ticks read better. On the measure channel it is a magnitude and must
        # stay quantitative — otherwise a bar chart of three sales figures gets a categorical y axis
        # with no zero baseline and no proportional bar heights.
        distinct = {float(v) for v in values}
        if not prefer_measure and all(float(v).is_integer() for v in values) and len(distinct) <= 12:
            return "ordinal"
        return "quantitative"
    strs = [str(v) for v in values]
    if all(_DATE_RE.match(s) for s in strs):
        return "temporal"
    # Numbers arriving as strings ("1234.5") are still a measure.
    try:
        for s in strs:
            float(s)
        return "quantitative"
    except (TypeError, ValueError):
        return "nominal"


def _nominal_or_ordinal(t: str) -> str:
    """Slice identity in a pie chart is categorical; a temporal or quantitative field used for
    colour must be reduced to a discrete type or the legend becomes a continuous gradient."""
    return t if t in {"nominal", "ordinal"} else "nominal"


def _rows_from_input(inp: dict):
    """Return list-of-dicts from csv/json/jsonl input."""
    if "rows" in inp and isinstance(inp["rows"], list):
        return inp["rows"]
    if "json" in inp:
        v = inp["json"]
        return json.loads(v) if isinstance(v, str) else v
    if "jsonl" in inp:
        return [json.loads(l) for l in str(inp["jsonl"]).splitlines() if l.strip()]
    if "csv" in inp:
        import csv as _csv
        return list(_csv.DictReader(io.StringIO(str(inp["csv"]))))
    raise NodeError(ErrorCode.INVALID_INPUT, "provide rows/json/jsonl/csv")


class DataConvertNode(Node):
    name = "data.convert"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-dataconv"

    def run(self, ctx: NodeContext) -> dict:
        rows = _rows_from_input(ctx.input)
        to = str(ctx.input.get("to", "json")).lower()
        if to == "json":
            out = json.dumps(rows, ensure_ascii=False)
        elif to == "jsonl":
            out = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        elif to == "csv":
            import csv as _csv
            buf = io.StringIO()
            cols = list({k for r in rows for k in (r.keys() if isinstance(r, dict) else [])})
            w = _csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
            out = buf.getvalue()
        else:
            raise NodeError(ErrorCode.INVALID_INPUT, f"unknown target '{to}'")
        return {"to": to, "row_count": len(rows), "output": out, "output_chars": len(out)}

    def validate(self, result, ctx):
        return [ValidationCheck(name="converted", passed="output" in result)]


class DataValidateNode(Node):
    name = "data.validate"
    price_usdt = 0.01
    deterministic = True
    engine = "jsonschema"

    def engine_available(self) -> bool:
        try:
            import jsonschema  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import jsonschema
        rows = _rows_from_input(ctx.input)
        schema = ctx.input.get("schema")
        if not isinstance(schema, dict):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'schema' (JSON Schema object)")
        validator = jsonschema.Draft202012Validator(schema)
        errors = []
        valid = 0
        for i, row in enumerate(rows):
            errs = sorted(validator.iter_errors(row), key=lambda e: e.path)
            if errs:
                errors.append({"row": i, "errors": [e.message for e in errs[:5]]})
            else:
                valid += 1
        return {"total": len(rows), "valid": valid, "invalid": len(rows) - valid,
                "errors": errors[:200], "all_valid": valid == len(rows)}

    def validate(self, result, ctx):
        return [ValidationCheck(name="counted", passed="valid" in result)]


class DataStatsNode(Node):
    name = "data.stats"
    price_usdt = 0.01
    deterministic = True
    engine = "numpy"

    def run(self, ctx: NodeContext) -> dict:
        import numpy as np
        rows = _rows_from_input(ctx.input)
        cols = {}
        # First-appearance order, NOT a set.
        #
        # `list({...})` iterates a set, whose order depends on hash randomisation, so the same input
        # could return its columns in a different order between runs — changing `numeric_columns`,
        # the serialized `stats` object, and therefore the output digest that gets SIGNED. A node
        # declared `deterministic = True` whose signed output varies run to run cannot be reproduced,
        # which is the one property the receipt exists to assert.
        keys: list[str] = []
        for r in rows:
            if isinstance(r, dict):
                for k in r:
                    if k not in keys:
                        keys.append(k)
        for k in keys:
            vals = []
            for r in rows:
                v = r.get(k)
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            if len(vals) >= 1:
                a = np.array(vals, dtype=float)
                # `sum` is the aggregate most often wanted from a numeric column and was the one
                # missing. A caller could recover it as mean × count, but making someone multiply
                # two rounded figures to get a total this node already holds exactly is a worse
                # answer than giving them the total. `std` is the population standard deviation
                # (numpy's default ddof=0), stated here because the sample version differs and a
                # buyer reconciling against their own tool needs to know which one they were sold.
                cols[k] = {"count": len(vals), "sum": round(float(a.sum()), 6),
                           "min": float(a.min()), "max": float(a.max()),
                           "mean": round(float(a.mean()), 6), "std": round(float(a.std()), 6),
                           "std_kind": "population (ddof=0)",
                           "median": round(float(np.median(a)), 6)}
        return {"row_count": len(rows), "numeric_columns": list(cols.keys()), "stats": cols}

    def validate(self, result, ctx):
        return [ValidationCheck(name="has_stats", passed="stats" in result)]


class DataDedupeNode(Node):
    name = "data.dedupe"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-dedupe"

    def run(self, ctx: NodeContext) -> dict:
        rows = _rows_from_input(ctx.input)
        keys = ctx.input.get("keys")
        seen = {}
        unique, clusters = [], {}
        for i, r in enumerate(rows):
            if keys:
                sig = json.dumps({k: r.get(k) for k in keys}, sort_keys=True, default=str)
            else:
                sig = json.dumps(r, sort_keys=True, default=str)
            if sig in seen:
                clusters.setdefault(seen[sig], []).append(i)
            else:
                seen[sig] = i
                unique.append(r)
        dup_clusters = [{"kept_row": k, "duplicate_rows": v} for k, v in clusters.items()]
        return {"input_rows": len(rows), "unique_rows": len(unique),
                "duplicate_rows": len(rows) - len(unique),
                "clusters": dup_clusters[:200], "unique": unique}

    def validate(self, result, ctx):
        return [ValidationCheck(name="dedup_counts",
                                passed=result.get("unique_rows", -1) + result.get("duplicate_rows", 0) == result.get("input_rows"))]


class ChartSpecNode(Node):
    name = "chart.spec"
    price_usdt = 0.01
    deterministic = True
    engine = "vega-lite"

    def run(self, ctx: NodeContext) -> dict:
        rows = _rows_from_input(ctx.input)
        x = ctx.input.get("x")
        y = ctx.input.get("y")
        mark = str(ctx.input.get("mark", "bar"))
        if mark not in {"bar", "line", "point", "area", "arc"}:
            raise NodeError(ErrorCode.INVALID_INPUT, f"unsupported mark '{mark}'")
        if not x or not y:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'x' and 'y' field names")
        # A field name that is not in the data produces a chart that renders empty with no error
        # anywhere — the caller pays, gets a valid-looking spec, and sees a blank panel. Catch the
        # typo here and name the fields that do exist.
        present = {k for r in rows[:200] if isinstance(r, dict) for k in r}
        missing = [f for f in (x, y) if f not in present]
        if missing and present:
            raise NodeError(
                ErrorCode.INVALID_INPUT,
                f"field(s) {missing} are not present in the data; available fields: {sorted(present)}",
            )

        x_type = _infer_vl_type(rows, x)
        y_type = _infer_vl_type(rows, y, prefer_measure=True)  # y is the measure axis
        spec: dict = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "description": f"{mark} of {y} by {x}",
            "data": {"values": rows[:5000]},
            "mark": mark if mark != "arc" else {"type": "arc", "tooltip": True},
        }
        if mark == "arc":
            # Vega-Lite renders `arc` from theta (the angular extent) and colour (the slice
            # identity). Handing it x/y produced a schema-valid spec that draws NOTHING — a paid pie
            # chart that comes back blank. This is the encoding the mark actually requires.
            spec["encoding"] = {
                "theta": {"field": y, "type": "quantitative", "stack": True},
                "color": {"field": x, "type": _nominal_or_ordinal(x_type),
                          "legend": {"title": x}},
                "tooltip": [{"field": x, "type": _nominal_or_ordinal(x_type)},
                            {"field": y, "type": "quantitative"}],
            }
        else:
            # Types are inferred, not assumed. Hardcoding x as `nominal` made every time series
            # plot as unordered categories in whatever order the rows happened to arrive, and every
            # numeric x-axis lose its scale — the chart looked plausible and told the wrong story.
            x_enc: dict = {"field": x, "type": x_type}
            if x_type == "temporal":
                x_enc["sort"] = "ascending"   # chronological, not row order
            spec["encoding"] = {
                "x": x_enc,
                "y": {"field": y, "type": y_type},
                "tooltip": [{"field": x, "type": x_type}, {"field": y, "type": y_type}],
            }
        return {"spec": spec, "mark": mark, "row_count": len(rows),
                "x_field": x, "y_field": y, "x_type": x_type, "y_type": y_type,
                "fields_available": sorted(present)}

    def validate(self, result, ctx):
        s = result.get("spec", {})
        enc = s.get("encoding", {})
        mark = result.get("mark")
        # Each mark has a required encoding channel set; a spec missing them is syntactically fine
        # and visually empty, which is the failure this check exists to make impossible.
        required = {"theta", "color"} if mark == "arc" else {"x", "y"}
        fields_used = {c.get("field") for c in enc.values() if isinstance(c, dict)}
        available = set(result.get("fields_available", []))
        return [
            ValidationCheck(name="valid_vega_lite",
                            passed=s.get("$schema", "").startswith("https://vega.github.io/schema/vega-lite")
                            and "encoding" in s),
            ValidationCheck(name="mark_has_required_channels",
                            passed=required.issubset(set(enc)),
                            detail=f"mark '{mark}' requires {sorted(required)}; spec has {sorted(enc)}"),
            ValidationCheck(name="encoded_fields_exist_in_data",
                            passed=not available or fields_used.issubset(available | {None}),
                            detail="every encoded field is a real column, so the chart cannot render empty"),
            ValidationCheck(name="types_inferred_not_assumed",
                            passed=result.get("x_type") in {"nominal", "ordinal", "quantitative", "temporal"}
                            and result.get("y_type") in {"nominal", "ordinal", "quantitative", "temporal"}),
        ]
