import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings
from app.core.workflow_diagnostics import dependency_health

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def connect_to_mongo() -> None:
    global _client, _db
    _client = AsyncIOMotorClient(
        settings.MONGODB_URI,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=20000,
        maxPoolSize=settings.MONGO_MAX_POOL_SIZE,
        retryWrites=True,   # idempotent single-doc write retry on transient errors
        retryReads=True,
    )
    _db = _client[settings.DB_NAME]
    dependency_health.observe('mongodb','client_created',source='lifecycle')
    logging.info("MongoDB client created for database '%s'", settings.DB_NAME)


async def ping_mongo() -> None:
    if _client is None:
        raise RuntimeError("Mongo client not initialized — call connect_to_mongo() first")
    try:
        await _client.admin.command("ping")
    except Exception as exc:
        dependency_health.observe('mongodb','unavailable',error_type=type(exc).__name__,source='startup_ping')
        raise
    dependency_health.observe('mongodb','connected',source='startup_ping')
    logging.info("MongoDB connection verified")


def close_mongo_connection() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None
    dependency_health.observe('mongodb','closed',source='lifecycle')


def get_database() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Mongo client not initialized — call connect_to_mongo() first")
    return _db
