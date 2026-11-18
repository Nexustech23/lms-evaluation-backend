from typing import List, Optional

from pydantic import BaseModel


class SchoolGradeBand(BaseModel):
    # Only the shape is checked here; the rules (order, overlap, the lowest grade starting at 0) live in
    # app.utils.school_grading.validate_config, which the router calls.
    grade: str = ""
    min_percentage: Optional[float] = None
    grade_point: Optional[float] = None


class SchoolGradingRequest(BaseModel):
    pass_percentage: float = 33
    bands: List[SchoolGradeBand] = []
