"""Data pack nodes: csv.profile, data.query_sql, data.diff (polars engine)."""
from __future__ import annotations

import csv as _csv
import io

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError


def _csv_bytes(inp: dict) -> bytes:
    import base64
    if "content_b64" in inp:
        return base64.b64decode(inp["content_b64"])
    if "csv" in inp:
        return str(inp["csv"]).encode("utf-8")
    if "text" in inp:
        return str(inp["text"]).encode("utf-8")
    raise NodeError(ErrorCode.INVALID_INPUT, "provide 'csv', 'text' or 'content_b64'")


class CsvProfileNode(Node):
    name = "csv.profile"
    price_usdt = 0.01
    deterministic = True
    engine = "polars"

    def engine_available(self) -> bool:
        try:
            import polars  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import polars as pl
        data = _csv_bytes(ctx.input)
        try:
            df = pl.read_csv(io.BytesIO(data), infer_schema_length=1000)
        except Exception as e:
            raise NodeError(ErrorCode.UNSUPPORTED_FORMAT, f"csv parse failed: {e}")
        rows, cols = df.height, df.width
        nulls = {c: int(df[c].null_count()) for c in df.columns}
        dtypes = {c: str(df.schema[c]) for c in df.columns}
        alerts = []
        for c in df.columns:
            if rows and nulls[c] == rows:
                alerts.append(f"column '{c}' is entirely null")
            elif rows and nulls[c] / rows > 0.5:
                alerts.append(f"column '{c}' >50% null")
            try:
                if df[c].n_unique() == 1 and rows > 1:
                    alerts.append(f"column '{c}' is constant")
            except Exception:
                pass
        numeric = {}
        for c in df.columns:
            if df.schema[c].is_numeric():
                s = df[c]
                numeric[c] = {
                    "min": _num(s.min()), "max": _num(s.max()),
                    "mean": _num(s.mean()), "null": nulls[c],
                }
        return {
            "rows": rows, "cols": cols, "columns": df.columns,
            "dtypes": dtypes, "null_counts": nulls,
            "numeric_summary": numeric, "alerts": alerts,
            "verify_key": {"rows": rows, "cols": cols},
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="rows_nonnegative", passed=result.get("rows", -1) >= 0),
            ValidationCheck(name="cols_match_columns",
                            passed=result.get("cols") == len(result.get("columns", []))),
        ]


class CsvProfileVerifier(Node):
    """Independent engine: count rows/cols via Python stdlib csv (not polars)."""

    name = "csv.profile.alt"
    engine = "python-stdlib-csv"
    deterministic = True

    def run(self, ctx: NodeContext) -> dict:
        data = _csv_bytes(ctx.input).decode("utf-8", errors="replace")
        reader = list(_csv.reader(io.StringIO(data)))
        if not reader:
            return {"verify_key": {"rows": 0, "cols": 0}}
        header, *body = reader
        return {"verify_key": {"rows": len(body), "cols": len(header)}}


class DataQuerySqlNode(Node):
    name = "data.query_sql"
    price_usdt = 0.01
    deterministic = True
    engine = "polars-sql"

    def engine_available(self) -> bool:
        try:
            import polars  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import polars as pl
        data = _csv_bytes(ctx.input)
        sql = str(ctx.input.get("sql", "")).strip()
        if not sql:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'sql'")
        low = sql.lower()
        if any(k in low for k in ("insert ", "update ", "delete ", "drop ", "alter ", "create ")):
            raise NodeError(ErrorCode.POLICY_BLOCKED, "only read-only SELECT queries allowed")
        try:
            df = pl.read_csv(io.BytesIO(data), infer_schema_length=1000)
        except Exception as e:
            raise NodeError(ErrorCode.UNSUPPORTED_FORMAT, f"csv parse failed: {e}")
        # The dataset was registered under the single hard-coded relation name `t`, and nothing in the
        # listing, the description or the published schema said so — so any query not written as
        # `FROM t` failed, and no working call could be constructed from what a buyer can see. Rather
        # than document one magic word, register the CSV under every name a caller would plausibly
        # reach for, and let them name their own.
        table = str(ctx.input.get("table") or "").strip() or None
        aliases = {"t", "data", "csv", "rows", "tbl", "dataset", "input"}
        if table:
            if not table.replace("_", "").isalnum():
                raise NodeError(ErrorCode.INVALID_INPUT,
                                f"'table' must be alphanumeric/underscore, got {table!r}")
            aliases.add(table)
        try:
            ctxs = pl.SQLContext(eager=True, **{name: df for name in sorted(aliases)})
            out = ctxs.execute(sql)
        except Exception as e:
            raise NodeError(
                ErrorCode.ENGINE_FAILED,
                f"query failed: {e}. The dataset is available as any of: {', '.join(sorted(aliases))} "
                f"— e.g. SELECT * FROM t LIMIT 5. Columns: {', '.join(df.columns)}",
            )
        limit = int(ctx.options.get("limit", 1000))
        out = out.head(limit)
        return {
            "sql": sql, "row_count": out.height,
            "columns": out.columns, "rows": out.to_dicts(),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [ValidationCheck(name="executed", passed="row_count" in result)]


class DataDiffNode(Node):
    name = "data.diff"
    price_usdt = 0.01
    deterministic = True
    engine = "polars"

    def engine_available(self) -> bool:
        try:
            import polars  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import polars as pl
        a = ctx.input.get("a")
        b = ctx.input.get("b")
        if a is None or b is None:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "provide 'a' and 'b' — each either a CSV string or a list of row objects")

        def _rows(v: object, which: str) -> list[dict]:
            """Accept rows or CSV. Never stringify a structure into the CSV parser.

            `str(list_of_dicts)` produces "[{'order_id': 1001, ...}]", which read_csv parses
            *successfully* as a zero-row table with nonsense column names. The node then reported
            added_rows=0 / removed_rows=0 — "no differences" — for two datasets that plainly differ,
            on a paid call, with ok=True and no warning anywhere. Every sibling node
            (data.dedupe, data.stats, data.pivot) takes `rows`, so passing rows here is the obvious
            thing to do and it silently produced a wrong answer.
            """
            if isinstance(v, list):
                if v and not all(isinstance(r, dict) for r in v):
                    raise NodeError(ErrorCode.INVALID_INPUT,
                                    f"'{which}' is a list but not every element is an object")
                return v
            if isinstance(v, str):
                if not v.strip():
                    return []
                try:
                    return pl.read_csv(io.BytesIO(v.encode())).to_dicts()
                except Exception as e:  # noqa: BLE001
                    raise NodeError(ErrorCode.INVALID_INPUT, f"'{which}' is not parseable CSV: {e}")
            raise NodeError(
                ErrorCode.INVALID_INPUT,
                f"'{which}' must be a CSV string or a list of row objects, got {type(v).__name__}",
            )

        rows_a, rows_b = _rows(a, "a"), _rows(b, "b")

        # Column order comes from the rows themselves so both input forms behave identically. Taking
        # it from a polars frame would only work for the CSV form; dict insertion order preserves the
        # column order of a CSV read and of a caller-supplied row list alike.
        def _cols(rows: list[dict]) -> list[str]:
            seen: dict[str, None] = {}
            for r in rows:
                for k in r:
                    seen.setdefault(k, None)
            return list(seen)

        cols_of_a, cols_of_b = _cols(rows_a), _cols(rows_b)

        # Counter, not set. A set collapses identical rows, so deleting one of two duplicates used
        # to report "no change" — a false negative on a paid diff, the worst answer a diff can give.
        from collections import Counter
        ca = Counter(tuple(r.values()) for r in rows_a)
        cb = Counter(tuple(r.values()) for r in rows_b)
        removed_multi = ca - cb
        added_multi = cb - ca

        shared = [c for c in cols_of_a if c in cols_of_b]
        key = ctx.input.get("key")
        if key is not None and key not in shared:
            raise NodeError(ErrorCode.INVALID_INPUT, f"key '{key}' is not a column present in both inputs")
        if key is None:
            # A column that uniquely identifies a row on BOTH sides lets us say "this row changed"
            # instead of "one row vanished and an unrelated one appeared".
            for c in shared:
                va = [r[c] for r in rows_a]
                vb = [r[c] for r in rows_b]
                if len(set(va)) == len(va) and len(set(vb)) == len(vb):
                    key = c
                    break

        added_rows: list[dict] = []
        removed_rows: list[dict] = []
        modified_rows: list[dict] = []

        def expand(counter):
            out = []
            for tup, n in counter.items():
                out.extend([tup] * n)
            return out

        if key:
            ka = {r[key]: r for r in rows_a}
            kb = {r[key]: r for r in rows_b}
            for k in kb.keys() - ka.keys():
                added_rows.append(kb[k])
            for k in ka.keys() - kb.keys():
                removed_rows.append(ka[k])
            for k in ka.keys() & kb.keys():
                ra, rb = ka[k], kb[k]
                changes = {c: {"from": ra.get(c), "to": rb.get(c)}
                           for c in shared if ra.get(c) != rb.get(c)}
                if changes:
                    modified_rows.append({"key": k, "changes": changes})
        else:
            cols_a, cols_b = cols_of_a, cols_of_b
            removed_rows = [dict(zip(cols_a, t)) for t in expand(removed_multi)]
            added_rows = [dict(zip(cols_b, t)) for t in expand(added_multi)]

        cols_added = [c for c in cols_of_b if c not in cols_of_a]
        cols_removed = [c for c in cols_of_a if c not in cols_of_b]
        order_changed = (not cols_added and not cols_removed and cols_of_a != cols_of_b)
        schema_changed = bool(cols_added or cols_removed or order_changed)

        cap = 200  # bound the payload; the counts above stay exact
        return {
            "rows_a": len(rows_a), "rows_b": len(rows_b),
            "key_column": key,
            "added_rows": len(added_rows),
            "removed_rows": len(removed_rows),
            "modified_rows": len(modified_rows),
            "added": added_rows[:cap],
            "removed": removed_rows[:cap],
            "modified": modified_rows[:cap],
            "truncated": max(len(added_rows), len(removed_rows), len(modified_rows)) > cap,
            "columns_added": cols_added,
            "columns_removed": cols_removed,
            "column_order_changed": order_changed,
            "schema_changed": schema_changed,
            "changed": bool(added_rows or removed_rows or modified_rows or schema_changed
                            or len(rows_a) != len(rows_b)),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        checks = [ValidationCheck(name="counts_present", passed="added_rows" in result)]
        # Row arithmetic must close. This invariant is what would have caught the set-based
        # false negative: with sets, a removed duplicate left the equation unbalanced.
        try:
            delta = result["rows_a"] - result["rows_b"]
            expected = result["removed_rows"] - result["added_rows"]
            ok = delta == expected
            checks.append(ValidationCheck(
                name="row_counts_reconcile", passed=ok,
                detail=None if ok else f"rows_a-rows_b={delta} but removed-added={expected}",
            ))
        except Exception:
            checks.append(ValidationCheck(name="row_counts_reconcile", passed=False,
                                          detail="could not reconcile row counts"))
        # "No change" is a claim, so make it falsifiable rather than a default.
        if result.get("changed") is False:
            identical = (result.get("rows_a") == result.get("rows_b")
                         and not result.get("schema_changed"))
            checks.append(ValidationCheck(
                name="no_change_is_consistent", passed=bool(identical),
                detail=None if identical else "reported unchanged while shape differs",
            ))
        return checks


def _num(v):
    if v is None:
        return None
    try:
        return round(float(v), 6)
    except Exception:
        return None
