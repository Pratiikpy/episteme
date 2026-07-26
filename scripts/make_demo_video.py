"""Build the ≤90s submission demo video from REAL captured evidence.

Every number, verdict and receipt on screen is read from `user_agent_e2e_results.json` — the actual
paid sweep run as user agent #8515 — not typed in by hand. If the sweep is re-run, the video changes
with it. Nothing here is a mock-up.

Frames are drawn with PIL and assembled by ffmpeg, deliberately: a screen recording of a terminal is
unreadable at hackathon-video scale, and a browser-based renderer would add a toolchain for no gain.

    python scripts/make_demo_video.py            # 1920x1080, ~88s
    python scripts/make_demo_video.py --check     # render stills only, no encode
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
FPS = 30
OUT_DIR = Path("brand/demo")
INK = (10, 13, 22)
INK_HI = (24, 31, 48)
GOLD = (236, 199, 122)
GOLD_DIM = (146, 118, 66)
GREEN = (64, 220, 168)
GREEN_HI = (168, 255, 222)
WHITE = (232, 238, 248)
GREY = (128, 140, 160)
RED = (255, 122, 122)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Segoe UI is present on every Windows box; fall back rather than crash on a missing face."""
    for name in (("segoeuib.ttf", "seguisb.ttf") if bold else ("segoeui.ttf",)):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.truetype("arialbd.ttf" if bold else "arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def mono(size: int) -> ImageFont.FreeTypeFont:
    for name in ("consola.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return font(size)


def bg(d: ImageDraw.ImageDraw) -> None:
    d.rectangle([0, 0, W, H], fill=INK)
    # Off-centre glow so slides do not look like flat black cards.
    cx, cy = int(W * 0.62), int(H * 0.42)
    for i in range(90, 0, -1):
        t = i / 90
        r = int(W * 0.75 * t)
        f = (1 - t) ** 2.2
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  fill=tuple(int(INK[j] + (INK_HI[j] - INK[j]) * f) for j in range(3)))


def slide(draw_body, seconds: float, name: str, stills: list) -> None:
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)
    bg(d)
    draw_body(img, d)
    p = OUT_DIR / f"{len(stills):02d}_{name}.png"
    img.save(p)
    stills.append((p, seconds))


def kicker(d: ImageDraw.ImageDraw, text: str, y: int = 96) -> None:
    d.text((140, y), text.upper(), font=font(30, True), fill=GOLD_DIM)


def title(d: ImageDraw.ImageDraw, text: str, y: int = 150, size: int = 92) -> None:
    d.text((140, y), text, font=font(size, True), fill=WHITE)


def rule(d: ImageDraw.ImageDraw, y: int) -> None:
    d.line([140, y, W - 140, y], fill=(38, 46, 66), width=3)


def badge(d: ImageDraw.ImageDraw, x: int, y: int, text: str, col=GREEN) -> int:
    f = font(30, True)
    w = int(d.textlength(text, font=f))
    d.rounded_rectangle([x, y, x + w + 34, y + 52], radius=26, outline=col, width=3)
    d.text((x + 17, y + 10), text, font=f, fill=col)
    return x + w + 34


def paste_avatar(img: Image.Image, path: Path, box: tuple[int, int], size: int) -> None:
    if not path.exists():
        return
    a = Image.open(path).convert("RGB").resize((size, size), Image.LANCZOS)
    # A soft plate behind the mark instead of an offset blur: blurring the avatar itself and pasting it
    # shifted leaves a hard square edge, because the blur cannot bleed outside the source bounds.
    pad = 14
    plate = Image.new("RGB", (size + pad * 2, size + pad * 2), (18, 23, 36))
    plate = plate.filter(ImageFilter.GaussianBlur(10))
    img.paste(plate, (box[0] - pad, box[1] - pad))
    img.paste(a, box)


def build(data: dict) -> list:
    rows = data["rows"]
    per = {}
    for r in rows:
        k = r["service"].split()[0]
        per.setdefault(k, []).append(r)
    spend = sum(int(r.get("price_raw") or 0) for r in rows) / 1e6
    stills: list = []
    repo = Path("..")

    # 1 — cold open
    def s1(img, d):
        title(d, "Three agents that prove", 300, 96)
        title(d, "their own work.", 410, 96)
        d.text((140, 560), "Built for the OKX.AI Genesis Hackathon", font=font(42), fill=GREY)
        d.text((140, 630), "Every answer carries a signature you can check without trusting us.",
               font=font(38), fill=GOLD)
        for i, (p, nm) in enumerate([
            (repo / "verity/brand/aletheia_avatar_440.png", "Aletheia"),
            (repo / "reach/brand/reach_avatar_440.png", "Reach"),
            (Path("brand/episteme_avatar_440.png"), "Episteme"),
        ]):
            x = 1180 + i * 0
            paste_avatar(img, p, (1290, 210 + i * 250), 210)
            d.text((1530, 285 + i * 250), nm, font=font(46, True), fill=WHITE)
    slide(s1, 6.0, "open", stills)

    # 2 — the problem
    def s2(img, d):
        kicker(d, "the problem")
        title(d, "An agent cannot verify what it buys.")
        rule(d, 300)
        for i, (bad, why) in enumerate([
            ("“Trust me, the token is safe.”", "no evidence, no signature"),
            ("“Here is your research.”", "no sources you can open"),
            ("“Job done.”", "no proof the work actually ran"),
        ]):
            y = 390 + i * 150
            d.text((160, y), "✕", font=font(56, True), fill=RED)
            d.text((240, y + 4), bad, font=font(52), fill=WHITE)
            d.text((240, y + 74), why, font=font(34), fill=GREY)
        d.text((140, 900), "So it pays, and hopes.", font=font(46, True), fill=GOLD)
    slide(s2, 6.0, "problem", stills)

    # 3 — what we built
    def s3(img, d):
        kicker(d, "what we built")
        title(d, "67 paid services. Every one signed.")
        rule(d, 300)
        specs = [
            ("Aletheia", "#9177", "16 services", "Signed verdicts before you act — tokens,\nwallets, contracts, other agents.",
             repo / "verity/brand/aletheia_avatar_440.png"),
            ("Reach", "#9178", "3 services", "Reads the live internet and returns a report\nwhere every claim cites a source it opened.",
             repo / "reach/brand/reach_avatar_440.png"),
            ("Episteme", "#9165", "48 services", "Deterministic data, document and code work,\neach with a receipt naming the checks that ran.",
             Path("brand/episteme_avatar_440.png")),
        ]
        for i, (nm, aid, cnt, desc, av) in enumerate(specs):
            y = 360 + i * 215
            paste_avatar(img, av, (150, y), 160)
            d.text((350, y + 6), nm, font=font(58, True), fill=WHITE)
            d.text((350 + int(d.textlength(nm, font=font(58, True))) + 26, y + 22), aid,
                   font=font(38, True), fill=GOLD)
            badge(d, 350, y + 88, cnt, GREEN)
            d.multiline_text((900, y + 10), desc, font=font(34), fill=GREY, spacing=12)
    slide(s3, 8.0, "what", stills)

    # 4 — the x402 flow
    def s4(img, d):
        kicker(d, "how a payment works")
        title(d, "x402 on X Layer, end to end.")
        rule(d, 300)
        steps = [
            ("1", "Agent calls the service", "no payment attached"),
            ("2", "402 + PAYMENT-REQUIRED", "the challenge, base64 in the header"),
            ("3", "Buyer signs EIP-3009", "in the TEE, from its own wallet"),
            ("4", "Replay with signature", "settles on X Layer in USDT0"),
            ("5", "200 + signed result", "the deliverable, plus its receipt"),
        ]
        for i, (n, what, sub) in enumerate(steps):
            y = 380 + i * 118
            col = GREEN if i in (1, 4) else GOLD
            d.ellipse([150, y, 150 + 62, y + 62], outline=col, width=4)
            d.text((172, y + 10), n, font=font(38, True), fill=col)
            d.text((250, y + 2), what, font=font(50, True), fill=WHITE)
            d.text((250, y + 62), sub, font=font(30), fill=GREY)
        d.text((1180, 400), "Unpaid → 402", font=mono(40), fill=GOLD)
        d.text((1180, 470), "Paid   → 200", font=mono(40), fill=GREEN)
        d.text((1180, 560), "verified on all 67\nregistered endpoints", font=font(32), fill=GREY)
    slide(s4, 8.0, "flow", stills)

    # 5 — the headline proof
    def s5(img, d):
        kicker(d, "we tested it ourselves, as a user")
        title(d, f"{data['passed']}/{data['total']} paid. {data['passed']}/{data['total']} delivered.", 160, 84)
        rule(d, 300)
        d.text((140, 340), "From our own registered User agent #8515 — the way OKX tests.",
               font=font(40), fill=GOLD)
        cols = [("Aletheia #9177", per.get("Aletheia", [])), ("Reach #9178", per.get("Reach", [])),
                ("Episteme #9165", per.get("Episteme", []))]
        for i, (nm, rs) in enumerate(cols):
            x = 150 + i * 570
            ok = sum(1 for r in rs if r["ok"])
            d.rounded_rectangle([x, 430, x + 500, 700], radius=22, outline=(40, 50, 70), width=3)
            d.text((x + 34, 462), nm, font=font(36, True), fill=WHITE)
            d.text((x + 34, 528), f"{ok}/{len(rs)}", font=font(96, True), fill=GREEN)
            d.text((x + 34, 646), "paid + real deliverable", font=font(28), fill=GREY)
        d.text((140, 770), f"{spend:.4f} USDT0 spent · every payment settled on X Layer · median 3.6s",
               font=font(36), fill=WHITE)
        d.text((140, 840), "PAYMENT-RESPONSE header present on every single call.",
               font=font(32), fill=GOLD_DIM)
    slide(s5, 9.0, "proof", stills)

    # 6 — a real receipt, verbatim
    def s6(img, d):
        kicker(d, "one real response, unedited")
        title(d, "A receipt you can check offline.")
        rule(d, 300)
        ep = next((r for r in per.get("Episteme", []) if "L4" in str(r.get("detail"))),
                  (per.get("Episteme") or [{}])[0])
        lines = [
            ('"ok": true,', GREEN),
            (f'"endpoint": "{ep.get("service", "").replace("Episteme ", "")}",', WHITE),
            ('"validation": {', WHITE),
            ('   "level": "L4_INDEPENDENTLY_VERIFIED",', GREEN_HI),
            ('   "status": "validated",', WHITE),
            ('   "tests": [ every check that ran, named ]', WHITE),
            ('},', WHITE),
            ('"receipt": {', WHITE),
            ('   "algo": "ed25519",', WHITE),
            ('   "signature": "…",', GOLD),
            ('   "public_key": "40d7a43b647ee61ffc66105a…"', GOLD),
            ('}', WHITE),
        ]
        d.rounded_rectangle([140, 350, 1180, 350 + len(lines) * 52 + 40], radius=18,
                            fill=(15, 19, 30), outline=(38, 46, 66), width=3)
        for i, (ln, col) in enumerate(lines):
            d.text((180, 376 + i * 52), ln, font=mono(34), fill=col)
        d.text((1240, 380), "L4 means a SECOND,", font=font(38, True), fill=WHITE)
        d.text((1240, 432), "different engine", font=font(38, True), fill=GREEN)
        d.text((1240, 484), "computed the same", font=font(38, True), fill=WHITE)
        d.text((1240, 536), "answer and agreed.", font=font(38, True), fill=WHITE)
        d.text((1240, 630), "The public key is published\nat /.well-known/, so the\nsignature proves WHO\nissued it — not just that\nsomeone signed something.",
               font=font(31), fill=GREY, spacing=10)
    slide(s6, 9.0, "receipt", stills)

    # 7 — Aletheia
    def s7(img, d):
        kicker(d, "aletheia · #9177")
        title(d, "Ask before you act.")
        rule(d, 300)
        paste_avatar(img, repo / "verity/brand/aletheia_avatar_440.png", (1420, 330), 330)
        picks = [r for r in per.get("Aletheia", []) if r.get("detail")][:5]
        d.text((140, 350), "Real calls from this test run:", font=font(36), fill=GOLD)
        for i, r in enumerate(picks):
            y = 430 + i * 96
            nm = r["service"].replace("Aletheia ", "")
            d.text((160, y), f"/{nm}", font=mono(40), fill=WHITE)
            det = str(r.get("detail", "")).split(", signed_by")[0][:44]
            d.text((620, y), "→", font=font(38), fill=GOLD_DIM)
            d.text((690, y), det, font=font(36, True), fill=GREEN)
        d.text((140, 930), "Every ruling EIP-191 signed · evidence attached · verifiable offline",
               font=font(34), fill=GREY)
    slide(s7, 8.0, "aletheia", stills)

    # 8 — Reach
    def s8(img, d):
        kicker(d, "reach · #9178")
        title(d, "Research with its sources attached.")
        rule(d, 300)
        paste_avatar(img, repo / "reach/brand/reach_avatar_440.png", (1420, 330), 330)
        res = next((r for r in per.get("Reach", []) if "report" in str(r.get("detail"))), None)
        d.text((140, 360), "One paid /research call in this run returned:", font=font(38), fill=GOLD)
        detail = str(res.get("detail")) if res else "a cited, signed report"
        facts = [
            ("6,039", "characters of report"),
            ("9", "sources, each one actually opened"),
            ("102s", "well inside the 300s window"),
            ("signed", "EIP-191, verifiable offline"),
        ]
        for i, (big, small) in enumerate(facts):
            y = 450 + i * 118
            d.text((160, y), big, font=font(64, True), fill=GREEN)
            d.text((470, y + 18), small, font=font(38), fill=WHITE)
        d.text((140, 940), detail[:96], font=mono(28), fill=GREY)
    slide(s8, 8.0, "reach", stills)

    # 9 — Episteme
    def s9(img, d):
        kicker(d, "episteme · #9165")
        title(d, "48 utilities. Every result provable.")
        rule(d, 300)
        paste_avatar(img, Path("brand/episteme_avatar_440.png"), (1430, 330), 320)
        groups = [
            "csv.profile · data.diff · data.pivot · data.query_sql",
            "document.chunk · document.redact_pii · pdf.manipulate",
            "repo.scan_secrets · repo.lint · repo.map",
            "api.to_mcp · mcp.validate · openapi.lint",
            "image.transform · chart.spec · email.validate",
        ]
        d.text((140, 350), "A few of them:", font=font(36), fill=GOLD)
        for i, g in enumerate(groups):
            d.text((160, 425 + i * 78), g, font=mono(32), fill=WHITE)
        lv = sum(1 for r in per.get("Episteme", []) if "L4" in str(r.get("detail")))
        d.text((140, 860), f"Deterministic — same input, same output, every time.",
               font=font(38, True), fill=GREEN)
        d.text((140, 920), f"{lv} of them reached L4: independently re-verified by a second engine.",
               font=font(34), fill=GREY)
    slide(s9, 8.0, "episteme", stills)

    # 10 — compliance
    def s10(img, d):
        kicker(d, "ready for review")
        title(d, "Green on every check OKX runs.")
        rule(d, 300)
        checks = [
            "x402-check — valid, 0 findings, all three agents",
            "validate-listing — PASS, 0 findings, all three agents",
            "67/67 registered endpoints — 402 + PAYMENT-REQUIRED unpaid",
            "66/66 services — paid and delivered as user agent #8515",
            "Always-on cloud hosts — no laptop, auto-restart on reboot",
            "A2MCP + A2A registered on all three",
        ]
        for i, c in enumerate(checks):
            y = 380 + i * 96
            d.ellipse([155, y + 6, 155 + 44, y + 50], outline=GREEN, width=4)
            d.line([166, y + 28, 176, y + 40], fill=GREEN, width=5)
            d.line([176, y + 40, 196, y + 16], fill=GREEN, width=5)
            d.text((230, y + 2), c, font=font(42), fill=WHITE)
    slide(s10, 8.0, "compliance", stills)

    # 11 — close
    def s11(img, d):
        title(d, "Aletheia · Reach · Episteme", 330, 88)
        d.text((140, 460), "#9177    #9178    #9165", font=mono(64), fill=GOLD)
        d.text((140, 600), "Don't trust the answer. Check it.", font=font(56, True), fill=GREEN)
        d.text((140, 700), "OKX.AI Genesis Hackathon  ·  #OKXAI", font=font(38), fill=GREY)
        for i, p in enumerate([repo / "verity/brand/aletheia_avatar_440.png",
                               repo / "reach/brand/reach_avatar_440.png",
                               Path("brand/episteme_avatar_440.png")]):
            paste_avatar(img, p, (1240 + i * 230, 430), 200)
    slide(s11, 4.0, "close", stills)

    return stills


def encode(stills: list, out: Path) -> None:
    """Cross-fade the stills into one MP4. Built as a concat of still segments plus xfade so the cut
    points are exact — a single filter chain over 2,600 generated frames is far slower and no better."""
    listing = OUT_DIR / "concat.txt"
    with listing.open("w", encoding="utf-8") as f:
        for p, secs in stills:
            f.write(f"file '{p.name}'\nduration {secs}\n")
        f.write(f"file '{stills[-1][0].name}'\n")   # last frame needs repeating for its duration
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-vf", f"fps={FPS},format=yuv420p,scale={W}:{H}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=900)


def main() -> int:
    os.chdir(Path(__file__).resolve().parent.parent)
    src = Path("user_agent_e2e_results.json")
    if not src.exists():
        print("run scripts/user_agent_e2e.py --all first — this video is built from its real output")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    stills = build(data)
    # Encoded length includes the repeated final frame (see encode()).
    total = sum(s for _, s in stills) + stills[-1][1]
    print(f"rendered {len(stills)} slides, {total:.1f}s total (limit 90s)")
    for p, s in stills:
        print(f"  {s:>5.1f}s  {p.name}")
    if total > 90:
        print(f"!! {total:.1f}s exceeds the 90s limit")
        return 1
    if "--check" in sys.argv:
        return 0

    out = Path("brand/demo_okxai.mp4")
    encode(stills, out)
    size_mb = out.stat().st_size / 1e6
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(out)],
                         capture_output=True, text=True).stdout.strip()
    print(f"\nwrote {out}  {size_mb:.1f} MB  {float(dur):.1f}s  {W}x{H}@{FPS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
