# ============================================================
# Classic chunk + embed + similarity path, used ONLY for unstructured, long
# documents (textbooks, reference PDFs). Structured course profiles never
# touch this file — they go through tree_index.py instead.
#
# Ported from the Flask prototype's rag/indexing/vector_store.py +
# rag/generation/llm_client.py's GeminiEmbedder. Reuses this backend's
# existing Gemini singleton client (app.services.gemini) instead of
# constructing a second one, and the Qdrant client itself is synchronous
# (qdrant-client has no async variant in general use here) — callers from
# async routes must run VectorStore methods via asyncio.to_thread, same
# convention already used for app.services.claude/gemini's blocking calls.
# ============================================================
from __future__ import annotations

import re
import time
from typing import List

from app.services.rag.schemas import Chunk, new_id

COLLECTION = "lms_course_material_chunks"
CHUNK_SIZE_TOKENS = 450       # ~500 tokens with overlap headroom
CHUNK_OVERLAP_TOKENS = 60

EMBED_DIM = 768
_EMBED_BATCH_SIZE = 100
_EMBED_MAX_RETRIES = 5
_EMBED_MODEL = "gemini-embedding-001"


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Blocking call — run via asyncio.to_thread() from async callers.

    Model choice (matches the Flask prototype's verified findings for this
    project's API key): `text-embedding-004` is not enabled (404), but
    `gemini-embedding-001` is. Its native output is 3072-dim;
    `output_dimensionality` is pinned to 768 to keep the Qdrant collection
    size predictable.

    The free tier's ~100 req/min cap on embed_content is consumed per
    embedded item, not per API call, so batches are capped and retried on
    429 honoring the server's retryDelay hint when present.
    """
    from app.services.gemini import _get_client

    vectors: List[List[float]] = []
    client = _get_client()
    for i in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = texts[i:i + _EMBED_BATCH_SIZE]
        vectors.extend(_embed_batch_with_retry(client, batch))
    return vectors


def _embed_batch_with_retry(client, batch: List[str]) -> List[List[float]]:
    from google.genai.errors import ClientError

    for attempt in range(_EMBED_MAX_RETRIES):
        try:
            resp = client.models.embed_content(
                model=_EMBED_MODEL,
                contents=batch,
                config={"output_dimensionality": EMBED_DIM},
            )
            return [e.values for e in resp.embeddings]
        except ClientError as exc:
            if exc.code != 429 or attempt == _EMBED_MAX_RETRIES - 1:
                raise
            time.sleep(_retry_delay_seconds(exc, attempt))
    raise AssertionError("unreachable")  # loop above always returns or raises


def _retry_delay_seconds(exc, attempt: int) -> float:
    try:
        for detail in exc.details.get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                return float(detail["retryDelay"].rstrip("s")) + 1
    except (AttributeError, KeyError, ValueError, TypeError):
        pass
    return 2 ** attempt  # fallback exponential backoff: 1s, 2s, 4s, 8s


def chunk_text(doc_id: str, pages: List[dict]) -> List[Chunk]:
    """
    Sentence-aware sliding window, not a blind character cut. Prevents the
    classic RAG failure of cutting a sentence in half at the chunk boundary.
    """
    sentences = []
    for page in pages:
        for sent in re.split(r"(?<=[.!?])\s+", page["text"]):
            if sent.strip():
                sentences.append((page["page_num"], sent.strip()))

    chunks: List[Chunk] = []
    buf: List[str] = []
    buf_pages: List[int] = []
    approx_tokens = 0

    def flush():
        if not buf:
            return
        chunks.append(
            Chunk(
                id=new_id(),
                doc_id=doc_id,
                text=" ".join(buf),
                page_start=buf_pages[0],
                page_end=buf_pages[-1],
            )
        )

    for page_num, sent in sentences:
        sent_tokens = max(len(sent.split()) * 1.3, 1)  # rough token estimate
        if approx_tokens + sent_tokens > CHUNK_SIZE_TOKENS and buf:
            flush()
            # overlap: keep tail of previous buffer for continuity
            overlap_words = " ".join(buf).split()[-CHUNK_OVERLAP_TOKENS:]
            buf = [" ".join(overlap_words)]
            buf_pages = [buf_pages[-1]]
            approx_tokens = len(overlap_words)
        buf.append(sent)
        buf_pages.append(page_num)
        approx_tokens += sent_tokens
    flush()
    return chunks


class VectorStore:
    """Thin Qdrant wrapper. All methods are blocking — call via asyncio.to_thread."""

    def __init__(self, url: str = "http://localhost:6333"):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self.client = QdrantClient(url=url)
        try:
            self.client.get_collection(COLLECTION)
        except Exception:
            self.client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )

    def upsert(self, chunks: List[Chunk]) -> None:
        from qdrant_client.models import PointStruct

        vectors = embed_texts([c.text for c in chunks])
        points = [
            PointStruct(
                id=c.id,
                vector=vec,
                payload={"doc_id": c.doc_id, "text": c.text,
                         "page_start": c.page_start, "page_end": c.page_end},
            )
            for c, vec in zip(chunks, vectors)
        ]
        self.client.upsert(collection_name=COLLECTION, points=points)

    def has_chunks(self, doc_id: str) -> bool:
        """Cheap existence check — no embedding call needed, unlike search()."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        result = self.client.count(
            collection_name=COLLECTION,
            count_filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        )
        return result.count > 0

    def delete_doc(self, doc_id: str) -> None:
        """Drop every chunk previously indexed for doc_id — call before
        re-upserting on re-upload, otherwise stale chunks from the old
        version keep surfacing alongside the new ones."""
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        self.client.delete(
            collection_name=COLLECTION,
            points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        )

    def search(self, query: str, doc_id: str, top_k: int = 4):
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        vec = embed_texts([query])[0]
        # `.search()` was removed in qdrant-client >=1.10 in favor of `.query_points()`,
        # which wraps results in a QueryResponse (`.points`) instead of returning a bare list.
        response = self.client.query_points(
            collection_name=COLLECTION,
            query=vec,
            query_filter=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
            limit=top_k,
        )
        return response.points
