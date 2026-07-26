# Episteme ASP — single image: Node x402 gateway (public) + Python runtime (private)
#   public :8080  ->  gateway/server.ts (official OKX @okxweb3/app-x402-* SDK)  ->  127.0.0.1:8890 FastAPI
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=22

# Node.js (for the OKX x402 seller SDK) + runtime libs for Pillow/lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg libjpeg62-turbo zlib1g libxml2 libxslt1.1 \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (pinned set that the test suite runs against)
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.7" "httpx>=0.27" \
      "cryptography>=42" "openai>=1.40" "pypdf>=4" "pillow>=10" "polars>=1.0" \
      "numpy>=1.26" "jsonschema>=4" "pyyaml>=6" "beautifulsoup4>=4.12" "lxml>=5" \
      "pygments>=2.17" "tabulate>=0.9" "python-multipart>=0.0.9" \
      "dnspython>=2.6"

# Node deps for the x402 gateway (cached layer)
COPY gateway/package.json gateway/package-lock.json* /app/gateway/
RUN cd /app/gateway && npm install --omit=dev --no-audit --no-fund

COPY . /app
RUN chmod +x /app/start.sh

# non-root; signing key + artifacts on a writable path
RUN useradd -m episteme && mkdir -p /app/.secrets /app/.artifacts && chown -R episteme /app
USER episteme

ENV EPISTEME_ARTIFACT_DIR=/app/.artifacts \
    EPISTEME_SIGNING_KEY=/app/.secrets/episteme_ed25519.key \
    PORT=8080
EXPOSE 8080

# Gate No.2: must stay reachable for the whole review window
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import httpx,os;httpx.get(f'http://127.0.0.1:{os.environ.get(\"PORT\",8080)}/healthz',timeout=4).raise_for_status()" || exit 1

CMD ["/app/start.sh"]
