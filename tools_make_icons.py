"""Draws the home-screen icons (static/icon-180.png, icon-512.png)."""
from PIL import Image, ImageDraw

def icon(size: int) -> Image.Image:
    s = size * 4  # supersample, then downscale for smooth edges
    img = Image.new("RGB", (s, s))
    d = ImageDraw.Draw(img)
    for y in range(s):  # vertical gradient
        t = y / s
        c = tuple(int(a + (b - a) * t) for a, b in zip((34, 38, 46), (11, 12, 15)))
        d.line([(0, y), (s, y)], fill=c)
    cx = cy = s / 2
    r = s * 0.33
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(31, 34, 41), outline=(70, 76, 88), width=int(s * 0.012))
    # arrows
    a = s * 0.055
    off = r * 0.72
    grey = (150, 156, 167)
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        px, py = cx + dx * off, cy + dy * off
        if dy:
            pts = [(px - a, py + dy * -a * 0.55), (px, py + dy * a * 0.55), (px + a, py + dy * -a * 0.55)]
        else:
            pts = [(px + dx * -a * 0.55, py - a), (px + dx * a * 0.55, py), (px + dx * -a * 0.55, py + a)]
        d.line(pts, fill=grey, width=int(s * 0.022), joint="curve")
    ok = r * 0.40
    d.ellipse([cx - ok, cy - ok, cx + ok, cy + ok], fill=(118, 185, 0))
    return img.resize((size, size), Image.LANCZOS)

for n in (180, 512):
    icon(n).save(f"static/icon-{n}.png", optimize=True)
print("ok")
