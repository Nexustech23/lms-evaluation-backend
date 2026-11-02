"""Offline checks for the Gemini image-resolution settings; every provider call is mocked."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.services import gemini


def _response(text="ok"):
    return SimpleNamespace(text=text, usage_metadata=None, candidates=[])


@pytest.mark.parametrize("level,expected", [
    ("high", "MEDIA_RESOLUTION_HIGH"), ("medium", "MEDIA_RESOLUTION_MEDIUM"), ("low", "MEDIA_RESOLUTION_LOW")])
def test_resolution_levels_map_to_gemini_setting(level, expected):
    config = gemini._media_resolution_config(level)
    assert config.media_resolution.name == expected


@pytest.mark.parametrize("level", [None, "", "default"])
def test_default_resolution_leaves_the_model_default(level):
    assert gemini._media_resolution_config(level) is None


def test_answer_ocr_passes_the_resolution_to_gemini(monkeypatch):
    call = MagicMock(return_value=_response())
    monkeypatch.setattr(gemini, "_get_client", lambda: object())
    monkeypatch.setattr(gemini, "_call_generate_content_with_retry", call)
    gemini.generate_content_from_file_checked(b"%PDF", "application/pdf", "read", model="m", media_resolution="high")
    assert call.call_args.kwargs["config"].media_resolution.name == "MEDIA_RESOLUTION_HIGH"
    call.reset_mock()
    gemini.generate_content_from_file_checked(b"%PDF", "application/pdf", "read", model="m")
    assert call.call_args.kwargs["config"] is None


def test_ocr_resolution_defaults_to_high_and_paper_rendering_to_off():
    assert settings.GEMINI_OCR_MEDIA_RESOLUTION == "high"
    assert settings.PAPER_EXTRACT_RENDER_DPI == 0


def test_paper_pdf_is_sent_unchanged_when_rendering_is_off(monkeypatch):
    client = MagicMock(); client.models.generate_content.return_value = _response("paper")
    monkeypatch.setattr(gemini, "_get_client", lambda: client)
    monkeypatch.setattr(settings, "PAPER_EXTRACT_RENDER_DPI", 0)
    monkeypatch.setattr(gemini, "_render_pdf_pages", MagicMock(side_effect=AssertionError("must not render")))
    text, _ = gemini.extract_question_paper_text_with_gemini(b"%PDF", "paper.pdf")
    assert text == "paper"
    assert "config" not in client.models.generate_content.call_args.kwargs


def test_paper_pages_are_rendered_at_the_set_dpi_and_read_at_high_resolution(monkeypatch):
    client = MagicMock(); client.models.generate_content.return_value = _response("paper")
    render = MagicMock(return_value=["page1", "page2"])
    monkeypatch.setattr(gemini, "_get_client", lambda: client)
    monkeypatch.setattr(gemini, "_render_pdf_pages", render)
    monkeypatch.setattr(settings, "PAPER_EXTRACT_RENDER_DPI", 300)
    gemini.extract_question_paper_text_with_gemini(b"%PDF", "paper.pdf")
    render.assert_called_once_with(b"%PDF", 300)
    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["contents"][:2] == ["page1", "page2"]
    assert kwargs["config"].media_resolution.name == "MEDIA_RESOLUTION_HIGH"


def test_render_pdf_pages_returns_one_png_part_per_page():
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    for _ in range(2):
        document.new_page().insert_text((72, 72), "x^2")
    parts = gemini._render_pdf_pages(document.tobytes(), 100)
    assert len(parts) == 2
    assert all(p.inline_data.mime_type == "image/png" and p.inline_data.data[:4] == b"\x89PNG" for p in parts)


def test_answer_ocr_requests_the_configured_resolution(monkeypatch):
    import io
    import PyPDF2
    import app.services.grading as grading_module
    writer = PyPDF2.PdfWriter(); writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO(); writer.write(buffer)
    seen = {}

    def fake(pdf_bytes, mime_type, prompt, model=None, media_resolution=None):
        seen["resolution"] = media_resolution
        return "text\nPAGE_COMPLETE", {"prompt_tokens": 1, "candidate_tokens": 1, "total_tokens": 2}, False

    monkeypatch.setattr(grading_module, "generate_content_from_file_checked", fake)
    monkeypatch.setattr(settings, "GEMINI_OCR_MEDIA_RESOLUTION", "high")
    grading_module.extract_answer_text_with_gemini(buffer.getvalue())
    assert seen["resolution"] == "high"


def test_paper_extraction_uses_the_configured_model_for_every_input_type(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_PAPER_MODEL", "model-under-test")
    monkeypatch.setattr(settings, "PAPER_EXTRACT_RENDER_DPI", 0)
    client = MagicMock(); client.models.generate_content.return_value = _response("paper")
    monkeypatch.setattr(gemini, "_get_client", lambda: client)
    gemini.extract_question_paper_text_with_gemini(b"%PDF", "paper.pdf")
    monkeypatch.setattr(settings, "PAPER_EXTRACT_RENDER_DPI", 300)
    monkeypatch.setattr(gemini, "_render_pdf_pages", MagicMock(return_value=["page"]))
    gemini.extract_question_paper_text_with_gemini(b"%PDF", "paper.pdf")
    monkeypatch.setattr(gemini, "extract_text_from_file", lambda data, name: "typed text")
    gemini.extract_question_paper_text_with_gemini(b"x", "paper.docx")
    assert [c.kwargs["model"] for c in client.models.generate_content.call_args_list] == ["model-under-test"] * 3


def test_paper_model_defaults_to_flash_lite_and_has_a_price():
    from app.utils.ai_pricing import _PRICES
    assert settings.GEMINI_PAPER_MODEL == "gemini-3.1-flash-lite"
    assert settings.GEMINI_PAPER_MODEL in _PRICES
