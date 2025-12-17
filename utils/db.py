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
server_lore_collection = db.get_collection("server_lore")
ai_config_collection = db.get_collection("ai_config")
rpg_sessions_collection = db["rpg_sessions"]     # Stores active game threads
rpg_inventory_collection = db["rpg_inventory"]   # Stores user items

# UPDATED: Stores personal/contextual memories tied to users and guilds.
ai_personal_memories_collection = db.get_collection("ai_personal_memories")
ai_personal_memories_collection.create_index([("user_id", pymongo.ASCENDING), ("guild_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])

# NEW: Stores general, objective knowledge shared across all guilds.
ai_global_memories_collection = db.get_collection("ai_global_memories")
ai_global_memories_collection.create_index([("timestamp", pymongo.DESCENDING)])

logs_collection = db.get_collection("improved_logs")
print("Logging is configured to use the 'improved_logs' collection in the main database.")