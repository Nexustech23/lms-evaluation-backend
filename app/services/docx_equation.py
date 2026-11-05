# ============================================================
# Equations in the question-paper Word export.
# The editor saves each equation as
#   <img class="lms-eq" data-latex="…" data-display="0|1" data-w data-h data-d src="data:image/png;base64,…">
# with sizes in em (see src/lib/equation.js). Here the picture is placed inline in the paragraph, sized
# to the text and dropped below the baseline by its depth, so a formula sits on its line like text.
# ============================================================

import base64
import binascii
import logging
from io import BytesIO
from typing import Optional, Tuple

from docx.enum.text import WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

EQ_FONT_PX = 16  # the editor font the picture was sized for; keep equal to the frontend
EQ_SCALE = 3  # the frontend draws the picture 3x larger than shown
MAX_PNG_BYTES = 2 * 1024 * 1024
MAX_EM = 60
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def is_equation(element) -> bool:
    return getattr(element, "name", None) == "img" and element.has_attr("data-latex")


def contains_equation(element) -> bool:
    return hasattr(element, "find") and element.find("img", attrs={"data-latex": True}) is not None


def only_display_equations(element) -> bool:
    """True for a paragraph that holds nothing but equations shown on their own line."""
    equations = element.find_all("img", attrs={"data-latex": True})
    if not equations or any(e.get("data-display") != "1" for e in equations):
        return False
    return not any(piece.strip() for piece in element.find_all(string=True))


def equation_png(element) -> Optional[bytes]:
    src = element.get("src", "")
    if not (src.startswith("data:image/png;base64,")):
        return None
    try:
        data = base64.b64decode(src.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(data) > MAX_PNG_BYTES or not data.startswith(PNG_SIGNATURE):
        return None
    return data


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def equation_size_em(element, png: bytes) -> Optional[Tuple[float, float, float]]:
    """Width, height and baseline drop in em: from the saved attributes, else from the picture's pixels."""
    w, h, d = (_number(element.get(name)) for name in ("data-w", "data-h", "data-d"))
    if w is None or h is None or not (0 < w <= MAX_EM and 0 < h <= MAX_EM):
        try:
            from PIL import Image
            with Image.open(BytesIO(png)) as picture:
                width_px, height_px = picture.size
        except Exception:
            return None
        w, h, d = width_px / (EQ_FONT_PX * EQ_SCALE), height_px / (EQ_FONT_PX * EQ_SCALE), 0.0
        if not (0 < w <= MAX_EM and 0 < h <= MAX_EM):
            return None
    return w, h, min(max(d or 0.0, 0.0), h)


def add_equation(paragraph, element, base_size: float) -> bool:
    """Add the equation to the paragraph as a picture. If it cannot be used, keep its LaTeX as plain text."""
    png = equation_png(element)
    size = equation_size_em(element, png) if png else None
    if size is None:
        logging.warning("Equation picture unusable; its LaTeX was kept as text")
        run = paragraph.add_run(element.get("data-latex") or element.get("alt") or "")
        run.italic = True
        run.font.size = Pt(base_size)
        return False
    w, h, d = size
    run = paragraph.add_run()
    run.add_picture(BytesIO(png), width=Pt(w * base_size), height=Pt(h * base_size))
    drop = round(d * base_size * 2)  # Word measures the raise or drop in half points
    if drop > 0:
        position = OxmlElement("w:position")
        position.set(qn("w:val"), str(-drop))
        run._r.get_or_add_rPr().append(position)
    return True


def allow_tall_lines(paragraph) -> None:
    """The body paragraphs use an exact line height, which would cut off a tall fraction or matrix."""
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
