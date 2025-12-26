# utils/db.py
import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = "antima_db"

if not MONGO_URI:
    client = MongoClient("mongodb://localhost:27017/")
else:
    client = MongoClient(MONGO_URI)

db = client[DB_NAME]

# --- COLLECTIONS ---
ai_config_collection = db["ai_config"]
ai_personal_memories_collection = db["ai_personal_memories"]
ai_global_memories_collection = db["ai_global_memories"]
server_lore_collection = db["server_lore"]
search_debug_collection = db["search_debug"] 

# RPG Core
rpg_sessions_collection = db["rpg_sessions"]
rpg_inventory_collection = db["rpg_inventory"]
rpg_web_tokens_collection = db["rpg_web_tokens"]
user_personas_collection = db["user_personas"]

# RPG Extended Memory & State
rpg_vector_memory_collection = db["rpg_vector_memory"] # Stores embeddings
rpg_world_state_collection = db["rpg_world_state"]   # Stores detailed NPC/Location sheets

stats_collection = db["bot_stats"] 
live_activity_collection = db["live_activity"]
web_actions_collection = db["web_actions"]

user_timezones_collection = db["user_timezones"]
reminders_collection = db["reminders"]
logs_collection = db["improved_logs"]

def init_db():
    try:
        client.admin.command('ping')
        print(f"✅ MongoDB Connected to: {DB_NAME}")
    except Exception as e:
        print(f"❌ MongoDB Connection Failed: {e}")