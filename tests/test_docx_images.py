"""The Word file keeps the size and alignment a picture was given in the editor."""
import base64
from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH as A
from PIL import Image

from app.services.docx_from_html import DEFAULT_IMAGE_WIDTH_IN, TEXT_WIDTH_IN, generate_docx_from_html, image_layout


def _png_uri():
    buf = BytesIO()
    Image.new("RGB", (40, 20), "#1565c0").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _img(style):
    return BeautifulSoup(f'<img src="x" style="{style}">', "html.parser").img


@pytest.mark.parametrize("style,width,align", [
    ("max-width:50%;width:50%;margin:16px auto;", TEXT_WIDTH_IN * 0.5, A.CENTER),
    ("max-width:75%;width:75%;margin:16px 0;", TEXT_WIDTH_IN * 0.75, A.LEFT),
    ("max-width:100%;width:100%;margin:16px 0 16px auto;", TEXT_WIDTH_IN, A.RIGHT),
    ("width:288px;margin:16px auto;", 3.0, A.CENTER),                    # 288 px at 96 dpi
    ("width:4000px;margin:16px auto;", TEXT_WIDTH_IN, A.CENTER),         # never wider than the text
    ("width:10px;margin:16px auto;", 0.6, A.CENTER),                     # never vanishingly small
])
def test_size_and_alignment_come_from_the_editor_style(style, width, align):
    got_width, got_align = image_layout(_img(style))
    assert got_width == pytest.approx(width) and got_align == align


def test_a_picture_with_no_size_keeps_the_old_width_and_is_centred():
    # generated diagrams carry max-width but no width of their own
    assert image_layout(_img("max-width: 90%; height: auto; margin: 12px auto; display: block;")) == (DEFAULT_IMAGE_WIDTH_IN, A.CENTER)
    assert image_layout(_img("")) == (DEFAULT_IMAGE_WIDTH_IN, A.CENTER)


def _docx(style):
    html = f'<p>Q1. Study the figure.</p><img src="{_png_uri()}" alt="f" style="{style}">'
    return Document(BytesIO(generate_docx_from_html(html)))


def test_the_word_file_uses_the_size_and_alignment():
    doc = _docx("max-width:50%;width:50%;margin:16px 0;")
    (shape,) = doc.inline_shapes
    assert shape.width / 914400 == pytest.approx(TEXT_WIDTH_IN * 0.5, abs=0.01)
    carrier = next(p for p in doc.paragraphs if p._p.xpath(".//pic:pic"))
    assert carrier.alignment == A.LEFT


def test_the_word_file_keeps_five_inches_for_an_unsized_picture():
    doc = _docx("max-width: 90%; margin: 12px auto;")
    (shape,) = doc.inline_shapes
    assert shape.width / 914400 == pytest.approx(5.0, abs=0.01)
