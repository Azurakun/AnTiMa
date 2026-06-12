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
        "main_model": "gemini-2.0-flash",
        "fast_model": "gemini-2.0-flash-lite",
        "max_retries": 0,
        "rate_limit": (12, 60.0),  # 12 requests per 60 seconds
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
        "rate_limit": (20, 60.0),  # 20 requests per 60 seconds
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


async def throttled_create(client_create_coro):
    """Wrap any client.chat.completions.create() call with the rate limiter."""
    await _rate_limiter.acquire()
    return await client_create_coro


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