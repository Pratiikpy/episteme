"""Regression tests for defects found by auditing REAL paid outputs.

Every case here was a live, signed response that a paying buyer actually received. They are grouped by
the property that was violated, because the individual bugs were symptoms of the same three mistakes:

  * claiming work that was not done (`data.clean`),
  * measuring wrongly and signing the measurement (`text.stats`, `text.diff`),
  * attesting facts about the run that were not true (`_tool_versions`, `data.stats` ordering).

A signature over a wrong answer is worse than no signature, so these are correctness tests, not
formatting preferences.
"""
from __future__ import annotations

import pytest

from config import get_settings
from contract import ArtifactRequest
from nodes import build_registry
from nodes.util2_nodes import _split_sentences
from runtime import NodeContext, NodeError, _tool_versions


@pytest.fixture(scope="module")
def registry():
    return build_registry()


def _ctx(endpoint: str, inp: dict) -> NodeContext:
    return NodeContext(ArtifactRequest(endpoint=endpoint, input=inp), get_settings(), "job_test")


# --------------------------------------------------------------- data.clean: do what you claim
def test_lowercase_alias_is_actually_applied(registry):
    """`ops: ["lowercase"]` was silently ignored — the code tested for "lower" — while the response
    echoed the op back and the artifact was signed. The buyer got an attested claim that their data
    had been lowercased when it had not been touched."""
    node = registry.get("data.clean")
    ctx = _ctx("data.clean", {"rows": [{"n": "  Bob   Smith ", "e": " A@B.COM "}],
                              "ops": ["trim", "collapse_ws", "lowercase"]})
    out = node.run(ctx)
    assert out["rows"] == [{"n": "bob smith", "e": "a@b.com"}]
    assert "lower" in out["ops_applied"]
    assert all(c.passed for c in node.validate(out, ctx))


def test_unknown_op_fails_loudly_rather_than_silently(registry):
    node = registry.get("data.clean")
    with pytest.raises(NodeError) as ei:
        node.run(_ctx("data.clean", {"rows": [{"a": "X"}], "ops": ["make_it_nice"]}))
    assert "unsupported op" in str(ei.value).lower()


def test_validation_would_catch_a_no_op(registry):
    """The old check only asserted a `rows` key existed, so untouched output still passed and got
    signed. The checks must fail if the requested transform is not observable in the result."""
    node = registry.get("data.clean")
    ctx = _ctx("data.clean", {"rows": [{"a": "  MiXeD  "}], "ops": ["lowercase"]})
    untouched = {"ops_applied": ["lower"], "rows": [{"a": "  MiXeD  "}]}
    assert any(not c.passed for c in node.validate(untouched, ctx))


# --------------------------------------------------------------- text.stats: count correctly
@pytest.mark.parametrize("text,expected", [
    ("Contact jane.doe@acme.com for help. She replies fast. Thanks!", 3),  # email is not 3 sentences
    ("Pi is 3.14 and e is 2.71. That is all.", 2),                         # decimals are not boundaries
    ("Dr. Smith arrived. He was late.", 2),                                # abbreviation is not a boundary
    ("He ran fast. Then stopped.", 2),                                     # "fast." is not the "st." abbrev
    ("See https://a.b/c/d.html now. Done.", 2),                            # URL dots are not boundaries
    ("One sentence with no terminator", 1),
    # [A-Z] is ASCII-only: accented capitals and non-cased scripts were not recognised as
    # sentence starts, so non-English text was silently undercounted.
    ("Héllo wörld — 你好世界! Ça va? Sí.", 3),
    ("", 0),
])
def test_sentence_counting(text, expected):
    assert len(_split_sentences(text)) == expected


# --------------------------------------------------------------- text.diff: emit a valid patch
def test_unified_diff_is_well_formed(registry):
    """With `keepends=True` and `"".join`, a final line lacking a newline was fused onto the next diff
    line, producing a patch that no tool can apply."""
    node = registry.get("text.diff")
    out = node.run(_ctx("text.diff", {"a": "alpha\nbeta\ngamma", "b": "alpha\nbeta\ndelta"}))
    lines = out["unified_diff"].splitlines()
    assert "-gamma" in lines and "+delta" in lines
    for ln in lines:
        assert not (ln.startswith("-") and "+" in ln[1:]), f"removed and added fused: {ln!r}"
    assert (out["added_lines"], out["removed_lines"]) == (1, 1)


def test_diff_counts_duplicate_lines(registry):
    """`l not in a_lines` counted a repeated line once and ignored moved lines entirely."""
    node = registry.get("text.diff")
    out = node.run(_ctx("text.diff", {"a": "x", "b": "x\ny\ny\ny"}))
    assert out["added_lines"] == 3


# --------------------------------------------------------------- provenance: attest what really ran
def test_tool_versions_are_real_not_placeholder(registry):
    """`engine_version` defaults to "1.0" on the base class, so nodes that never overrode it attested
    a version of "1.0" — and for `pillow` that is a version which has never existed. The value sits
    inside the signed manifest, making it a signed false statement about how the result was made."""
    node = registry.get("image.transform")
    versions = _tool_versions(node)
    assert versions.get("pillow") not in (None, "1.0", "unknown")
    assert versions["python"].count(".") >= 2
    for name, ver in versions.items():
        assert ver != "1.0", f"{name} still reports the placeholder version"


def test_deterministic_node_has_stable_column_order(registry):
    """`list({...})` iterates a set, so column order could vary between runs — changing the serialized
    output and therefore the SIGNED digest of a node declared deterministic."""
    node = registry.get("data.stats")
    rows = [{"b": 1, "a": 2, "c": 3}, {"b": 4, "a": 5, "c": 6}]
    first = node.run(_ctx("data.stats", {"rows": rows}))["numeric_columns"]
    for _ in range(5):
        assert node.run(_ctx("data.stats", {"rows": rows}))["numeric_columns"] == first
    assert first == ["b", "a", "c"], "column order must follow first appearance in the input"
