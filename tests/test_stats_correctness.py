"""The numbers have to be right, not merely present.

Every service in the deterministic tier returns quickly and confidently, which is exactly what makes
a wrong figure dangerous — nothing about the response looks like a failure. These check the arithmetic
against values computed here independently, on inputs whose correct answer is known in advance.

The `sum` and `counting` assertions exist because both were gaps found by auditing real purchases:
`data.stats` omitted the one aggregate people most often want, and `text.stats` used a word-counting
rule that disagrees with naive whitespace splitting without saying so.
"""
from __future__ import annotations

import statistics

import pytest

from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

RUNTIME = Runtime(build_registry())

ROWS = [
    {"city": "Lisbon", "population": 545923},
    {"city": "Porto", "population": 231962},
    {"city": "Madrid", "population": 3223334},
    {"city": "Lisbon", "population": 545923},
]
POPS = [r["population"] for r in ROWS]


def run(endpoint, **inp):
    env = RUNTIME.execute(ArtifactRequest(endpoint=endpoint, input=inp)).model_dump()
    assert env["ok"] is True, env.get("error")
    return env["result"]


def test_data_stats_arithmetic_is_correct():
    s = run("data.stats", rows=ROWS)["stats"]["population"]
    assert s["count"] == 4
    assert s["sum"] == pytest.approx(sum(POPS))
    assert s["min"] == pytest.approx(min(POPS))
    assert s["max"] == pytest.approx(max(POPS))
    assert s["mean"] == pytest.approx(statistics.fmean(POPS))
    assert s["median"] == pytest.approx(statistics.median(POPS))
    # Population, not sample — the two differ and the buyer is told which they got.
    assert s["std"] == pytest.approx(statistics.pstdev(POPS), rel=1e-6)
    assert s["std_kind"].startswith("population")


def test_the_sum_matches_mean_times_count():
    """Internal consistency: a caller who derives the total the old way must get the same figure."""
    s = run("data.stats", rows=ROWS)["stats"]["population"]
    assert s["sum"] == pytest.approx(s["mean"] * s["count"], rel=1e-9)


def test_text_stats_counts_and_states_its_rule():
    text = "X Layer is a layer-2 network using zero-knowledge proofs."
    out = run("text.stats", text=text)
    assert out["chars"] == len(text)
    # Hyphenated compounds count as two, so this must exceed a naive whitespace split.
    assert out["words"] > len(text.split())
    assert "counting" in out, "a count whose rule is unstated cannot be reconciled by the buyer"
    assert "whitespace" in out["counting"]["words"]


def test_dedupe_removes_exactly_the_planted_duplicate():
    out = run("data.dedupe", rows=ROWS, keys=["city"])
    kept = out.get("kept") or out.get("rows") or out.get("unique")
    assert len(kept) == 3, "one row was a deliberate duplicate; exactly one must go"
