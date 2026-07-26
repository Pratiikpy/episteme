#!/bin/sh
# Episteme container entrypoint: Python runtime (private) + Node x402 gateway (public).
#
#   public :$PORT  ->  Node OKX-SDK x402 gateway  ->  127.0.0.1:8890 Python runtime
#
# If EITHER process dies the container exits, so the platform restarts a clean pair
# (a half-dead pair is what produces "paid but 502" and reviewer timeouts).
set -e

BACKEND_PORT=8890
export EPISTEME_BACKEND="http://127.0.0.1:${BACKEND_PORT}"
export GATEWAY_PORT="${PORT:-8080}"
export GATEWAY_HOST="0.0.0.0"   # container: platform routes to the pod IP, not loopback

echo "[episteme] starting python runtime on 127.0.0.1:${BACKEND_PORT}"
uvicorn gateway:app --host 127.0.0.1 --port "${BACKEND_PORT}" --workers 2 --timeout-keep-alive 65 &
PY_PID=$!

# wait for the runtime to answer before exposing the paywall
i=0
while [ $i -lt 40 ]; do
  if python -c "import httpx;httpx.get('http://127.0.0.1:${BACKEND_PORT}/healthz',timeout=2).raise_for_status()" 2>/dev/null; then
    echo "[episteme] python runtime healthy"
    break
  fi
  i=$((i+1)); sleep 1
done

echo "[episteme] starting x402 gateway on 0.0.0.0:${GATEWAY_PORT}"
cd /app/gateway && npx tsx server.ts &
GW_PID=$!

# exit as soon as either child exits
wait -n "$PY_PID" "$GW_PID" 2>/dev/null || wait "$PY_PID" "$GW_PID"
echo "[episteme] a process exited — shutting down for a clean restart"
kill "$PY_PID" "$GW_PID" 2>/dev/null || true
exit 1
