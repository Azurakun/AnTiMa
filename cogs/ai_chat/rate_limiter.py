# cogs/ai_chat/rate_limiter.py
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Simple in-memory storage for rate limiting
# Format: { "date_str": count }
_daily_usage = {}
DAILY_LIMIT = 100

def can_make_request() -> tuple[bool, int]:
    """
    Checks if the global daily limit for Gemini Pro has been reached.
    Returns (is_allowed, current_count).
    """
    global _daily_usage
    
    # Use timezone-aware UTC now to avoid DeprecationWarning
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    # Reset if it's a new day (primitive check, but works for simple blocking)
    if today_utc not in _daily_usage:
        _daily_usage = {today_utc: 0}
        logger.info("New day detected. Resetting Gemini Pro quota.")
    
    current_count = _daily_usage[today_utc]
    
    if current_count >= DAILY_LIMIT:
        return False, current_count
    
    # Increment usage
    _daily_usage[today_utc] += 1
    return True, _daily_usage[today_utc]