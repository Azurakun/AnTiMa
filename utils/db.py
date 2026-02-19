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

# Anime Gacha System
anime_gacha_users_collection = db["anime_gacha_users"]       # Currency, stats, cooldowns
anime_gacha_inventory_collection = db["anime_gacha_inventory"] # Owned cards

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
        
        # --- PERFORMANCE INDEXING ---
        print("⚙️ Verifying Database Indexes...")
        
        # 1. RPG Sessions: Queries often look for 'thread_id'
        rpg_sessions_collection.create_index("thread_id", unique=True)
        
        # 2. World State: Always 1:1 with thread_id
        rpg_world_state_collection.create_index("thread_id", unique=True)
        
        # 3. Web Actions: Poller queries by status+type every 3 seconds
        web_actions_collection.create_index([("status", 1), ("type", 1)])
        
        # 4. Vector Memory: Frequent lookups by thread_id
        rpg_vector_memory_collection.create_index("thread_id")

        # 5. Gacha System
        anime_gacha_users_collection.create_index("user_id", unique=True)
        anime_gacha_inventory_collection.create_index([("user_id", 1), ("image_id", 1)], unique=True)
        
        print("✅ Database Indexes Verified.")
        
    except Exception as e:
        print(f"❌ MongoDB Connection/Indexing Failed: {e}")