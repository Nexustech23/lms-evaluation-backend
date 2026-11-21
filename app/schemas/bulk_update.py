from pydantic import BaseModel, Field


class BulkUpdateApplyRequest(BaseModel):
    # The rules (a reason of 3 to 500 characters) live in app.services.bulk_update_apply, which the router calls;
    # this only keeps an absurdly large body out.
    reason: str = Field(default="", max_length=2000)
    # Rows that had problems in the preview are applied-around only when the admin says so.
    skip_rejected: bool = False
