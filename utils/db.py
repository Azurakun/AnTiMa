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

# --- Unified Logs Collection ---
# Logs will now be stored in the main database.
logs_collection = db.get_collection("logs")
print("Logging is configured to use the main database.")