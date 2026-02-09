# restore_game.py
import os
import json
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# 1. Connect to DB
# This uses the connection string from your .env file
mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
try:
    client = MongoClient(mongo_uri)
    db = client["antima_db"]
    print(f"✅ Connected to Database: antima_db")
except Exception as e:
    print(f"❌ Database Connection Failed: {e}")
    exit()

print("\n--- AnTiMa Game Restore Tool ---")
print("This tool registers your existing Discord Thread into the local database.")
print("Please enter the IDs carefully (Right-click in Discord -> Copy ID).\n")

# 2. Ask for IDs
try:
    thread_input = input("1. Paste the Thread ID of your game: ").strip()
    if not thread_input.isdigit(): raise ValueError("Thread ID must be a number.")
    
    guild_input = input("2. Paste the Server (Guild) ID: ").strip()
    if not guild_input.isdigit(): raise ValueError("Guild ID must be a number.")
    
    owner_input = input("3. Paste your User ID: ").strip()
    if not owner_input.isdigit(): raise ValueError("User ID must be a number.")
    
    story_mode_input = input("4. Is this a Story Mode game? (y/n, default n): ").strip().lower()
    is_story_mode = (story_mode_input == 'y')

except ValueError as e:
    print(f"\n❌ Input Error: {e}")
    exit()

# 3. Check for Existing Data
existing_session = db.rpg_sessions.find_one({"thread_id": int(thread_input)})

if existing_session:
    print(f"\n⚠️  WARNING: A session with Thread ID {thread_input} ALREADY EXISTS in the database.")
    print(f"   Current Owner: {existing_session.get('owner_id')}")
    print(f"   Current Turn Count: {existing_session.get('total_turns')}")
    overwrite = input("   Do you want to OVERWRITE it? (y/n): ").lower()
    if overwrite != 'y':
        print("❌ Operation cancelled.")
        exit()

# 4. Construct the Payload
session_data = {
    "thread_id": int(thread_input),
    "guild_id": int(guild_input),
    "owner_id": int(owner_input),
    "active": True,
    "story_mode": is_story_mode,
    "players": [int(owner_input)],
    "turn_history": [],
    "total_turns": 0,
    "scenario_type": "Restored Session",
    "lore": "Waiting for Sync...",
    "created_at": None  # Will be filled by sync or bot
}

# 5. PREVIEW STEP
print("\n" + "="*40)
print("      PREVIEW INFORMATION")
print("="*40)
print(json.dumps(session_data, indent=4))
print("="*40)
print("NOTE: 'lore' and 'turn_history' are empty. This is normal.")
print("The actual story text will be fetched from Discord when you run '/rpg sync'.")
print("-" * 40)

# 6. Confirmation
confirm = input("\n✅ Is this information correct? Write to Database? (y/n): ").lower()

if confirm == 'y':
    try:
        # We use replace_one to completely overwrite if we decided to proceed
        result = db.rpg_sessions.replace_one(
            {"thread_id": int(thread_input)},
            session_data,
            upsert=True
        )
        print(f"\n🚀 SUCCESS! Thread {thread_input} registered.")
        print("Next Step: Start the bot and type '/rpg sync' in that Discord thread.")
    except Exception as e:
        print(f"\n❌ Write Error: {e}")
else:
    print("\n❌ Operation cancelled by user.")