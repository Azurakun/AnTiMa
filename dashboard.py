# dashboard.py
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import asyncio
import json
from datetime import datetime, timedelta
import functools
from utils.db import stats_collection, live_activity_collection, rpg_sessions_collection, search_debug_collection

app = FastAPI()
templates = Jinja2Templates(directory="templates")

async def run_sync_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

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
            data.append({"id": doc["_id"], "name": doc.get("name", "Unknown"), "messages": doc.get("messages", 0)})

    elif data_type == "guilds":
        cursor = stats_collection.find({"_id": {"$regex": "^guild_"}}).sort("messages", -1).limit(50)
        for doc in cursor:
            data.append({"id": doc["_id"], "name": doc.get("name", "Unknown Server"), "messages": doc.get("messages", 0)})

    elif data_type == "debug":
        # New Debug Menu Logic: Returns detailed search logs and sources
        cursor = search_debug_collection.find().sort("timestamp", -1).limit(20)
        for doc in cursor:
            data.append({
                "query": doc.get("query"),
                "time": doc.get("timestamp").strftime("%H:%M:%S") if doc.get("timestamp") else "N/A",
                "sources_used": doc.get("source_count", 0),
                "links": [s['link'] for s in doc.get("sources", [])[:3]],
                "synthesis_preview": doc.get("synthesis", "")[:200] + "..."
            })

    elif data_type == "rpgs":
        cursor = rpg_sessions_collection.find().sort("last_active", -1)
        for doc in cursor:
            data.append({
                "title": doc.get("title", "Quest"),
                "host": doc.get("owner_name", "Unknown"),
                "status": "Active",
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
            "timestamp": doc.get("timestamp").strftime("%H:%M:%S") if doc.get("timestamp") else ""
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
    except Exception: pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)