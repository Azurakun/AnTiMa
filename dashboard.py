# dashboard.py
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import asyncio
import json
from datetime import datetime, timedelta
import functools
from utils.db import stats_collection, live_activity_collection, rpg_sessions_collection

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Async DB Helper
async def run_sync_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    partial = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, partial)

# --- DATA FETCHING ---

def fetch_overview():
    global_stats = stats_collection.find_one({"_id": "global"}) or {}
    active_rpgs = rpg_sessions_collection.count_documents({})
    return {
        "messages": global_stats.get("total_messages", 0),
        "commands": global_stats.get("total_commands", 0),
        "guilds": global_stats.get("total_guilds", 0),
        "users": global_stats.get("total_users", 0),
        "active_rpgs": active_rpgs
    }

def fetch_details(data_type: str):
    data = []
    
    if data_type == "users":
        cursor = stats_collection.find({"_id": {"$regex": "^user_"}}).sort("messages", -1).limit(50)
        for doc in cursor:
            data.append({
                "id": doc["_id"].replace("user_", ""),
                "name": doc.get("name", "Unknown"),
                "messages": doc.get("messages", 0),
                "last_active": doc.get("last_active", "").strftime("%Y-%m-%d %H:%M") if isinstance(doc.get("last_active"), datetime) else "N/A"
            })

    elif data_type == "guilds":
        cursor = stats_collection.find({"_id": {"$regex": "^guild_"}}).sort("messages", -1).limit(50)
        for doc in cursor:
            data.append({
                "id": doc["_id"].replace("guild_", ""),
                "name": doc.get("name", "Unknown Server"),
                "messages": doc.get("messages", 0),
                "last_active": doc.get("last_active", "").strftime("%Y-%m-%d %H:%M") if isinstance(doc.get("last_active"), datetime) else "N/A"
            })

    elif data_type == "commands":
        cursor = stats_collection.find({"_id": {"$regex": "^cmd_"}}).sort("usage_count", -1).limit(50)
        for doc in cursor:
            data.append({
                "name": doc.get("name", "Unknown"),
                "usage_count": doc.get("usage_count", 0),
                "last_used": doc.get("last_used", "").strftime("%Y-%m-%d %H:%M") if isinstance(doc.get("last_used"), datetime) else "N/A"
            })

    elif data_type == "rpgs":
        cursor = rpg_sessions_collection.find().sort("last_active", -1)
        now = datetime.utcnow()
        for doc in cursor:
            last_active = doc.get("last_active")
            is_active = False
            last_str = "Never"
            if isinstance(last_active, datetime):
                if (now - last_active) < timedelta(hours=24): is_active = True
                last_str = last_active.strftime("%Y-%m-%d %H:%M")

            data.append({
                "title": doc.get("title", "Quest"),
                "host": doc.get("owner_name", "Unknown"),
                "status": "Active" if is_active else "Inactive",
                "last_active": last_str,
                "scenario": doc.get("scenario_type", "Unknown")
            })

    return data

def fetch_live_feed():
    cursor = live_activity_collection.find().sort("timestamp", -1).limit(10)
    feed = []
    for doc in cursor:
        feed.append({
            "user": doc.get("user", "Unknown"),
            "guild": doc.get("guild", "DM"),
            "action": doc.get("action", "Action"),
            "timestamp": doc.get("timestamp").strftime("%H:%M:%S") if isinstance(doc.get("timestamp"), datetime) else ""
        })
    return feed

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/details/{data_type}")
async def get_details_api(data_type: str):
    data = await run_sync_db(fetch_details, data_type)
    return JSONResponse(content=data)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            overview = await run_sync_db(fetch_overview)
            feed = await run_sync_db(fetch_live_feed)
            await websocket.send_text(json.dumps({"overview": overview, "activities": feed}, default=str))
            await asyncio.sleep(2)
    except Exception:
        pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)