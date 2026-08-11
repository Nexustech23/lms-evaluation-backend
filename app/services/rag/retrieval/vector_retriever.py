# ============================================================
# Ported from the Flask prototype's rag/retrieval/vector_retriever.py.
# VectorStore.search() is blocking (Qdrant client + Gemini embedding call),
# run via asyncio.to_thread.
# ============================================================
from __future__ import annotations

import asyncio
import logging

from app.services.rag.schemas import RetrievalResult
from app.services.rag.vector_store import VectorStore

logger = logging.getLogger("rag.vector_retriever")


async def retrieve(query: str, doc_id: str, store: VectorStore, top_k: int = 4) -> RetrievalResult:
    hits = await asyncio.to_thread(store.search, query, doc_id, top_k)
    if not hits:
        logger.warning("vector_retriever: doc_id=%s returned zero hits from Qdrant", doc_id)
        return RetrievalResult(context_text="", source_nodes=[], confidence=0.0, doc_id=doc_id)

    context = "\n\n---\n\n".join(h.payload["text"] for h in hits)
    # qdrant cosine score ~ [0,1] already for normalized vectors; treat top
    # hit's score as a proxy confidence signal for the fallback decision.
    confidence = float(hits[0].score)
    logger.info("vector_retriever: doc_id=%s hits=%d top_score=%.3f", doc_id, len(hits), confidence)
    return RetrievalResult(
        context_text=context,
        source_nodes=[str(h.id) for h in hits],
        confidence=confidence,
        doc_id=doc_id,
    )
