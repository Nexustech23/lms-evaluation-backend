# ============================================================
# MongoDB index creation, run once on app startup (app/main.py lifespan).
#
# Nothing in this codebase created indexes before this file except
# app/services/ai_usage.ensure_ai_usage_indexes — so every other query
# (users by email, the whole institute hierarchy by institute_id / parent
# FK, answerDetails by exam_id, subjectDetails by faculty_id, roadmaps by
# user_id, ...) was a full collection scan.
#
# All indexes here are NON-unique on purpose: create_index is idempotent
# (same spec = no-op) and can't fail on existing data the way a unique
# index over accidental duplicates would. Making users.email unique is a
# good follow-up once a dedupe pass confirms it's safe.
#
# Each create is isolated in try/except so one failure (e.g. an index that
# conflicts with a differently-named legacy index of the same shape) never
# blocks startup.
# ============================================================
import logging

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("db.indexes")

# collection -> list of key specs (each spec is a list of (field, direction))
_INDEXES: dict[str, list[list[tuple[str, int]]]] = {
    "users": [
        [("email", 1)],
        [("is_deleted", 1), ("role", 1)],
    ],
    "studentDetails": [
        [("user_id", 1)],
        [("institute_id", 1), ("role", 1)],
        [("roll_no", 1), ("programme_id", 1)],
        [("tutor_id", 1), ("role", 1)],
    ],
    "facultyDetails": [
        [("user_id", 1)],
        [("institute_id", 1), ("is_deleted", 1)],
    ],
    "instituteDetails": [
        [("user_id", 1)],
    ],
    "tutorDetails": [
        [("user_id", 1)],
    ],
    "schoolDetails": [
        [("institute_id", 1)],
    ],
    "programmeDetails": [
        [("institute_id", 1)],
        [("school_id", 1)],
    ],
    "departmentDetails": [
        [("institute_id", 1)],
        [("programme_id", 1)],
    ],
    "batchDetails": [
        [("institute_id", 1)],
        [("programme_id", 1)],
        [("department_id", 1)],
    ],
    "subjectDetails": [
        [("institute_id", 1), ("is_deleted", 1)],
        [("faculty_id", 1), ("is_deleted", 1)],
        [("programme_id", 1), ("is_deleted", 1)],
        [("batch_id", 1)],
        [("school_id", 1)],
    ],
    "newsavedDocs": [
        [("subject_id", 1)],
        [("faculty_id", 1)],
        [("batch_id", 1)],
        [("school_id", 1)],
    ],
    "answerDetails": [
        [("exam_id", 1)],
    ],
    "evaluationDetails": [
        [("exam_id", 1)],
        [("exam_id", 1), ("faculty_id", 1)],
    ],
    "questionPaperDetails": [
        [("faculty_id", 1), ("is_deleted", 1)],
        [("subject_id", 1)],
        [("school_id", 1)],
    ],
    "relativeGradings": [
        [("university_id", 1)],
    ],
    "importedMarks": [
        [("institute_id", 1), ("batch_id", 1), ("semester", 1)],
    ],
    "academic_transcripts": [
        [("import_id", 1)],
        [("institute_id", 1), ("batch_id", 1)],
        [("student_id", 1)],
    ],
    "transcriptImports": [
        [("import_id", 1)],
        [("institute_id", 1)],
    ],
    "selfLearnerRoadmaps": [
        [("user_id", 1), ("created_at", -1)],
        [("user_id", 1), ("active", 1)],
    ],
    "mockTests": [
        [("student_id", 1), ("created_at", -1)],
    ],
    "testAttempts": [
        [("student_id", 1)],
        [("test_id", 1), ("student_id", 1)],
    ],
    "facultyMaterials": [
        [("faculty_id", 1), ("created_at", -1)],
        [("subject_id", 1), ("is_published", 1)],
    ],
    "studentMaterialInteractions": [
        [("material_id", 1), ("student_id", 1)],
    ],
    "StudentSubjectRelationModel": [
        [("user_id", 1)],
        [("subject_id", 1)],
    ],
    "contacts": [
        [("read", 1), ("created_at", -1)],
    ],
    "courseMaterials": [
        [("content_hash", 1)],
        [("course_title", 1)],
        [("course_code", 1)],
    ],
}


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    created = 0
    for collection, specs in _INDEXES.items():
        for spec in specs:
            try:
                # MongoDB 4.2+ builds indexes with an optimized, mostly
                # non-blocking build; no need for the deprecated background=True.
                await db[collection].create_index(spec)
                created += 1
            except Exception as exc:  # noqa: BLE001 - one bad index must not block startup
                logger.warning("ensure_indexes: %s %s failed: %s", collection, spec, exc)
    logger.info("ensure_indexes: %d indexes ensured across %d collections", created, len(_INDEXES))
