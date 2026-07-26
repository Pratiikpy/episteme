"""Regressions for defects found by paying for our own services and checking the output.

Each test here corresponds to a real paid call that returned wrong data while reporting success —
the worst failure mode for a paid ASP, because the buyer has no way to know."""
import csv as _csv
import io

import pytest

from config import get_settings
from contract import ArtifactRequest
from nodes import build_registry
from runtime import NodeContext


@pytest.fixture(scope="module")
def reg():
    return build_registry()


def _ctx(endpoint, inp):
    return NodeContext(ArtifactRequest(endpoint=endpoint, input=inp, options={}), get_settings(), "job_test")


# --- csv.to_table: tabulate silently reformatted numeric cells -------------------------------

MONEY_CSV = "vendor,amount\nACME,1284500.50\nGLOBEX,0.000015\n"


def test_csv_to_table_preserves_exact_numeric_cells(reg):
    """1284500.50 came back as 1.2845e+06 — a finance table delivered wrong, and signed."""
    node = reg.get("csv.to_table")
    ctx = _ctx("csv.to_table", {"csv": MONEY_CSV})
    table = node.run(ctx)["table"]
    assert "1284500.50" in table, f"amount was reformatted: {table}"
    assert "0.000015" in table, f"small amount was reformatted: {table}"
    assert "1.2845e+06" not in table


def test_csv_to_table_flags_any_altered_cell(reg):
    """The quality check must FAIL when a cell is rewritten — 'it produced a string' is not proof."""
    from tabulate import tabulate
    node = reg.get("csv.to_table")
    rows = list(_csv.reader(io.StringIO(MONEY_CSV)))
    header, *body = rows
    corrupted = tabulate(body, headers=header, tablefmt="github")  # numparse on = the old bug
    checks = node.validate(
        {"table": corrupted, "numparse": False, "rows": len(body), "cols": len(header), "format": "github"},
        _ctx("csv.to_table", {"csv": MONEY_CSV}),
    )
    preserved = [c for c in checks if c.name == "cells_preserved"]
    assert preserved, "cells_preserved check is missing"
    assert preserved[0].passed is False, "corruption was not detected"


def test_csv_to_table_numparse_is_opt_in(reg):
    """A caller may still ask for parsing — but only explicitly."""
    node = reg.get("csv.to_table")
    table = node.run(_ctx("csv.to_table", {"csv": MONEY_CSV, "numparse": True}))["table"]
    assert "1.2845e+06" in table


# --- document.extract_json: a parse failure was signed as a valid extraction -----------------


def test_unparseable_llm_output_is_not_a_valid_result():
    """The old sentinel {"_raw":..., "_parse_error":True} is a dict, so every isinstance(x, dict)
    check passed and the failure was stamped 'validated' and ed25519-signed."""
    from nodes.llm_nodes import _coerce_json
    value, ok = _coerce_json("this is not json at all")
    assert ok is False
    assert value is None, "must not return a look-alike sentinel"


@pytest.mark.parametrize("text,expected", [
    ('{"a":1}', {"a": 1}),
    ('```json\n{"a":1}\n```', {"a": 1}),
    ('<think>reasoning</think>\n{"a":1}', {"a": 1}),
    # the greedy <think> strip used to delete the JSON along with the unclosed tag
    ('<think>never closed, json follows {"a":1}', {"a": 1}),
    ('prose {"a":{"b":[1,2]}} prose', {"a": {"b": [1, 2]}}),
    # a brace inside a string literal must not end the object early
    ('{"s":"has } brace","a":1}', {"s": "has } brace", "a": 1}),
])
def test_json_recovery_cases(text, expected):
    from nodes.llm_nodes import _coerce_json
    value, ok = _coerce_json(text)
    assert ok is True and value == expected


def test_extract_json_validate_rejects_the_old_sentinel(reg):
    """Belt and braces: even if a sentinel somehow appeared, validation must fail it."""
    node = reg.get("document.extract_json")
    checks = node.validate(
        {"extracted": {"_raw": "junk", "_parse_error": True}, "schema_valid": True},
        _ctx("document.extract_json", {"text": "x"}),
    )
    flag = [c for c in checks if c.name == "no_parse_error"]
    assert flag and flag[0].passed is False


def test_extract_json_enforces_declared_required_fields(reg):
    node = reg.get("document.extract_json")
    checks = node.validate(
        {"extracted": {"name": "ACME"}, "schema_valid": True, "requested_type": "object"},
        _ctx("document.extract_json", {"text": "x", "schema": {"type": "object", "required": ["name", "total"]}}),
    )
    req = [c for c in checks if c.name == "required_fields_present"]
    assert req and req[0].passed is False and "total" in (req[0].detail or "")
