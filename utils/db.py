# utils/db.py
import os
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = "antima_db"

if not MONGO_URI:
    print("⚠️ Warning: MONGO_URI not found in .env. Defaulting to localhost.")
    client = MongoClient("mongodb://localhost:27017/")
else:
    client = MongoClient(MONGO_URI)

db = client[DB_NAME]

# --- COLLECTIONS ---
# Core AI Config
ai_config_collection = db["ai_config"]
ai_personal_memories_collection = db["ai_personal_memories"]
ai_global_memories_collection = db["ai_global_memories"] # Fixed missing import
server_lore_collection = db["server_lore"]

# RPG System
rpg_sessions_collection = db["rpg_sessions"]
rpg_inventory_collection = db["rpg_inventory"]

# Utility Cogs
logs_collection = db["logs"]             # Fixed missing import
reminders_collection = db["reminders"]   # Fixed missing import
welcome_config_collection = db["welcome_config"]

# Dashboard Stats
stats_collection = db["bot_stats"]
live_activity_collection = db["live_activity"]

def init_db():
    """Checks database connection on startup."""
    try:
        client.admin.command('ping')
        print(f"✅ MongoDB Connected to: {DB_NAME}")
    except Exception as e:
        print(f"❌ MongoDB Connection Failed: {e}")