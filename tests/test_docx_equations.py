"""Equations saved by the paper editor are placed inline in the Word file, sized to the text."""
import base64
from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Pt
from PIL import Image

from app.services import docx_equation
from app.services.docx_from_html import generate_docx_from_html, process_and_upload_base64_images


def png_bytes(width=96, height=64):
    buffer = BytesIO()
    Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def png_data_uri(width=96, height=64):
    return "data:image/png;base64," + base64.b64encode(png_bytes(width, height)).decode()


def equation(latex="x^2", display="0", w="2", h="1.5", d="0.4", src=None):
    attrs = f'class="lms-eq" src="{png_data_uri() if src is None else src}" alt="{latex}" data-latex="{latex}" data-display="{display}"'
    for name, value in (("data-w", w), ("data-h", h), ("data-d", d)):
        if value is not None:
            attrs += f' {name}="{value}"'
    return f"<img {attrs}>"


def build(html):
    return Document(BytesIO(generate_docx_from_html(html)))


def drawings(paragraph):
    return paragraph._p.xpath(".//w:drawing")


def with_picture(document):
    """The paragraphs of the paper body that hold a picture (the exam header and footer hold none)."""
    return [p for p in document.paragraphs if drawings(p)]


def marked(document, marker="MARK"):
    return [p for p in document.paragraphs if marker in p.text][0]


def test_an_equation_is_placed_inline_in_its_paragraph_not_as_a_separate_picture():
    document = build(f"<p>MARK Find v = {equation()} in m/s</p>")
    pictures = with_picture(document)
    assert len(pictures) == 1 and len(document.inline_shapes) == 1
    assert "Find v =" in pictures[0].text and "in m/s" in pictures[0].text
    assert pictures[0].alignment != WD_ALIGN_PARAGRAPH.CENTER


def test_the_picture_is_sized_in_em_of_the_11_point_text():
    shape = build(f"<p>{equation(w='2', h='1.5')}</p>").inline_shapes[0]
    assert shape.width == Pt(22) and shape.height == Pt(16.5)


def test_the_picture_is_dropped_below_the_baseline_by_its_depth():
    paragraph = with_picture(build(f"<p>a {equation(d='0.4')}</p>"))[0]
    position = paragraph._p.xpath(".//w:position")
    assert len(position) == 1 and position[0].get(qn("w:val")) == "-9"  # 0.4 em of 11 pt = 4.4 pt = 9 half points


def test_an_equation_with_no_depth_is_not_moved():
    paragraph = with_picture(build(f"<p>a {equation(d='0')}</p>"))[0]
    assert paragraph._p.xpath(".//w:position") == []


def test_a_tall_equation_keeps_its_height_because_the_line_may_grow():
    paragraph = with_picture(build(f"<p>m = {equation(h='2.4')}</p>"))[0]
    assert paragraph.paragraph_format.line_spacing_rule == WD_LINE_SPACING.AT_LEAST


def test_a_paragraph_without_equations_keeps_its_exact_line_height():
    paragraph = marked(build("<p>MARK No formulas here</p>"))
    assert paragraph.paragraph_format.line_spacing_rule == WD_LINE_SPACING.EXACTLY


def test_a_display_equation_on_its_own_is_centred():
    paragraph = with_picture(build(f"<p>{equation(display='1')}</p>"))[0]
    assert paragraph.alignment == WD_ALIGN_PARAGRAPH.CENTER


def test_a_display_equation_beside_text_is_not_centred():
    paragraph = with_picture(build(f"<p>Then {equation(display='1')}</p>"))[0]
    assert paragraph.alignment != WD_ALIGN_PARAGRAPH.CENTER


def test_an_equation_inside_bold_keeps_the_style_and_the_picture():
    paragraph = with_picture(build(f"<p>Before <strong>bold {equation()} after</strong> end</p>"))[0]
    assert len(drawings(paragraph)) == 1
    bold = [r.text for r in paragraph.runs if r.bold]
    assert any("bold" in t for t in bold) and any("after" in t for t in bold)
    assert "Before" in paragraph.text and "end" in paragraph.text and not any("Before" in t for t in bold)


def test_equations_work_in_list_items_and_table_cells():
    html = (f"<ul><li>one {equation()}</li></ul><ol><li>two {equation()}</li></ol>"
            f"<table><tr><th>H {equation()}</th></tr><tr><td>C {equation()}</td></tr></table>")
    document = build(html)
    assert len(document.inline_shapes) == 4
    cells = [c.text for t in document.tables for r in t.rows for c in r.cells]
    assert any(text.startswith("H") for text in cells) and any(text.startswith("C") for text in cells)


def test_several_equations_in_one_paragraph_stay_in_order():
    paragraph = with_picture(build(f"<p>a {equation('x')} b {equation('y')} c</p>"))[0]
    assert len(drawings(paragraph)) == 2
    assert paragraph.text.index("a") < paragraph.text.index("b") < paragraph.text.index("c")


def test_an_ordinary_image_is_still_a_separate_centred_picture():
    document = build(f'<p>Figure</p><img src="{png_data_uri()}">')
    pictures = with_picture(document)
    assert len(pictures) == 1 and pictures[0].alignment == WD_ALIGN_PARAGRAPH.CENTER


def test_an_ordinary_image_inside_a_paragraph_is_added_once_and_the_equation_is_not_repeated():
    document = build(f'<p>See {equation()} and <img src="{png_data_uri()}"></p>')
    assert len(document.inline_shapes) == 2  # the equation inline + the ordinary image after the paragraph


@pytest.mark.parametrize("src", ["", "data:image/jpeg;base64,AAAA", "data:image/png;base64,not base64!!", "https://example.com/x.png",
                                 "data:image/png;base64," + base64.b64encode(b"not a png").decode()])
def test_an_unusable_picture_keeps_the_latex_as_text(src):
    document = build(f"<p>MARK f = {equation('E=mc^2', src=src)}</p>")
    assert len(document.inline_shapes) == 0
    assert "E=mc^2" in marked(document).text


@pytest.mark.parametrize("w,h", [("0", "1"), ("-1", "1"), ("999", "1"), ("1", "nan"), ("abc", "abc"), (None, None)])
def test_nonsense_or_missing_sizes_fall_back_to_the_pictures_own_pixels(w, h):
    shapes = build(f"<p>{equation(w=w, h=h, d='0')}</p>").inline_shapes
    assert len(shapes) == 1  # 96x64 px drawn at 3x of 16 px is 2 x 1.333 em of 11 pt
    assert shapes[0].width == pytest.approx(Pt(22), abs=Pt(0.1)) and shapes[0].height == pytest.approx(Pt(14.67), abs=Pt(0.1))


def test_the_depth_can_never_exceed_the_height():
    element = BeautifulSoup(equation(w="2", h="1", d="5"), "html.parser").img
    assert docx_equation.equation_size_em(element, png_bytes())[2] == 1.0


def test_a_picture_too_large_to_trust_is_not_embedded():
    big = "data:image/png;base64," + base64.b64encode(docx_equation.PNG_SIGNATURE + b"0" * (docx_equation.MAX_PNG_BYTES + 1)).decode()
    document = build(f"<p>MARK {equation('big', src=big)}</p>")
    assert len(document.inline_shapes) == 0 and "big" in marked(document).text


# ---- saving: equation pictures are not uploaded one by one ----

def test_saving_uploads_ordinary_images_but_keeps_equations_inside_the_paper(monkeypatch):
    uploads = []

    def fake_upload(data, name, folder, tags):
        uploads.append(name)
        return {"url": "https://ik.example/" + name, "file_id": "f"}

    import app.services.imagekit as imagekit
    monkeypatch.setattr(imagekit, "upload_file_to_imagekit", fake_upload)
    html = f'<p>a {equation("x^2")} b</p><img src="{png_data_uri(8, 8)}">'
    result = process_and_upload_base64_images(html, "faculty-1")
    assert len(uploads) == 1 and uploads[0].startswith("editor-image-")
    assert "https://ik.example/" in result and 'data-latex="x^2"' in result
    assert result.count("data:image/png;base64,") == 1  # only the equation still carries its picture
