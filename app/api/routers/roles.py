# ============================================================
# ROLES ROUTER
# Ported from routes/role_routes.py + controllers/institute/role_controller.py
#
# Deviation from Flask (agreed security fix): the original /create_role had
# NO auth check at all. Here it requires an authenticated superadmin.
# ============================================================

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import SUPERADMIN, require_role
from app.db.mongodb import get_database
from app.models.role import create_role_document, serialize_role
from app.schemas.role import RoleCreate

router = APIRouter(tags=["roles"])


@router.post("/create_role")
async def create_role(
    payload: RoleCreate,
    _identity: dict = Depends(require_role(SUPERADMIN)),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    try:
        role_doc = create_role_document(payload.model_dump())

        existing = await db["roles"].find_one({"name": role_doc["name"]})
        if existing:
            raise HTTPException(status_code=400, detail="Role already exists")

        result = await db["roles"].insert_one(role_doc)
        role_doc["_id"] = result.inserted_id

        return {"message": "Role created successfully", "role": serialize_role(role_doc)}

    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")
