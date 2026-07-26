"""Final batch: data.pivot (group-by aggregate), robots.check (robots.txt policy)."""
from __future__ import annotations

from urllib import robotparser
from urllib.parse import urlparse

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError


class DataPivotNode(Node):
    name = "data.pivot"
    price_usdt = 0.01
    deterministic = True
    engine = "episteme-pivot"

    def run(self, ctx: NodeContext) -> dict:
        rows = ctx.input.get("rows")
        if not isinstance(rows, list):
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'rows' (list of objects)")
        group_by = ctx.input.get("group_by")
        if isinstance(group_by, str):
            group_by = [group_by]
        if not group_by:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'group_by' field(s)")
        valid_funcs = {"sum", "count", "avg", "min", "max"}
        agg = ctx.input.get("agg") or {"__count__": "count"}
        # Accept the shorthand a caller naturally reaches for — `agg: "sum", value: "amount"` — as
        # well as the full `{field: func}` mapping. Previously anything but a dict reached
        # `agg.items()` and escaped as a raw AttributeError under ENGINE_FAILED: an internal Python
        # error surfaced to a paying caller with nothing to act on, when the real answer is a plain
        # statement of the expected shape.
        if isinstance(agg, str):
            field = ctx.input.get("value") or ctx.input.get("values") or ctx.input.get("field")
            if agg == "count":
                agg = {str(field): "count"} if field else {"__count__": "count"}
            elif not field:
                raise NodeError(
                    ErrorCode.INVALID_INPUT,
                    f"agg '{agg}' needs a column to aggregate: pass 'value' (e.g. "
                    f"{{\"agg\": \"{agg}\", \"value\": \"amount\"}}) or use the mapping form "
                    f"{{\"agg\": {{\"amount\": \"{agg}\"}}}}",
                )
            else:
                agg = {str(field): agg}
        if not isinstance(agg, dict):
            raise NodeError(
                ErrorCode.INVALID_INPUT,
                "'agg' must be either a function name with 'value' (e.g. agg=\"sum\", "
                'value="amount") or a mapping of column to function (e.g. {"amount": "sum"}); '
                f"got {type(agg).__name__}",
            )
        bad_rows = [i for i, r in enumerate(rows) if not isinstance(r, dict)]
        if bad_rows:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            f"'rows' must contain only objects; element {bad_rows[0]} is "
                            f"{type(rows[bad_rows[0]]).__name__}")
        missing = [g for g in group_by if not any(g in r for r in rows)]
        if missing:
            present = sorted({k for r in rows for k in r})
            raise NodeError(ErrorCode.INVALID_INPUT,
                            f"group_by column(s) {missing} are not present in any row; "
                            f"available columns: {present}")
        buckets: dict = {}
        for r in rows:
            key = tuple(r.get(g) for g in group_by)
            buckets.setdefault(key, []).append(r)
        out = []
        for key, group in buckets.items():
            row = {g: k for g, k in zip(group_by, key)}
            for field, func in agg.items():
                if func not in valid_funcs:
                    raise NodeError(ErrorCode.INVALID_INPUT, f"bad agg func '{func}'")
                if func == "count":
                    row[f"{field}_count" if field != "__count__" else "count"] = len(group)
                    continue
                vals = []
                for r in group:
                    try:
                        vals.append(float(r.get(field)))
                    except (TypeError, ValueError):
                        pass
                if not vals:
                    row[f"{field}_{func}"] = None
                elif func == "sum":
                    row[f"{field}_{func}"] = round(sum(vals), 6)
                elif func == "avg":
                    row[f"{field}_{func}"] = round(sum(vals) / len(vals), 6)
                elif func == "min":
                    row[f"{field}_{func}"] = min(vals)
                elif func == "max":
                    row[f"{field}_{func}"] = max(vals)
            out.append(row)
        return {"group_by": group_by, "group_count": len(out), "rows": out[:5000]}

    def validate(self, result, ctx):
        return [ValidationCheck(name="pivoted", passed="group_count" in result)]


class RobotsCheckNode(Node):
    name = "robots.check"
    price_usdt = 0.005
    deterministic = True
    engine = "episteme-robots"

    def run(self, ctx: NodeContext) -> dict:
        robots = ctx.input.get("robots")
        if not isinstance(robots, str) or not robots.strip():
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'robots' (robots.txt content)")
        # Accept either a full URL or a bare path. A caller checking crawl permission holds a URL,
        # not a path — and `url` was silently ignored, so the node fell back to "/" and answered
        # `allowed: true` for a URL that robots.txt explicitly disallowed. For a compliance tool that
        # is the dangerous direction to be wrong in: it tells you to go ahead.
        url = ctx.input.get("url")
        path_in = ctx.input.get("path")
        if url:
            parsed = urlparse(str(url))
            if not parsed.scheme and not str(url).startswith("/"):
                raise NodeError(ErrorCode.INVALID_INPUT,
                                f"'url' is not a URL or absolute path: {str(url)[:80]!r}")
            path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        elif path_in is not None:
            path = str(path_in)
        else:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "provide 'url' (full URL) or 'path' — refusing to guess, since the "
                            "default '/' is almost always allowed and would report a false 'allowed'")
        ua = str(ctx.input.get("user_agent", "*"))
        rp = robotparser.RobotFileParser()
        rp.parse(robots.splitlines())
        allowed = rp.can_fetch(ua, path)
        sitemaps = [l.split(":", 1)[1].strip() for l in robots.splitlines()
                    if l.lower().startswith("sitemap:")]
        crawl_delay = None
        try:
            crawl_delay = rp.crawl_delay(ua)
        except Exception:
            pass
        return {"user_agent": ua, "path": path, "allowed": bool(allowed),
                "sitemaps": sitemaps, "crawl_delay": crawl_delay}

    def validate(self, result, ctx):
        path = result.get("path", "")
        return [
            ValidationCheck(name="decided", passed="allowed" in result),
            # The verdict must be about the path the caller asked about. Answering for "/" when a
            # deeper path was supplied is how a disallowed URL came back allowed.
            ValidationCheck(
                name="verdict_is_about_the_requested_path",
                passed=bool(path) and (
                    path == str(ctx.input.get("path", path))
                    or path.startswith(urlparse(str(ctx.input.get("url", ""))).path or "\0")
                ),
                detail=f"decided for {path!r}",
            ),
        ]
