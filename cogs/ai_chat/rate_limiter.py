
import json
import os
import logging
from datetime import datetime
import threading

logger = logging.getLogger(__name__)

DAILY_LIMIT = 50  # Your 50 RPD limit
QUOTA_FILE = 'gemini_pro_quota.json'

# Use a lock to prevent a race condition if two messages
# are processed at the exact same time.
_lock = threading.Lock()

def get_quota_data():
    """Reads the quota data from the file."""
    if not os.path.exists(QUOTA_FILE):
        return {'request_count': 0, 'last_reset_date': ''}
    try:
        with open(QUOTA_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        # Handle empty or corrupted file
        return {'request_count': 0, 'last_reset_date': ''}

def save_quota_data(data):
    """Saves the quota data to the file."""
    try:
        with open(QUOTA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        logger.error(f"Failed to save quota data: {e}")

def can_make_request():
    """
    Checks if an API request can be made based on the 50 RPD limit.
    This function is thread-safe.

    Returns:
        tuple[bool, int]: (is_allowed, current_count)
    """
    # Acquire the lock to ensure only one process
    # can check/update the file at a time.
    with _lock:
        data = get_quota_data()
        # Use UTC date as API quotas almost always reset in UTC
        today_utc = datetime.utcnow().strftime('%Y-%m-%d')
        
        last_reset_date = data.get('last_reset_date')
        request_count = data.get('request_count', 0)
        
        if last_reset_date != today_utc:
            # It's a new day, reset the counter
            logger.info("New day detected. Resetting Gemini Pro quota.")
            data['request_count'] = 1
            data['last_reset_date'] = today_utc
            save_quota_data(data)
            return True, 1
        else:
            # It's the same day, check the count
            if request_count < DAILY_LIMIT:
                data['request_count'] += 1
                save_quota_data(data)
                return True, data['request_count']
            else:
                # Limit reached
                logger.warning(f"Gemini Pro 50 RPD limit reached. Count: {request_count}")
                return False, request_count