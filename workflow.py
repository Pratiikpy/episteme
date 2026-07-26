"""workflow.compose — the verifiable artifact graph (chain nodes, pass data, per-node receipts).

A step input value of the form "$<stepIdOrIndex>.<dotted.path>" is resolved from a
prior step's result, e.g. "$0.markdown" or "$extract.extracted.total". Each step runs
through the full Verification Runtime, so every node in the graph carries its own
signed receipt and verification level.
"""
from __future__ import annotations

import re

from contract import ArtifactRequest, ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError

_REF = re.compile(r"^\$([\w.-]+?)\.(.+)$")


class WorkflowComposeNode(Node):
    name = "workflow.compose"
    price_usdt = 0.05
    deterministic = False  # graph may include non-deterministic (LLM/web) steps
    asp_type = "A2A"       # composite/bespoke → best fit A2A (also callable A2MCP)
    engine = "episteme-workflow"

    def run(self, ctx: NodeContext) -> dict:
        steps = ctx.input.get("steps")
        if not isinstance(steps, list) or not steps:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'steps' (non-empty list)")
        if len(steps) > 25:
            raise NodeError(ErrorCode.LIMIT_EXCEEDED, "max 25 steps")

        # build a sub-runtime (fresh registry); guard against recursion
        from nodes import build_registry
        from runtime import Runtime
        sub = Runtime(build_registry(), ctx.settings)

        results_by_key: dict[str, dict] = {}
        summaries, edges = [], []
        all_ok = True

        for idx, step in enumerate(steps):
            endpoint = step.get("endpoint")
            if endpoint == "workflow.compose":
                raise NodeError(ErrorCode.POLICY_BLOCKED, "nested workflow.compose not allowed")
            if not endpoint:
                raise NodeError(ErrorCode.INVALID_INPUT, f"step {idx} missing 'endpoint'")
            sid = str(step.get("id", idx))
            raw_input = step.get("input", {})
            resolved, deps = self._resolve(raw_input, results_by_key)
            for d in deps:
                edges.append({"from": d, "to": sid})

            env = sub.execute(ArtifactRequest(endpoint=endpoint, input=resolved,
                                              options=step.get("options", {})))
            results_by_key[sid] = env.result if env.ok else {}
            results_by_key[str(idx)] = results_by_key[sid]
            ok = env.ok
            all_ok = all_ok and ok
            summaries.append({
                "id": sid, "endpoint": endpoint, "ok": ok,
                "level": env.validation.level.value,
                "output_hash": env.output_hash,
                "receipt_signed": bool(env.receipt and env.receipt.signature),
                "error": env.error.message if env.error else None,
            })
            if not ok and not step.get("continue_on_error"):
                break

        final = summaries[-1]
        return {
            "step_count": len(summaries),
            "all_ok": all_ok,
            "steps": summaries,
            "edges": edges,
            "final_result": results_by_key.get(final["id"], {}),
        }

    def _resolve(self, value, results):
        """Recursively resolve $step.path references. Returns (resolved, deps)."""
        deps = []
        if isinstance(value, str):
            m = _REF.match(value)
            if m:
                key, path = m.group(1), m.group(2)
                deps.append(key)
                cur = results.get(key, {})
                for part in path.split("."):
                    if isinstance(cur, dict):
                        cur = cur.get(part)
                    elif isinstance(cur, list) and part.isdigit():
                        cur = cur[int(part)] if int(part) < len(cur) else None
                    else:
                        cur = None
                return cur, deps
            return value, deps
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                rv, d = self._resolve(v, results)
                out[k] = rv
                deps += d
            return out, deps
        if isinstance(value, list):
            out = []
            for v in value:
                rv, d = self._resolve(v, results)
                out.append(rv)
                deps += d
            return out, deps
        return value, deps

    def validate(self, result, ctx):
        steps = result.get("steps", [])
        failed = [s["endpoint"] for s in steps if not s.get("ok")]
        return [
            ValidationCheck(name="ran_steps", passed=result.get("step_count", 0) >= 1),
            # A FAILED step is still signed — the receipt attests "this is what happened", and what
            # happened was an error. So `all_steps_signed` passed even when every step had failed, and
            # the buyer received HTTP 200, validation.status="validated", L2_VALIDATED and a signature
            # over a graph that produced nothing. A signed receipt asserting success over failed work
            # is worse than no receipt: it lends the failure authority.
            ValidationCheck(name="all_steps_signed",
                            passed=all(s["receipt_signed"] for s in steps),
                            detail="every step returned a signed receipt, successful or not"),
            ValidationCheck(name="all_steps_succeeded", passed=not failed,
                            detail=f"failed step(s): {failed}" if failed else "every step returned ok"),
        ]
