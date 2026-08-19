# ============================================================
# CONTACT ROUTER
# Ported from routes/contact_routes.py + controllers/contact_controller.py
# ============================================================

from datetime import datetime, timezone
from typing import Any, Dict

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import SUPERADMIN, require_role
from app.core.rate_limit import contact_rate_limit
from app.db.mongodb import get_database
from app.models.contact import create_contact_document, serialize_contact
from app.schemas.contact import ContactCreate

router = APIRouter(tags=["contact"])


@router.post("/contact", dependencies=[Depends(contact_rate_limit)])
async def create_contact(
    payload: ContactCreate,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    try:
        contact_doc = create_contact_document(payload.model_dump())
        result = await db["contacts"].insert_one(contact_doc)
        created_doc = await db["contacts"].find_one({"_id": result.inserted_id})

        return {
            "success": True,
            "message": "Contact query submitted successfully",
            "data": serialize_contact(created_doc),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/contact-queries", dependencies=[Depends(require_role(SUPERADMIN))])
async def get_all_contacts(
    page: int = Query(1),
    limit: int = Query(10),
    status: str = Query("all"),
    topic: str = Query("all"),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    skip = (page - 1) * limit
    query: Dict[str, Any] = {}

    if status == "new":
        query["read"] = False
    elif status == "read":
        query["read"] = True

    if topic != "all":
        query["topic"] = topic

    total = await db["contacts"].count_documents(query)
    unread_count = await db["contacts"].count_documents({"read": False})

    cursor = db["contacts"].find(query).sort("created_at", -1).skip(skip).limit(limit)
    contacts = [serialize_contact(c) async for c in cursor]

    return {
        "success": True,
        "page": page,
        "limit": limit,
        "total": total,
        "count": len(contacts),
        "unread_count": unread_count,
        "filters": {"status": status, "topic": topic},
        "data": contacts,
    }


@router.get("/admin/contact-queries/{contact_id}", dependencies=[Depends(require_role(SUPERADMIN))])
async def get_single_contact(contact_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(status_code=400, detail="Invalid contact id")

    contact = await db["contacts"].find_one({"_id": ObjectId(contact_id)})
    if not contact:
        raise HTTPException(status_code=404, detail="Contact query not found")

    return {"success": True, "data": serialize_contact(contact)}


@router.patch("/admin/contact-queries/{contact_id}/read", dependencies=[Depends(require_role(SUPERADMIN))])
async def mark_contact_read(contact_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(status_code=400, detail="Invalid contact id")

    existing = await db["contacts"].find_one({"_id": ObjectId(contact_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Contact query not found")

    await db["contacts"].update_one(
        {"_id": ObjectId(contact_id)},
        {"$set": {"read": True, "updated_at": datetime.now(timezone.utc)}},
    )
    updated = await db["contacts"].find_one({"_id": ObjectId(contact_id)})

    return {"success": True, "message": "Marked as read", "data": serialize_contact(updated)}


@router.delete("/admin/contact-queries/{contact_id}", dependencies=[Depends(require_role(SUPERADMIN))])
async def delete_contact(contact_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(status_code=400, detail="Invalid contact id")

    existing = await db["contacts"].find_one({"_id": ObjectId(contact_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Contact query not found")

    await db["contacts"].delete_one({"_id": ObjectId(contact_id)})

    return {"success": True, "message": "Contact query deleted successfully"}
