"""Regenerates raster brand assets (favicons, apple-touch-icon, OG image) from
the same geometry as app/static/brand/*.svg. Run whenever the mark changes:
    .venv/Scripts/python.exe scripts/generate_brand_assets.py
Requires Pillow (build-time only - not a runtime app dependency, not in requirements.txt)."""
from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

STATIC = Path(__file__).parent.parent / "app" / "static"
INK = (7, 16, 24, 255)
SLATE = (58, 85, 104, 255)
BLUE = (126, 168, 255, 255)
MINT = (114, 230, 194, 255)
TEXT = (238, 244, 248, 255)
MUTED = (144, 163, 180, 255)

def draw_mark(size: int, *, with_tile: bool) -> Image.Image:
    """Draws the three-bar + evidence-ring mark scaled to `size`x`size`,
    using the same 32x32 coordinate system as logo-mark.svg/favicon.svg."""
    scale = size / 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if with_tile:
        radius = round(7 * scale)
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=INK)

    def bar(x, y, w, h, color):
        x0, y0, x1, y1 = x * scale, y * scale, (x + w) * scale, (y + h) * scale
        r = min(1.5 * scale, (x1 - x0) / 2)
        d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=color)

    bar(5, 17, 5, 9, SLATE)
    bar(13, 11, 5, 15, BLUE)
    bar(21, 5, 5, 21, MINT)
    cx, cy, r = 23.5 * scale, 5 * scale, 3.4 * scale
    sw = max(1, round(1.8 * scale))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=MINT, width=sw)
    return img

def save_favicons():
    out = STATIC / "brand"
    draw_mark(16, with_tile=True).save(out / "favicon-16x16.png")
    draw_mark(32, with_tile=True).save(out / "favicon-32x32.png")
    draw_mark(180, with_tile=True).save(out / "apple-touch-icon.png")
    draw_mark(192, with_tile=True).save(out / "icon-192.png")
    draw_mark(512, with_tile=True).save(out / "icon-512.png")
    icon16 = draw_mark(16, with_tile=True)
    icon32 = draw_mark(32, with_tile=True)
    icon48 = draw_mark(48, with_tile=True)
    icon16.save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)],
                append_images=[icon32, icon48])

def save_og_image():
    """1200x630 social preview - the mark plus the wordmark on the brand's
    ink background, no external assets, no false claims in the copy."""
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), INK[:3])
    d = ImageDraw.Draw(img)
    mark = draw_mark(160, with_tile=False)
    img.paste(mark, (W // 2 - 80, 150), mark)
    try:
        font_head = ImageFont.truetype("segoeuib.ttf", 54)
        font_sub = ImageFont.truetype("segoeui.ttf", 26)
    except Exception:
        font_head = ImageFont.load_default()
        font_sub = ImageFont.load_default()
    headline = "IPO Intelligence Terminal"
    sub = "Evidence-first IPO research for India and the United States"
    hw = d.textlength(headline, font=font_head)
    d.text((W / 2 - hw / 2, 350), headline, font=font_head, fill=TEXT)
    sw = d.textlength(sub, font=font_sub)
    d.text((W / 2 - sw / 2, 430), sub, font=font_sub, fill=MUTED)
    img.save(STATIC / "brand" / "og-image.png")

if __name__ == "__main__":
    (STATIC / "brand").mkdir(parents=True, exist_ok=True)
    save_favicons()
    save_og_image()
    print("brand assets written to", STATIC / "brand", "and", STATIC / "favicon.ico")
