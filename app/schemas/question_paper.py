from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class RenderDiagramRequest(BaseModel):
    # spec's internal shape is fully validated (and safely evaluated via
    # asteval, not eval()) by app.services.diagram_render.draw_diagram —
    # kept loose here, this only guarantees the key is present.
    spec: Dict[str, Any]


class QuestionPaperSaveRequest(BaseModel):
    # app.models.question_paper.build_create_document only strictly
    # requires subjectId; every other field is optional header/content
    # metadata read defensively via .get(). extra="allow" preserves the
    # original flexibility for the many optional passthrough fields
    # (instituteName, departmentName, examType, subjectName, semester,
    # academicYear, duration, totalMarks, generationSource, promptUsed, ...).
    model_config = ConfigDict(extra="allow")

    subjectId: str
    editorContent: str = ""


class QuestionPaperExportRequest(BaseModel):
    """Editor HTML plus the header fields, to be turned into a Word file without saving."""
    model_config = ConfigDict(extra="allow")

    editorContent: str = Field(default="", max_length=8_000_000)


class EquationFromTextRequest(BaseModel):
    """A description in words, and (for the one automatic retry) the attempt that did not draw."""
    description: str = Field(min_length=3, max_length=500)
    previousLatex: Optional[str] = Field(default=None, max_length=2000)
    previousError: Optional[str] = Field(default=None, max_length=300)


class QuestionPaperUpdateRequest(BaseModel):
    # Genuine partial update — every field optional, arbitrary extra
    # metadata fields allowed through to build_update_fields() unchanged.
    model_config = ConfigDict(extra="allow")
