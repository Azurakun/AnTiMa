# dashboard.py
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import asyncio
from utils.db import db # Your existing synchronous PyMongo connection
import json
from datetime import datetime
import functools

# Setup Collections
stats_collection = db["bot_stats"]
live_activity_collection = db["live_activity"]
rpg_collection = db["rpg_sessions"]

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Helper to run synchronous DB calls in a thread (Prevents blocking the event loop)
async def run_sync_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    partial = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, partial)

def fetch_data_sync():
    """
    Synchronous data fetching logic compatible with PyMongo.
    This function runs inside a thread.
    """
    
    # 1. Global Stats
    # Removed 'await'. find_one returns the dict directly or None.
    global_stats = stats_collection.find_one({"_id": "global"}) or {}
    
    # 2. Top Active Guilds
    # PyMongo returns a cursor that we can list() directly.
    top_guilds = list(stats_collection.find({"_id": {"$regex": "^guild_"}}).sort("messages", -1).limit(5))
    
    # 3. Top Active Users
    top_users = list(stats_collection.find({"_id": {"$regex": "^user_"}}).sort("messages", -1).limit(5))

    # 4. Live Activity Feed
    activities = list(live_activity_collection.find().sort("timestamp", -1).limit(10))
    
    # Format data for JSON serialization
    for a in activities:
        a["_id"] = str(a["_id"])
        if isinstance(a.get("timestamp"), datetime):
            a["timestamp"] = a["timestamp"].strftime("%H:%M:%S")

    # 5. RPG Stats
    active_rpgs = rpg_collection.count_documents({})

    return {
        "global": {
            "messages": global_stats.get("total_messages", 0),
            "commands": global_stats.get("total_commands", 0),
            "guilds": global_stats.get("total_guilds", 0),
            "users": global_stats.get("total_users", 0),
            "active_rpgs": active_rpgs
        },
        "top_guilds": top_guilds,
        "top_users": top_users,
        "activities": activities
    }

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# --- WEBSOCKET FOR LIVE UPDATES ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Execute the sync DB fetch in a separate thread so we can 'await' it
            data = await run_sync_db(fetch_data_sync)
            
            # Send to frontend
            await websocket.send_text(json.dumps(data, default=str))
            
            # Update frequency (2 seconds)
            await asyncio.sleep(2)
    except Exception as e:
        print(f"WebSocket disconnected: {e}")

if __name__ == "__main__":
    # Run on port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)