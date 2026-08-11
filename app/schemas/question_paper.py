from typing import Any, Dict

from pydantic import BaseModel, ConfigDict


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


class QuestionPaperUpdateRequest(BaseModel):
    # Genuine partial update — every field optional, arbitrary extra
    # metadata fields allowed through to build_update_fields() unchanged.
    model_config = ConfigDict(extra="allow")
