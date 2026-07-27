"""A units converter must accept the names people write.

`unit.convert` supported only the symbols — `c`, `f`, `m`, `kg` — and rejected `celsius`,
`fahrenheit`, `meters`, `pounds`. The error said "unknown unit(s)" without listing what would have
worked, so a caller paid, was refused, and had no way to discover that a one-letter spelling of the
same unit was accepted. Found by buying the service, not by reading it.
"""
from __future__ import annotations

import pytest

from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

RUNTIME = Runtime(build_registry())


def convert(**inp):
    return RUNTIME.execute(ArtifactRequest(endpoint="unit.convert", input=inp)).model_dump()


@pytest.mark.parametrize(("spelled", "symbol"), [
    ("fahrenheit", "f"), ("celsius", "c"), ("kelvin", "k"),
    ("kilometers", "km"), ("meters", "m"), ("pounds", "lb"),
    ("hours", "h"), ("megabytes", "mb"),
])
def test_the_spelled_out_name_matches_the_symbol(spelled, symbol):
    """Two spellings of one unit must give one answer, not an answer and a refusal."""
    target = "c" if symbol in {"f", "k"} else ("m" if symbol in {"km"} else symbol)
    spelled_out = convert(value=7, **{"from": spelled, "to": target})
    by_symbol = convert(value=7, **{"from": symbol, "to": target})
    assert spelled_out["ok"] is True, f"{spelled} was rejected while {symbol} was accepted"
    assert spelled_out["result"]["result"] == pytest.approx(by_symbol["result"]["result"])


def test_a_known_conversion_is_actually_correct():
    """Aliases are worthless if the arithmetic behind them is wrong."""
    assert convert(value=72, **{"from": "fahrenheit", "to": "celsius"})["result"]["result"] \
        == pytest.approx(22.2222, abs=1e-3)
    assert convert(value=2, **{"from": "pounds", "to": "grams"})["result"]["result"] \
        == pytest.approx(907.18474, abs=1e-4)


def test_an_unknown_unit_is_told_what_would_have_worked():
    env = convert(value=1, **{"from": "bananas", "to": "m"})
    assert env["ok"] is False
    message = env["error"]["message"]
    assert "banana" in message
    assert "Supported:" in message, "a refusal must name the units that do work"
    assert "celsius" in message, "the message should say full names are accepted"
