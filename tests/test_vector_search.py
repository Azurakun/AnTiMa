import asyncio

from utils import ai_client, vector_search
from utils.ai_client import LOCAL_EMBED_MODEL, infer_embed_model


def run(coro):
    return asyncio.run(coro)


def test_infer_embed_model_legacy_dims():
    assert infer_embed_model({"vector": [0.0] * 256}) == LOCAL_EMBED_MODEL
    assert infer_embed_model({"vector": [0.0] * 768}) == "text-embedding-004"
    assert infer_embed_model({"vector": [0.0] * 3072, "embed_model": "gemini-embedding-001"}) == "gemini-embedding-001"
    assert infer_embed_model({}) is None


def test_local_fallback_when_no_api_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    vector, model = run(ai_client.embed("the dragon guards the gate"))
    assert model == LOCAL_EMBED_MODEL
    assert len(vector) == ai_client.FALLBACK_EMBED_DIM


def test_mixed_models_are_reembedded_and_saved(monkeypatch):
    """Old memories from another model must still be found, then migrated."""
    calls = []

    async def fake_embed(text):
        return [1.0, 0.0, 0.0], "model-new"

    async def fake_embed_texts(texts, model=None):
        calls.append((list(texts), model))
        return [[1.0, 0.0, 0.0] if "dragon" in t else [0.0, 1.0, 0.0] for t in texts], model

    saved = []
    monkeypatch.setattr(vector_search, "embed", fake_embed)
    monkeypatch.setattr(vector_search, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(vector_search, "_save_vectors", lambda coll, docs, vecs, model, key: saved.append((docs, model)))

    docs = [
        {"_id": 1, "text": "a dragon sleeps", "vector": [0.5] * 256},                                  # legacy local
        {"_id": 2, "text": "the tavern", "vector": [0.5] * 768},                                       # legacy 004
        {"_id": 3, "text": "dragon fire", "vector": [1.0, 0.0, 0.0], "embed_model": "model-new"},      # current
    ]
    ranked = run(vector_search.rank_documents("dragon", docs, text_key="text", collection=object(), threshold=0.5))

    assert {d["_id"] for _, d in ranked} == {1, 3}
    assert calls == [(["a dragon sleeps", "the tavern"], "model-new")]
    assert [d["_id"] for d in saved[0][0]] == [1, 2] and saved[0][1] == "model-new"


def test_local_query_compares_mismatched_by_text(monkeypatch):
    async def fake_embed(text):
        return ai_client.fallback_embed_text_sync(text), LOCAL_EMBED_MODEL

    monkeypatch.setattr(vector_search, "embed", fake_embed)
    docs = [{"_id": 1, "text": "the ancient dragon of the north", "vector": [0.1] * 3072, "embed_model": "gemini-embedding-001"}]
    ranked = run(vector_search.rank_documents("the ancient dragon of the north", docs, text_key="text", threshold=0.9))
    assert len(ranked) == 1 and ranked[0][0] > 0.99


def test_cosine_similarity_handles_mismatch():
    assert vector_search.cosine_similarity([1, 0], [1, 0, 0]) == 0.0
    assert vector_search.cosine_similarity([], [1]) == 0.0
    assert round(vector_search.cosine_similarity([1, 1], [1, 1]), 6) == 1.0
