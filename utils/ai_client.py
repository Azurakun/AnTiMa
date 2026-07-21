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
FALLBACK_EMBED_DIM = 256

def fallback_embed_text_sync(text: str) -> list[float]:
    """Pure-Python n-gram hash embedding fallback."""
    import hashlib, math
    text = text.lower()[:2000]
    vec = [0.0] * FALLBACK_EMBED_DIM
    for i in range(len(text) - 2):
        gram = text[i:i+3]
        h = int(hashlib.md5(gram.encode()).hexdigest(), 16) % FALLBACK_EMBED_DIM
        vec[h] += 1.0
    magnitude = math.sqrt(sum(x * x for x in vec))
    if magnitude > 0:
        vec = [x / magnitude for x in vec]
    return vec


async def get_embedding(text: str) -> list[float]:
    """
    Retrieves vector embedding for given text using Gemini / OpenAI API,
    falling back to local n-gram hash vector if API calls fail or offline.
    """
    if not text or not text.strip():
        return [0.0] * FALLBACK_EMBED_DIM

    try:
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if gemini_key and not gemini_key.startswith("YOUR_"):
            embed_client = AsyncOpenAI(
                api_key=gemini_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                max_retries=1
            )
            response = await embed_client.embeddings.create(
                model="text-embedding-004",
                input=text[:2000]
            )
            if response and response.data:
                return response.data[0].embedding

        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key and not openai_key.startswith("YOUR_"):
            embed_client = AsyncOpenAI(api_key=openai_key, max_retries=1)
            response = await embed_client.embeddings.create(
                model="text-embedding-3-small",
                input=text[:2000]
            )
            if response and response.data:
                return response.data[0].embedding

        client = get_client()
        response = await client.embeddings.create(
            model="text-embedding-004",
            input=text[:2000]
        )
        if response and response.data:
            return response.data[0].embedding

    except Exception as e:
        logger.debug(f"[Embedding Fallback] {e}")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fallback_embed_text_sync, text)