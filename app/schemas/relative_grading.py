from pydantic import BaseModel

from app.models.relative_grading import PERCENTAGE_FIELDS


class _PercentageFieldsModel(BaseModel):
    a_plus_percentage: float = 0
    a_percentage: float = 0
    a_minus_percentage: float = 0
    b_plus_percentage: float = 0
    b_percentage: float = 0
    b_minus_percentage: float = 0
    c_plus_percentage: float = 0
    c_percentage: float = 0
    c_minus_percentage: float = 0
    d_percentage: float = 0
    u_percentage: float = 0


assert set(_PercentageFieldsModel.model_fields) == set(PERCENTAGE_FIELDS), (
    "RelativeGradingRequest fields drifted from app.models.relative_grading.PERCENTAGE_FIELDS"
)


class RelativeGradingRequest(_PercentageFieldsModel):
    # The sum-to-100 business rule stays in
    # app.models.relative_grading.validate_percentage_total, called
    # unchanged by the router — this schema only guarantees each field is
    # actually numeric before that check runs.
    pass
