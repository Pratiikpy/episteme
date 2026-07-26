"""Simulation & foresight MVP: sim.run.

A deterministic, seeded agent-based opinion model over synthetic personas. Given a
topic, population and rounds, it simulates stance evolution and (optionally) the
divergence caused by an injected intervention. Fully reproducible (seeded) → L3.

HONESTY GUARDRAILS (baked into every output): labeled simulation, synthetic personas
only, NOT a prediction and NOT advice, no real-person targeting. Premium/A2A tier.
"""
from __future__ import annotations

import hashlib

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

_DISCLAIMER = ("Simulated synthetic personas. Decision-support and creative what-if only — "
               "NOT a prediction, NOT financial/political/medical/legal advice.")
_PROHIBITED = "No real-person targeting, no astroturfing, no manipulation/disinformation use."


class SimRunNode(Node):
    name = "sim.run"
    price_usdt = 0.20
    deterministic = True     # seeded → reproducible (L3)
    asp_type = "A2A"         # premium/heavy tier
    engine = "episteme-abm"

    def engine_available(self) -> bool:
        try:
            import numpy  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        import numpy as np
        inp = ctx.input
        topic = str(inp.get("topic", "")).strip()
        if not topic:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'topic' (a scenario, not a real person)")
        population = int(inp.get("population", 200))
        rounds = int(inp.get("rounds", 20))
        seed = int(inp.get("seed", 42))
        intervention = float(inp.get("intervention_strength", 0.0))
        if not (1 <= population <= 5000):
            raise NodeError(ErrorCode.LIMIT_EXCEEDED, "population must be 1..5000")
        if not (1 <= rounds <= 200):
            raise NodeError(ErrorCode.LIMIT_EXCEEDED, "rounds must be 1..200")
        if not (-1.0 <= intervention <= 1.0):
            raise NodeError(ErrorCode.INVALID_INPUT, "intervention_strength must be -1..1")

        # `confidence_bound` is what makes this a bounded-confidence model: an agent is only moved by
        # peers whose stance is within this distance of its own. Without it the run was plain
        # random-peer averaging while the assumptions block called it "bounded-confidence" — a label
        # for a mechanism that was not in the code.
        bound = float(inp.get("confidence_bound", 0.4))
        if not (0.05 <= bound <= 2.0):
            raise NodeError(ErrorCode.INVALID_INPUT, "confidence_bound must be 0.05..2.0")

        # The scenario must actually change the run. `topic` used to be echoed back and never read, so
        # every topic at the same population/rounds/seed produced byte-identical numbers — a $0.20
        # "scenario foresight" that was a function of three integers. The topic now seeds the persona
        # initialisation, so different scenarios start from different opinion landscapes while the same
        # scenario stays exactly reproducible (which is what `deterministic = True` promises).
        topic_seed = int(hashlib.sha256(topic.encode("utf-8")).hexdigest()[:8], 16)

        def simulate(interv: float) -> tuple[list[float], "np.ndarray"]:
            rng = np.random.default_rng([seed, topic_seed])
            stance = rng.uniform(-1, 1, population)
            infl = rng.uniform(0.05, 0.4, population)  # influenceability
            timeline = [round(float(stance.mean()), 6)]
            for _ in range(rounds):
                peers = rng.integers(0, population, size=(population, 5))
                peer_stance = stance[peers]
                # Only peers INSIDE the confidence bound exert influence.
                within = np.abs(peer_stance - stance[:, None]) <= bound
                counts = within.sum(axis=1)
                # An agent that hears no one it finds credible simply does not move this round.
                peer_mean = np.where(counts > 0,
                                     (peer_stance * within).sum(axis=1) / np.maximum(counts, 1),
                                     stance)
                stance = stance + infl * (peer_mean - stance) + interv * infl
                stance = np.clip(stance, -1, 1)
                timeline.append(round(float(stance.mean()), 6))
            return timeline, stance

        base_timeline, base_final = simulate(0.0)
        result = {
            "topic": topic,
            "assumptions": {
                "model": "bounded-confidence opinion dynamics: each agent samples 5 peers per round "
                         "and is moved only by those whose stance is within `confidence_bound`",
                "confidence_bound": bound,
                "population": population, "rounds": rounds, "seed": seed,
                "persona_generation": "stance and influenceability drawn from a generator seeded by "
                                      "(seed, topic), so the scenario changes the starting landscape "
                                      "and the same scenario reproduces exactly",
                # Stated plainly so nobody mistakes this for a semantic model of their scenario.
                "topic_is_not_interpreted": "The scenario text seeds persona initialisation. It is NOT "
                                            "read, classified, or reasoned about — this is a stylized "
                                            "opinion-dynamics simulation, not a forecast of this "
                                            "specific event.",
            },
            "baseline_timeline": base_timeline,
            "final_distribution": _dist(base_final),
            "confidence": "low-to-medium (stylized synthetic model; not calibrated to real data)",
            "simulation": True,
            "synthetic_personas": True,
            "not_real_people": True,
            "disclaimer": _DISCLAIMER,
            "prohibited_uses": _PROHIBITED,
        }
        if intervention != 0.0:
            interv_timeline, interv_final = simulate(intervention)
            divergence = round(interv_timeline[-1] - base_timeline[-1], 6)
            # simple sensitivity: +/- 20% of intervention
            hi, _ = simulate(intervention * 1.2)
            lo, _ = simulate(intervention * 0.8)
            result["intervention"] = {
                "strength": intervention,
                "intervened_timeline": interv_timeline,
                "intervened_distribution": _dist(interv_final),
                "final_divergence_vs_baseline": divergence,
                "sensitivity": {"minus20pct": round(lo[-1] - base_timeline[-1], 6),
                                "plus20pct": round(hi[-1] - base_timeline[-1], 6)},
            }
        return result

    def validate(self, result, ctx):
        return [
            ValidationCheck(name="labeled_simulation", passed=result.get("simulation") is True),
            ValidationCheck(name="has_disclaimer", passed=bool(result.get("disclaimer"))),
            ValidationCheck(name="timeline_len",
                            passed=len(result.get("baseline_timeline", [])) == result["assumptions"]["rounds"] + 1),
        ]


def _dist(final) -> dict:
    import numpy as np
    a = np.asarray(final)
    n = len(a)
    return {
        "against": round(float((a < -0.33).sum()) / n, 4),
        "neutral": round(float(((a >= -0.33) & (a <= 0.33)).sum()) / n, 4),
        "for": round(float((a > 0.33).sum()) / n, 4),
        "mean_stance": round(float(a.mean()), 6),
    }
