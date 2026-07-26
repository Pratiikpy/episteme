"""Exercise the Episteme ASP across representative endpoints and verify behavior.
ASCII-only, flushed, per-call error-guarded so every outcome prints.
"""
import sys, os, json, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

rt = Runtime(build_registry())


def run(ep, **inp):
    try:
        env = rt.execute(ArtifactRequest(endpoint=ep, input=inp))
    except Exception as e:
        print(f"{ep:24} EXC {type(e).__name__}: {e}", flush=True)
        return None
    ok, lvl, rec = env.ok, env.validation.level.value, rt.verify_receipt(env)
    if ok:
        r = env.result
        if ep == "file.inspect": s = "mime=%s sha=%s" % (r["mime_type"], r["sha256"][:12])
        elif ep == "document.to_markdown": s = "pages=%s chars=%s" % (r["pages"], r["char_count"])
        elif ep == "csv.profile": s = "rows=%s cols=%s alerts=%s" % (r["rows"], r["cols"], len(r["alerts"]))
        elif ep == "data.query_sql": s = "rows=%s %s" % (r["row_count"], r["rows"])
        elif ep == "document.redact_pii": s = "redactions=%s out=%r" % (r["total_redactions"], r["redacted_text"])
        elif ep == "sim.run": s = "final_dist=%s" % (r["final_distribution"],)
        elif ep == "workflow.compose": s = "steps=%s all_ok=%s final_matches=%s" % (r["step_count"], r["all_ok"], r.get("final_result", {}).get("matches"))
        elif ep == "receipt.verify": s = "verdict=%s" % r["verdict"]
        elif ep == "document.extract_json": s = "extracted=%s" % json.dumps(r.get("extracted"))[:120]
        else: s = json.dumps(r)[:90]
    else:
        s = "ERROR %s: %s" % (env.error.code.value, env.error.message[:60])
    print("%-24s ok=%-5s level=%-20s receipt=%s  %s" % (ep, ok, lvl, rec, s), flush=True)
    return env


print("=== EPISTEME ASP BEHAVIOR CHECK ===", flush=True)
run("file.inspect", content_b64=base64.b64encode(b"%PDF-1.4 test").decode())
run("document.to_markdown", text="# Launch Plan\n\nShip Episteme.")
run("csv.profile", csv="name,age\nalice,30\nbob,\ncarol,25\n")
run("data.query_sql", csv="name,age\nalice,30\nbob,40\n", sql="SELECT name FROM t WHERE age>35")
run("document.redact_pii", text="Email jane@acme.com, call 415-555-1234")
run("sim.run", topic="new pricing", population=60, rounds=8, seed=5, intervention_strength=0.3)
run("workflow.compose", steps=[
    {"id": "i", "endpoint": "file.inspect", "input": {"text": "hello"}},
    {"id": "v", "endpoint": "artifact.verify", "input": {"text": "hello", "expected_sha256": "$i.sha256"}},
])
src = rt.execute(ArtifactRequest(endpoint="hash.compute", input={"text": "prove me"}))
run("receipt.verify", envelope=src.model_dump(mode="json"))
run("document.extract_json",
    text="Invoice #B-9 dated 2026-07-25, total $50.00 for Globex.",
    schema={"invoice_no": "string", "date": "string", "total": "number", "vendor": "string"})
print("=== DONE ===", flush=True)
