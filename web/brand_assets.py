"""The logo files served by the web UI, derived from the brand files in ``docs/design/brand/`` (brand.md "Logo").

Only the two variants the brand guide itself defines are derived, and nothing else is changed:

* ``*-on-navy.svg``: "On navy or dark images: name in white, symbol keeps its gradient" — the name path's fill
  ``#0B2A3C`` becomes ``#FFFFFF``; the symbol and its gradient are untouched.
* ``almiras-symbol*.svg``: the symbol alone ("use the symbol below 140 px") — the gradient path of the logo with the
  viewBox cropped to it; no path data is changed.

``python manage.py shell -c "from web.brand_assets import write; write()"`` regenerates them; the test
``web/test_brand.py`` checks that the static files equal what this module derives, so they cannot drift.
"""
import re
from pathlib import Path

from django.conf import settings

SOURCE = Path(settings.BASE_DIR) / "docs" / "design" / "brand"
TARGET = Path(__file__).resolve().parent / "static" / "web" / "brand"
NAVY = "#0B2A3C"
WHITE = "#FFFFFF"


def _read(name):
    return (SOURCE / name).read_text(encoding="utf-8")


def _on_navy(svg):
    if svg.count(f'fill="{NAVY}"') != 1:
        raise ValueError("Expected exactly one navy name path in the logo.")
    return svg.replace(f'fill="{NAVY}"', f'fill="{WHITE}"')


def _symbol(svg):
    """The gradient (symbol) path with its gradient definition and a viewBox fitted to its coordinates."""
    defs = re.search(r"<defs>.*?</defs>", svg, re.S).group(0)
    path = re.search(r'<path fill="url\(#[^)]+\)"[^>]*/>', svg).group(0)
    data = re.search(r' d="([^"]*)"', path).group(1)
    if re.search(r"[a-z]", data):
        raise ValueError("Relative path commands: bounds cannot be read from the numbers.")
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", data)]
    xs, ys = numbers[0::2], numbers[1::2]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    box = f"{x0:.2f} {y0:.2f} {x1 - x0:.2f} {y1 - y0:.2f}"
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}" role="img">{defs}{path}</svg>'


def derived():
    """``{file name: SVG text}`` of every logo file the UI serves."""
    ar, en = _read("almiras-logo-ar.svg"), _read("almiras-logo-en.svg")
    return {
        "almiras-logo-ar.svg": ar,
        "almiras-logo-en.svg": en,
        "almiras-logo-ar-on-navy.svg": _on_navy(ar),
        "almiras-logo-en-on-navy.svg": _on_navy(en),
        "almiras-symbol.svg": _symbol(ar),
    }


def write():
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, text in derived().items():
        (TARGET / name).write_text(text, encoding="utf-8")
