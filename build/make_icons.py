"""
Generate assets/klearing.ico and assets/klearing.icns from the icon
geometry in assets/klearing.svg (drawn here with Pillow rather than
rasterizing the SVG, so the build needs no cairo/inkscape — the two
files describe the same shapes; change them together).

Run from the repo root:  venv/Scripts/python.exe build/make_icons.py
Pillow is the only dependency (build-time only, not shipped).
"""

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"

TILE = (247, 246, 242, 255)      # off-white
TILE_EDGE = (228, 228, 231, 255)  # hairline border
SLATE = (24, 24, 27, 255)         # ink
ACCENT = (37, 99, 235, 255)       # blue-600

# Geometry on the 512 canvas, mirrored from klearing.svg.
CANVAS = 512
CORNER = 104
STROKE = 62
STEM = ((168, 132), (168, 380))
ARM = ((180, 266), (330, 130))
CHECK = ((180, 266), (288, 378), (428, 192))

SUPERSAMPLE = 4


def _capped_line(draw, points, width, fill):
    """A stroked polyline with round caps and joins (PIL's joint=curve
    covers joins; caps come from endpoint circles)."""
    draw.line(points, fill=fill, width=width, joint="curve")
    radius = width / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=fill)


def render(size):
    """One RGBA icon frame at the requested square size."""
    s = CANVAS * SUPERSAMPLE
    scale = SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    inset = 8 * scale
    draw.rounded_rectangle((inset, inset, s - inset, s - inset),
                           radius=CORNER * scale, fill=TILE,
                           outline=TILE_EDGE, width=8 * scale)

    def pts(seq):
        return [(x * scale, y * scale) for x, y in seq]

    _capped_line(draw, pts(STEM), STROKE * scale, SLATE)
    _capped_line(draw, pts(ARM), STROKE * scale, SLATE)
    _capped_line(draw, pts(CHECK), STROKE * scale, ACCENT)

    return img.resize((size, size), Image.LANCZOS)


def main():
    ASSETS.mkdir(exist_ok=True)

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = {size: render(size) for size in ico_sizes}
    frames[256].save(
        ASSETS / "klearing.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=[frames[s] for s in ico_sizes if s != 256],
    )
    print(f"wrote {ASSETS / 'klearing.ico'}")

    icns_sizes = [16, 32, 64, 128, 256, 512]
    icns_frames = [render(s) for s in icns_sizes]
    icns_frames[-1].save(
        ASSETS / "klearing.icns",
        format="ICNS",
        append_images=icns_frames[:-1],
    )
    print(f"wrote {ASSETS / 'klearing.icns'}")

    # A PNG for the README / release page.
    render(256).save(ASSETS / "klearing-256.png")
    print(f"wrote {ASSETS / 'klearing-256.png'}")


if __name__ == "__main__":
    main()
