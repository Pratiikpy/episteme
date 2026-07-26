"""Copy the OKX facilitator creds from verity/.env into gateway/.env.real (gitignored)
so the Node gateway can run in REAL x402 mode locally. Prints names only."""
from pathlib import Path

SRC = Path(r"C:\Users\prate\okx\verity\.env")
WANT = {"OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"}
lines = []
for raw in SRC.read_text(encoding="utf-8", errors="replace").splitlines():
    s = raw.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k, _, v = s.partition("=")
    k = k.strip()
    v = v.strip().strip('"').strip("'")
    if k in WANT and v:
        lines.append(f"{k}={v}")

out = Path("gateway/.env.real")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("wrote", out, "with vars:", [l.split("=")[0] for l in lines])
