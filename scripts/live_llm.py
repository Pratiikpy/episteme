"""Live MiniMax M3 check — runs document.extract_json through the runtime.
Network-dependent; prints result or a graceful error. Never prints the API key.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings
from contract import ArtifactRequest
from nodes import build_registry
from runtime import Runtime

s = get_settings()
print("LLM configured:", s.llm_configured, "| base:", s.llm_base_url, "| model:", s.llm_model)

rt = Runtime(build_registry())
req = ArtifactRequest(endpoint="document.extract_json", input={
    "text": "Invoice #A-42 dated 2026-07-24, total $128.50, billed to Acme Corp for cloud services.",
    "schema": {"invoice_no": "string", "date": "string", "total": "number", "vendor": "string"},
})
env = rt.execute(req)
print("ok:", env.ok, "| level:", env.validation.level.value)
if env.ok:
    print("extracted:", json.dumps(env.result.get("extracted"), ensure_ascii=False)[:500])
    print("model:", env.result.get("model"))
    print("receipt_verified:", rt.verify_receipt(env))
else:
    print("error:", env.error.code.value, "-", env.error.message[:200])
