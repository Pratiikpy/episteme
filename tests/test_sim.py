"""Tests for the Simulation & foresight MVP (sim.run): determinism + guardrails."""
from contract import ArtifactRequest, VerificationLevel


def _req(**inp):
    return ArtifactRequest(endpoint="sim.run", input=inp)


def test_sim_run_deterministic_and_guarded(runtime):
    env = runtime.execute(_req(topic="adoption of a new tool", population=100, rounds=10, seed=7))
    assert env.ok
    r = env.result
    # guardrails present
    assert r["simulation"] is True and r["synthetic_personas"] is True
    assert "NOT a prediction" in r["disclaimer"]
    assert r["prohibited_uses"]
    # timeline length = rounds + 1
    assert len(r["baseline_timeline"]) == 11
    # distribution sums ~1
    d = r["final_distribution"]
    assert abs(d["against"] + d["neutral"] + d["for"] - 1.0) < 1e-6
    # seeded → reproduced (L3)
    assert env.validation.level == VerificationLevel.L3_REPRODUCED


def test_sim_run_intervention_divergence(runtime):
    env = runtime.execute(_req(topic="campaign", population=100, rounds=15, seed=3,
                               intervention_strength=0.5))
    assert env.ok
    iv = env.result["intervention"]
    assert iv["strength"] == 0.5
    # positive intervention should push mean stance up vs baseline
    assert iv["final_divergence_vs_baseline"] > 0
    assert "sensitivity" in iv


def test_sim_run_rejects_bad_input(runtime):
    assert runtime.execute(_req(topic="", population=10, rounds=5)).ok is False
    assert runtime.execute(_req(topic="x", population=100000, rounds=5)).ok is False
