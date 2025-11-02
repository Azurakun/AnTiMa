# test_limiter.py
import time
from datetime import datetime, timedelta
import os
import json

# We are importing the function directly from the file you created
from cogs.ai_chat.rate_limiter import can_make_request, QUOTA_FILE, save_quota_data

def run_test():
    print(f"--- Starting Test (Limit: 50 RPD) ---")
    print(f"Quota file: {QUOTA_FILE}\n")

    # Clean up old quota file for a fresh test
    if os.path.exists(QUOTA_FILE):
        os.remove(QUOTA_FILE)
        print("Removed old quota file.")

    # 1. --- Test the Limit ---
    print("\n--- Testing Daily Limit (55 requests) ---")
    for i in range(1, 56):
        is_allowed, count = can_make_request()
        if is_allowed:
            print(f"Request #{i}: ALLOWED (Count: {count})")
        else:
            print(f"Request #{i}: DENIED (Count: {count})")
        
        # Add a tiny delay so it's not instantaneous
        time.sleep(0.05)

    print("\n--- Limit Test Complete ---")
    try:
        with open(QUOTA_FILE, 'r') as f:
            data = json.load(f)
            print(f"Final quota file state: {data}")
            if data['request_count'] == 50:
                print("SUCCESS: Counter stopped at 50.")
            else:
                print(f"FAILURE: Counter is at {data['request_count']}.")
    except Exception as e:
        print(f"Error reading quota file: {e}")


    # 2. --- Test the Date Reset ---
    print("\n--- Testing Date Reset ---")
    print("Manually setting quota date to yesterday...")

    # Get yesterday's date in UTC
    yesterday_utc = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # Create fake "old" data
    old_data = {
        'request_count': 50,
        'last_reset_date': yesterday_utc
    }
    save_quota_data(old_data)
    print(f"Quota file set to: {old_data}")

    # Try making a request now
    is_allowed, count = can_make_request()
    
    print(f"\nMaking one new request...")
    if is_allowed and count == 1:
        print(f"SUCCESS: Request was ALLOWED and count reset to {count}.")
    else:
        print(f"FAILURE: Request was denied or count ({count}) did not reset to 1.")

    print("\n--- Test Finished ---")
    # Clean up the file
    if os.path.exists(QUOTA_FILE):
        os.remove(QUOTA_FILE)
        print(f"Removed {QUOTA_FILE} for cleanup.")


if __name__ == "__main__":
    run_test()