# utils/timezone_manager.py
from datetime import datetime
from zoneinfo import ZoneInfo
from utils.db import user_timezones_collection

DEFAULT_TIMEZONE = "Asia/Jakarta"  # GMT+7

def get_user_timezone(user_id: int) -> str:
    """
    Fetches the user's preferred timezone from the DB.
    Defaults to GMT+7 (Asia/Jakarta) if not found.
    """
    data = user_timezones_collection.find_one({"user_id": str(user_id)})
    if data:
        return data.get("timezone", DEFAULT_TIMEZONE)
    return DEFAULT_TIMEZONE

def set_user_timezone(user_id: int, timezone_str: str) -> bool:
    """
    Sets the user's timezone. Returns True if successful.
    """
    try:
        # Validate timezone
        ZoneInfo(timezone_str) 
        user_timezones_collection.update_one(
            {"user_id": str(user_id)},
            {"$set": {"timezone": timezone_str}},
            upsert=True
        )
        return True
    except Exception:
        return False

def get_local_time(user_id: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """
    Returns the current time formatted string in the user's timezone.
    """
    tz_str = get_user_timezone(user_id)
    try:
        tz = ZoneInfo(tz_str)
    except:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        
    local_dt = datetime.now(tz)
    return local_dt.strftime(fmt)

def get_local_datetime(user_id: int) -> datetime:
    """Returns the raw datetime object in the user's timezone."""
    tz_str = get_user_timezone(user_id)
    try:
        tz = ZoneInfo(tz_str)
    except:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tz)