# utils/vector_search.py
"""
Similarity search over memories stored with mixed embedding models.

Vectors from different models (or the local n-gram fallback) cannot be compared.
Previously such memories silently scored 0 and were never recalled. Now:

- Memories embedded with the same model as the query are compared directly.
- Memories from another model are re-embedded with the query's model, and the new
  vector is saved, so old data migrates as it gets searched.
- If the query itself had to use the local fallback (API down), mismatched
  memories are compared using a local embedding of their text (not saved).
"""
import asyncio
import logging
import math

from utils.ai_client import LOCAL_EMBED_MODEL, embed, embed_texts, fallback_embed_text_sync, infer_embed_model

logger = logging.getLogger(__name__)

# Cap on how many old memories are re-embedded per search, to bound latency and API usage.
MAX_REEMBED_PER_SEARCH = 50


def cosine_similarity(v1, v2) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    magnitude1 = math.sqrt(sum(a * a for a in v1))
    magnitude2 = math.sqrt(sum(b * b for b in v2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)


async def rank_documents(query_text: str, docs: list[dict], text_key: str, collection=None,
                         vector_key: str = "vector", threshold: float = 0.0, limit: int = 5) -> list[tuple[float, dict]]:
    """
    Scores docs against query_text and returns the top (score, doc) pairs above threshold.
    If collection is given, re-embedded vectors are written back to it (matched by _id).
    """
    if not query_text or not docs:
        return []

    query_vector, query_model = await embed(query_text)

    matched: list[dict] = []
    mismatched: list[dict] = []
    for doc in docs:
        if not doc.get(text_key):
            continue
        if doc.get(vector_key) and infer_embed_model(doc, vector_key) == query_model:
            matched.append(doc)
        else:
            mismatched.append(doc)

    # Pure-Python math over many vectors: keep it off the event loop.
    scored: list[tuple[float, dict]] = await asyncio.to_thread(
        lambda: [(cosine_similarity(query_vector, d[vector_key]), d) for d in matched]
    )

    if mismatched:
        if query_model == LOCAL_EMBED_MODEL:
            for doc in mismatched:
                vec = await asyncio.to_thread(fallback_embed_text_sync, doc[text_key])
                scored.append((cosine_similarity(query_vector, vec), doc))
        else:
            batch = mismatched[:MAX_REEMBED_PER_SEARCH]
            try:
                vectors, _ = await embed_texts([d[text_key] for d in batch], model=query_model)
            except Exception as e:
                logger.warning(f"[VectorSearch] Re-embedding {len(batch)} memories failed: {e}")
                vectors = None
            if vectors:
                for doc, vec in zip(batch, vectors):
                    scored.append((cosine_similarity(query_vector, vec), doc))
                if collection is not None:
                    await asyncio.to_thread(_save_vectors, collection, batch, vectors, query_model, vector_key)

    scored = [pair for pair in scored if pair[0] >= threshold]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:limit]


def _save_vectors(collection, docs, vectors, model, vector_key):
    from pymongo import UpdateOne
    ops = [UpdateOne({"_id": d["_id"]}, {"$set": {vector_key: v, "embed_model": model}})
           for d, v in zip(docs, vectors) if "_id" in d]
    if ops:
        try:
            collection.bulk_write(ops, ordered=False)
        except Exception as e:
            logger.warning(f"[VectorSearch] Saving re-embedded vectors failed: {e}")
