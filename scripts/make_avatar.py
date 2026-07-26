"""Generate the Episteme brand avatar — geometric seal, non-face, square, small PNG.
Concept: many messy inputs (edges) converge into ONE sealed, verified node.
OKX rules: image FILE, <=1MB, PNG/JPEG/WebP, not a real person.
"""
import math
from PIL import Image, ImageDraw

S = 512
BG = (11, 13, 18)        # obsidian
ACCENT = (64, 224, 168)  # verified accent
DIM = (86, 96, 112)      # dim edges

img = Image.new("RGB", (S, S), BG)
d = ImageDraw.Draw(img)
cx = cy = S // 2

# outer seal ring
d.ellipse([44, 44, S - 44, S - 44], outline=(38, 44, 56), width=10)
d.ellipse([64, 64, S - 64, S - 64], outline=(28, 33, 42), width=2)

# converging input edges (many -> one)
R_OUT, R_IN = 196, 74
for i in range(12):
    a = math.radians(i * 30 - 90)
    x1, y1 = cx + R_OUT * math.cos(a), cy + R_OUT * math.sin(a)
    x2, y2 = cx + R_IN * math.cos(a), cy + R_IN * math.sin(a)
    d.line([x1, y1, x2, y2], fill=DIM, width=4)
    d.ellipse([x1 - 7, y1 - 7, x1 + 7, y1 + 7], fill=(52, 60, 74))

# central verified node (hexagon = the sealed artifact)
hexr = 62
pts = [(cx + hexr * math.cos(math.radians(60 * k - 90)),
        cy + hexr * math.sin(math.radians(60 * k - 90))) for k in range(6)]
d.polygon(pts, fill=(17, 22, 30), outline=ACCENT)
d.line(pts + [pts[0]], fill=ACCENT, width=6, joint="curve")

# check mark inside = verified
d.line([cx - 26, cy + 2, cx - 8, cy + 22, cx + 28, cy - 22], fill=ACCENT, width=11, joint="curve")

img.save("brand/episteme_avatar.png", format="PNG", optimize=True)
import os
print("saved brand/episteme_avatar.png")
print("size:", img.size, "| bytes:", os.path.getsize("brand/episteme_avatar.png"))
print("square:", img.size[0] == img.size[1], "| <=1MB:", os.path.getsize("brand/episteme_avatar.png") <= 1024 * 1024)
