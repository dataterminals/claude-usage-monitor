"""Generate the app window icon.

Draws the same mark the system tray uses (dark rounded square, calm-blue accent
from tray.py) with a small "usage gauge" motif — three ascending bars in the
app's green/amber/blue — and writes a multi-resolution Windows .ico used for the
native pywebview window's titlebar and taskbar entry, plus a PNG for anything
that wants one.

    python make_icons.py   ->  icons/app.ico   (16/32/48/64/128/256)
                               icons/icon-256.png

Re-run whenever the brand colors change. Outputs are committed so a plain
checkout (and the .exe build) has the window icon without Pillow at runtime.
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "icons")

# Palette lifted from dashboard.html / tray.py so everything matches.
FILL = (24, 26, 32, 255)      # tray icon body
ACCENT = (90, 162, 255, 255)  # --accent / CALM
GREEN = (67, 201, 139, 255)   # --good
AMBER = (242, 179, 75, 255)   # --warn
BLUE = (90, 162, 255, 255)    # --opus / accent


def _draw(size):
    """Render the icon at `size` px on a transparent canvas."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    m = max(1, int(size * 0.055))            # outer margin
    plate = [m, m, size - m, size - m]
    d.rounded_rectangle(plate, radius=int(size * 0.22), fill=FILL,
                        outline=ACCENT, width=max(1, int(size * 0.045)))

    # Three ascending gauge bars centered in the plate.
    plate_w = plate[2] - plate[0]
    plate_h = plate[3] - plate[1]
    inset_x = plate[0] + plate_w * 0.24
    inset_r = plate[2] - plate_w * 0.24
    base_y = plate[1] + plate_h * 0.70       # bars grow upward from here
    span_w = inset_r - inset_x
    bar_w = span_w * 0.22
    gap = (span_w - bar_w * 3) / 2
    heights = [0.22, 0.34, 0.48]             # fractions of plate_h
    colors = [GREEN, AMBER, BLUE]
    br = max(1, int(bar_w * 0.28))
    for i in range(3):
        x0 = inset_x + i * (bar_w + gap)
        h = plate_h * heights[i]
        d.rounded_rectangle([x0, base_y - h, x0 + bar_w, base_y],
                            radius=br, fill=colors[i])
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    # A crisp 256 master, downscaled for the smaller .ico frames.
    master = _draw(256)
    master.save(os.path.join(OUT, "icon-256.png"))
    ico_path = os.path.join(OUT, "app.ico")
    master.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48),
                                 (64, 64), (128, 128), (256, 256)])
    print("wrote", os.path.join("icons", "icon-256.png"))
    print("wrote", ico_path)


if __name__ == "__main__":
    main()
