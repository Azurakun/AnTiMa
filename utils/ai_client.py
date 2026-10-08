# utils/ai_client.py
"""
Centralized AI client with provider switching (Gemini / Ollama / Groq / OpenRouter / DeepSeek).

Controlled by the AI_PROVIDER env var:
  - "gemini"     → Google Gemini free tier (OpenAI-compatible endpoint)
  - "ollama"     → Local Ollama server (OpenAI-compatible endpoint)
  - "groq"       → Groq cloud (ultra-fast inference, free tier)
  - "openrouter" → OpenRouter (access to many free models)
  - "deepseek"   → DeepSeek API (very generous free tier)

All AI modules should import get_client, MAIN_MODEL, and FAST_MODEL from here.

Supports dynamic provider switching via reload_provider() without bot restart.
"""
import os
import asyncio
import time
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------
_PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
        "main_model": "gemini-2.5-flash",
        "fast_model": "gemini-2.5-flash-lite",
        "max_retries": 0,
        "rate_limit": (8, 60.0),  # 8 requests per 60 seconds (safety margin below 12)
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": None,  # Ollama doesn't need an API key
        "main_model": "qwen3:1.7b",
        "fast_model": "qwen3:1.7b",
        "max_retries": 0,
        "rate_limit": None,  # No rate limit for local
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "main_model": "llama-3.3-70b-versatile",
        "fast_model": "llama-3.1-8b-instant",
        "max_retries": 0,
        "rate_limit": (30, 60.0),  # 30 requests per 60 seconds (free tier)
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "main_model": "meta-llama/llama-3.3-70b-instruct:free",
        "fast_model": "meta-llama/llama-3.1-8b-instruct:free",
        "max_retries": 0,
        "rate_limit": (15, 60.0),  # 15 requests per 60 seconds (safety margin below 20)
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "main_model": "deepseek-chat",
        "fast_model": "deepseek-chat",
        "max_retries": 0,
        "rate_limit": None,  # Very generous free tier
    },
}

# ---------------------------------------------------------------------------
# Token-bucket rate limiter (only active for cloud providers)
# ---------------------------------------------------------------------------
class _RateLimiter:
    """Allows at most `max_calls` requests per `period` seconds."""
    def __init__(self, max_calls: int, period: float):
        self._max_calls = max_calls
        self._period = period
        self._calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self._period]
            if len(self._calls) >= self._max_calls:
                sleep_for = self._period - (now - self._calls[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                self._calls = [t for t in self._calls if time.monotonic() - t < self._period]
            self._calls.append(time.monotonic())


class _NoOpLimiter:
    """Pass-through limiter for local providers (no throttling)."""
    async def acquire(self):
        pass


class _GlobalCooldown:
    """Ensures minimum time between ANY two API calls."""
    def __init__(self, min_interval: float = 2.0):
        self._min_interval = min_interval
        self._last_call: float = 0
        self._lock = asyncio.Lock()

    async def wait(self):
        # Adaptive cooldown based on provider type to prevent sluggishness
        provider = AI_PROVIDER.lower() if AI_PROVIDER else "gemini"
        if provider in ("ollama", "groq", "deepseek"):
            interval = 0.1  # Fast/local: no cooldown needed
        else:
            interval = 1.5  # Gemini/OpenRouter free tiers: safety margin to avoid burst errors

        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            self._last_call = time.monotonic()


_global_cooldown = _GlobalCooldown()


async def throttled_create(create_fn, max_retries: int = 2):
    """
    Wrap any client.chat.completions.create() call with the rate limiter.

    Pass a CALLABLE (e.g. a lambda or functools.partial) that returns the
    coroutine when called, so each retry gets a fresh coroutine object.

    Example:
        await throttled_create(lambda: client.chat.completions.create(...))

    For backward-compat, if a pre-built coroutine is passed instead of a
    callable, it is awaited once with no retry.
    """
    import inspect
    # Legacy path: caller already evaluated the coroutine — no retry possible
    if inspect.iscoroutine(create_fn):
        await _rate_limiter.acquire()
        await _global_cooldown.wait()
        return await create_fn

    # Preferred path: callable factory — fresh coroutine per attempt
    await _rate_limiter.acquire()
    await _global_cooldown.wait()
    for attempt in range(max_retries + 1):
        try:
            return await create_fn()
        except Exception as e:
            error_str = str(e)
            if "429" in error_str and attempt < max_retries:
                wait_time = 2 ** (attempt + 1)  # 2s, 4s
                logger.warning(f"Rate limited (429). Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(wait_time)
                await _rate_limiter.acquire()
                await _global_cooldown.wait()
                continue
            raise



# ---------------------------------------------------------------------------
# Provider state (mutable, can be reloaded)
# ---------------------------------------------------------------------------
AI_PROVIDER: str = ""
MAIN_MODEL: str = ""
FAST_MODEL: str = ""
_config: dict = {}
_client: AsyncOpenAI | None = None
_rate_limiter: _RateLimiter | _NoOpLimiter = _NoOpLimiter()


def reload_provider(provider: str | None = None) -> str:
    """
    Reload AI provider configuration from environment variables.
    
    This allows switching providers WITHOUT restarting the bot.
    Re-reads AI_PROVIDER, AI_MAIN_MODEL, AI_FAST_MODEL from env.
    Clears cached client so next get_client() creates a new one.
    
    Args:
        provider: Optional provider name to switch to. If None, reads from env.
    
    Returns:
        The name of the active provider after reload.
    """
    global AI_PROVIDER, MAIN_MODEL, FAST_MODEL, _config, _client, _rate_limiter
    
    # Determine provider
    if provider:
        AI_PROVIDER = provider.lower()
    else:
        # Re-read from environment (useful if .env was reloaded)
        AI_PROVIDER = os.environ.get("AI_PROVIDER", "gemini").lower()
    
    if AI_PROVIDER not in _PROVIDERS:
        logger.warning(f"Unknown AI_PROVIDER '{AI_PROVIDER}', falling back to 'gemini'.")
        AI_PROVIDER = "gemini"
    
    _config = _PROVIDERS[AI_PROVIDER]
    
    # Re-read model overrides from env
    MAIN_MODEL = os.environ.get("AI_MAIN_MODEL", _config["main_model"])
    FAST_MODEL = os.environ.get("AI_FAST_MODEL", _config["fast_model"])
    
    # Reset rate limiter
    if _config["rate_limit"]:
        _rate_limiter = _RateLimiter(*_config["rate_limit"])
    else:
        _rate_limiter = _NoOpLimiter()
    
    # Clear cached client so it gets recreated with new config
    _client = None
    
    logger.info(f"AI Provider reloaded: {AI_PROVIDER.upper()} | Main: {MAIN_MODEL} | Fast: {FAST_MODEL}")
    return AI_PROVIDER


# Initialize on first import
reload_provider()


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

def get_client() -> AsyncOpenAI:
    """
    Returns the shared async OpenAI-compatible client for the active provider.
    
    If the client was cleared (e.g., via reload_provider), creates a new one.
    """
    global _client
    if _client is None:
        env_key = _config["env_key"]
        if env_key:
            api_key = os.environ.get(env_key)
            if not api_key:
                raise EnvironmentError(f"{env_key} is not set in environment variables.")
        else:
            api_key = "ollama"  # Ollama accepts any non-empty string

        _client = AsyncOpenAI(
            api_key=api_key,
            base_url=_config["base_url"],
            max_retries=_config["max_retries"],
        )
        logger.info(f"AI Client created: {AI_PROVIDER.upper()} | Main: {MAIN_MODEL} | Fast: {FAST_MODEL}")
    return _client


# ---------------------------------------------------------------------------
# Embedding Helper
# ---------------------------------------------------------------------------
# Every stored vector records the model that produced it ("embed_model"), because
# vectors from different models are not comparable even when their lengths match.
# See utils/vector_search.py for how mixed-model memories are searched.

FALLBACK_EMBED_DIM = 256
LOCAL_EMBED_MODEL = "local-ngram-256"
GEMINI_EMBED_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
EMBED_INPUT_CHARS = 2000

# Models used by older versions of the bot, recognised by vector length for documents
# saved before "embed_model" was recorded.
_LEGACY_MODELS_BY_DIM = {
    256: LOCAL_EMBED_MODEL,
    768: "text-embedding-004",       # Shut down by Google in Jan 2026
    1536: "text-embedding-3-small",
}

_embed_clients: dict[str, AsyncOpenAI] = {}


def fallback_embed_text_sync(text: str) -> list[float]:
    """Pure-Python n-gram hash embedding fallback."""
    import hashlib, math
    text = text.lower()[:EMBED_INPUT_CHARS]
    vec = [0.0] * FALLBACK_EMBED_DIM
    for i in range(len(text) - 2):
        gram = text[i:i+3]
        h = int(hashlib.md5(gram.encode()).hexdigest(), 16) % FALLBACK_EMBED_DIM
        vec[h] += 1.0
    magnitude = math.sqrt(sum(x * x for x in vec))
    if magnitude > 0:
        vec = [x / magnitude for x in vec]
    return vec


def infer_embed_model(doc: dict, vector_key: str = "vector") -> str | None:
    """Returns the model that produced a stored vector (explicit, or guessed for legacy docs)."""
    if doc.get("embed_model"):
        return doc["embed_model"]
    vec = doc.get(vector_key)
    return _LEGACY_MODELS_BY_DIM.get(len(vec)) if vec else None


def _usable_key(name: str) -> str | None:
    key = os.environ.get(name)
    return key if key and not key.startswith("YOUR_") else None


def _embed_backends() -> list[tuple[str, str, str | None]]:
    """(model, api_key, base_url) for each API embedding backend, in preference order."""
    backends = []
    gemini_key = _usable_key("GEMINI_API_KEY")
    if gemini_key:
        backends.append((os.environ.get("EMBED_MODEL", "gemini-embedding-001"), gemini_key, GEMINI_EMBED_BASE_URL))
    openai_key = _usable_key("OPENAI_API_KEY")
    if openai_key:
        backends.append(("text-embedding-3-small", openai_key, None))
    return backends


async def _embed_with_backend(texts: list[str], model: str, api_key: str, base_url: str | None) -> list[list[float]]:
    cache_key = f"{base_url}|{api_key}"
    client = _embed_clients.get(cache_key)
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=1)
        _embed_clients[cache_key] = client
    response = await client.embeddings.create(model=model, input=[t[:EMBED_INPUT_CHARS] for t in texts])
    vectors = [d.embedding for d in sorted(response.data, key=lambda d: d.index)]
    if len(vectors) != len(texts):
        raise ValueError(f"{model} returned {len(vectors)} embeddings for {len(texts)} inputs")
    return vectors


async def embed_texts(texts: list[str], model: str | None = None) -> tuple[list[list[float]], str]:
    """
    Embeds a batch of texts. Returns (vectors, model_name).

    With model=None, uses the first API backend that works and falls back to the
    local n-gram embedding. With a model name, uses exactly that model and raises
    if it is unavailable (used to re-embed old memories into the current space).
    """
    texts = [t if t and t.strip() else " " for t in texts]
    if model == LOCAL_EMBED_MODEL:
        return [await asyncio.to_thread(fallback_embed_text_sync, t) for t in texts], LOCAL_EMBED_MODEL

    for backend_model, api_key, base_url in _embed_backends():
        if model and backend_model != model:
            continue
        try:
            return await _embed_with_backend(texts, backend_model, api_key, base_url), backend_model
        except Exception as e:
            logger.warning(f"[Embedding] {backend_model} failed: {e}")
            if model:
                raise

    if model:
        raise RuntimeError(f"Embedding model {model} is not available")
    return [await asyncio.to_thread(fallback_embed_text_sync, t) for t in texts], LOCAL_EMBED_MODEL


async def embed(text: str) -> tuple[list[float], str]:
    """Embeds one text. Returns (vector, model_name)."""
    vectors, model = await embed_texts([text])
    return vectors[0], model


async def get_embedding(text: str) -> list[float]:
    """Backwards-compatible wrapper: returns only the vector. Prefer embed() so the model can be stored."""
    vector, _ = await embed(text)
    return vector
