# utils/db.py
import pymongo
import os

# --- Main Database Connection ---
MONGO_URL = os.environ.get("MONGO_URL")
client = pymongo.MongoClient(MONGO_URL)
db = client.get_database("antima_db")

# Collections for the main database
reminders_collection = db.get_collection("reminders")
user_timezones_collection = db.get_collection("user_timezones")
ai_config_collection = db.get_collection("ai_config")
ai_memories_collection = db.get_collection("ai_memories")
ai_memories_collection.create_index([("user_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])


# --- Dedicated Logging Database Connection ---
MONGO_URL_LOGS = os.environ.get("MONGO_URL_LOGS")
if MONGO_URL_LOGS:
    try:
        logs_client = pymongo.MongoClient(MONGO_URL_LOGS)
        logs_db = logs_client.get_database("antima_logs_db")
        logs_collection = logs_db.get_collection("daily_logs")
        print("Successfully connected to the logging database.")
    except Exception as e:
        print(f"Could not connect to logging database: {e}")
        # Fallback to the main database if the log one fails
        logs_collection = db.get_collection("logs")
else:
    print("MONGO_URL_LOGS not set. Falling back to main database for logs.")
    logs_collection = db.get_collection("logs") # Fallback collection

