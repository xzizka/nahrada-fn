#!/usr/bin/env python3
"""Generates a small Czech-language PDF used as the hello-world fixture for
scripts/smoke-test.sh. Embeds DejaVuSans - reportlab's built-in fonts are
Latin-1 only and would silently drop the diacritics this test depends on.
"""

from __future__ import annotations

import sys

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

DEJAVU_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]

TEXT = (
    "Smlouva o dílo číslo 2026/001. Objednatel se zavazuje uhradit zálohu "
    "do třiceti dnů. Na dílo se vztahuje záruka v délce dvaceti čtyř "
    "měsíců. Veškeré změny smlouvy vyžadují písemnou formu."
)


def _register_dejavu() -> str:
    for path in DEJAVU_PATHS:
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans", path))
            return "DejaVuSans"
        except OSError:
            continue
    raise SystemExit(
        "DejaVuSans.ttf not found in any of "
        f"{DEJAVU_PATHS} - install fonts-dejavu-core in the image."
    )


def make_pdf(output_path: str, text: str = TEXT) -> None:
    font_name = _register_dejavu()

    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    c.setFont(font_name, 12)

    margin = 60
    max_width = width - 2 * margin
    y = height - margin

    words = text.split(" ")
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if pdfmetrics.stringWidth(candidate, font_name, 12) > max_width:
            c.drawString(margin, y, line)
            y -= 18
            line = word
        else:
            line = candidate
    if line:
        c.drawString(margin, y, line)

    c.showPage()
    c.save()


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(f"usage: {sys.argv[0]} <output.pdf> [text]", file=sys.stderr)
        raise SystemExit(2)
    make_pdf(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else TEXT)
    print(f"Wrote {sys.argv[1]}")
