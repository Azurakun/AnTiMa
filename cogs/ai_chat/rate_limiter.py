# cogs/ai_chat/rate_limiter.py
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# In-memory storage for rate limiting
# Structure: { "YYYY-MM-DD": { "guild_id": count } }
_daily_usage = {}

# Default fallback limit if none is configured for the server
DEFAULT_LIMIT = 50

def can_make_request(guild_id: str, max_limit: int = None) -> tuple[bool, int, int]:
    """
    Checks if the daily limit for a specific guild has been reached.
    
    Args:
        guild_id (str): The ID of the guild making the request.
        max_limit (int, optional): The custom limit for this guild. Defaults to 50.
        
    Returns:
        tuple[bool, int, int]: (is_allowed, current_count, active_limit)
    """
    global _daily_usage
    
    # Use timezone-aware UTC now
    today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    # Determine which limit to enforce
    limit = max_limit if max_limit is not None else DEFAULT_LIMIT
    
    # Reset/Initialize day bucket
    if today_utc not in _daily_usage:
        _daily_usage = {today_utc: {}}
        logger.info("New day detected. Resetting Gemini Pro quota counters.")
    
    # Initialize guild counter if missing
    if guild_id not in _daily_usage[today_utc]:
        _daily_usage[today_utc][guild_id] = 0
    
    current_count = _daily_usage[today_utc][guild_id]
    
    # Check limit
    if current_count >= limit:
        return False, current_count, limit
    
    # Increment usage
    _daily_usage[today_utc][guild_id] += 1
    return True, _daily_usage[today_utc][guild_id], limit