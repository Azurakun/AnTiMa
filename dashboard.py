# dashboard.py
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import asyncio
import json
import sys
import os
import functools
from datetime import datetime
from utils.db import (
    stats_collection, 
    live_activity_collection, 
    rpg_sessions_collection, 
    logs_collection,
    ai_config_collection, # Added for config management
    db # Added for accessing generic collections
)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Collection for queuing actions from Dashboard -> Bot
web_actions_collection = db["web_actions"]

# --- ASYNC DATABASE HELPER ---
async def run_sync_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

# --- DATA MODELS ---
class ConfigRequest(BaseModel):
    guild_id: str
    channel_id: str | None = None
    frequency: str | None = None
    bot_status: str | None = None # "on" or "off"
    group_chat: str | None = None # "allow" or "block"
    rpg_channel_id: str | None = None

class ActionRequest(BaseModel):
    guild_id: str
    action_type: str # kick, ban, purge, set_limit
    target_id: str # User ID or Channel ID
    reason: str | None = "Action requested via Dashboard"
    setting_value: int | str | None = None # For limit value, purge amount, delete days

# --- DATA FETCHING FUNCTIONS ---

def fetch_overview():
    """Fetches high-level stats for the top cards."""
    global_stats = stats_collection.find_one({"_id": "global"}) or {}
    active_rpgs = rpg_sessions_collection.count_documents({"active": {"$ne": False}})
    return {
        "messages": global_stats.get("total_messages", 0),
        "commands": global_stats.get("total_commands", 0),
        "guilds": global_stats.get("total_guilds", 0),
        "users": global_stats.get("total_users", 0),
        "active_rpgs": active_rpgs
    }

def fetch_details(data_type: str):
    """Fetches detailed lists for the modals."""
    data = []
    
    if data_type == "users":
        cursor = stats_collection.find({"_id": {"$regex": "^user_"}}).sort("messages", -1).limit(50)
        for doc in cursor: 
            data.append({
                "id": doc["_id"], 
                "name": doc.get("name", "Unknown"), 
                "messages": doc.get("messages", 0)
            })

    elif data_type == "guilds":
        cursor = stats_collection.find({"_id": {"$regex": "^guild_"}}).sort("messages", -1).limit(50)
        for doc in cursor: 
            data.append({
                "id": doc["_id"], 
                "name": doc.get("name", "Unknown"), 
                "messages": doc.get("messages", 0)
            })

    elif data_type == "rpgs":
        # Fetch ALL sessions, sorted by Active status then Date
        cursor = rpg_sessions_collection.find().sort([("active", -1), ("last_active", -1)])
        for doc in cursor:
            # Skip items pending deletion to update UI immediately
            if doc.get("delete_requested"): continue
            
            data.append({
                "thread_id": str(doc.get("thread_id")),
                "title": doc.get("title"), 
                "host": doc.get("owner_name"), 
                "scenario": doc.get("scenario_type"),
                "is_active": doc.get("active", True), # Used for filtering
                "last_active": doc.get("last_active", datetime.utcnow()).strftime("%Y-%m-%d %H:%M")
            })

    elif data_type == "commands":
        global_stats = stats_collection.find_one({"_id": "global"}) or {}
        cmd_usage = global_stats.get("command_usage", {})
        for cmd, count in cmd_usage.items():
            data.append({"command": cmd, "uses": count})
        data.sort(key=lambda x: x['uses'], reverse=True)

    return data

def fetch_live_feed():
    """Fetches the last 10 live activities."""
    cursor = live_activity_collection.find().sort("timestamp", -1).limit(10)
    return [{
        "user": d.get("user"), 
        "guild": d.get("guild"), 
        "action": d.get("action"), 
        "timestamp": d.get("timestamp").strftime("%H:%M:%S") if d.get("timestamp") else ""
    } for d in cursor]

def fetch_recent_logs():
    """Fetches the latest logs for the terminal."""
    cursor = logs_collection.find().sort("created_at", -1).limit(2)
    logs = []
    for bucket in cursor: logs.extend(bucket.get("logs", []))
    logs.sort(key=lambda x: x["timestamp"]) 
    return [{
        "time": l["timestamp"].strftime("%H:%M:%S"), 
        "level": l["level"], 
        "logger": l["logger"], 
        "message": l["message"]
    } for l in logs[-50:]]

def fetch_log_history_dates():
    """Aggregates logs by date for the history view."""
    pipeline = [
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "count": {"$sum": {"$size": "$logs"}}}},
        {"$sort": {"_id": -1}}
    ]
    return [{"date": r["_id"], "count": r["count"]} for r in logs_collection.aggregate(pipeline)]

def fetch_logs_by_date(date_str: str):
    """Fetches specific logs for a date string."""
    cursor = logs_collection.find({"_id": {"$regex": f"^{date_str}"}})
    logs = []
    for doc in cursor: logs.extend(doc.get("logs", []))
    logs.sort(key=lambda x: x["timestamp"])
    return [{
        "time": l["timestamp"].strftime("%H:%M:%S"), 
        "level": l["level"], 
        "logger": l["logger"], 
        "message": l["message"]
    } for l in logs]

# --- API ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/details/{data_type}")
async def get_details(data_type: str):
    data = await run_sync_db(fetch_details, data_type)
    return JSONResponse(data)

@app.get("/api/history/dates")
async def get_history_dates():
    data = await run_sync_db(fetch_log_history_dates)
    return JSONResponse(data)

@app.get("/api/history/view/{date_str}")
async def get_history_logs(date_str: str):
    data = await run_sync_db(fetch_logs_by_date, date_str)
    return JSONResponse(data)

@app.post("/api/action/restart")
async def action_restart():
    async def restart_task():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    asyncio.create_task(restart_task())
    return JSONResponse({"status": "Restarting system..."})

@app.post("/api/rpg/delete/{thread_id}")
async def delete_rpg_session(thread_id: str):
    """Marks an RPG session for deletion. The Bot picks this up."""
    try:
        t_id_int = int(thread_id)
        rpg_sessions_collection.update_one(
            {"thread_id": t_id_int}, 
            {"$set": {"delete_requested": True}}
        )
        return JSONResponse({"status": "Marked for deletion"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- CONTROL PANEL ROUTES ---

@app.post("/api/control/config/update")
async def update_bot_config(data: ConfigRequest):
    """Updates server-specific configuration directly in DB."""
    try:
        update_fields = {}
        
        # Mapping frontend values to DB schema
        if data.channel_id: 
            update_fields["channel"] = int(data.channel_id)
            
        if data.frequency: 
            update_fields["chat_frequency"] = data.frequency
            # Reset next time to trigger update logic in bot if needed
            update_fields["next_chat_time"] = datetime.utcnow()
            
        if data.bot_status:
            update_fields["bot_disabled"] = (data.bot_status == "off")
            
        if data.group_chat:
            update_fields["group_chat_enabled"] = (data.group_chat == "allow")
            
        if data.rpg_channel_id:
            update_fields["rpg_channel_id"] = int(data.rpg_channel_id)

        if not update_fields:
            return JSONResponse({"error": "No valid fields provided"}, status_code=400)

        ai_config_collection.update_one(
            {"_id": str(data.guild_id)}, 
            {"$set": update_fields}, 
            upsert=True
        )
        
        # Log this action for admin visibility
        live_activity_collection.insert_one({
            "user": "Dashboard Admin",
            "guild": f"ID: {data.guild_id}",
            "action": "Updated Config",
            "timestamp": datetime.utcnow()
        })

        return JSONResponse({"status": "Configuration updated successfully"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/control/action/queue")
async def queue_admin_action(data: ActionRequest):
    """Queues a moderation or creator action for the bot to execute."""
    try:
        action_doc = {
            "guild_id": data.guild_id,
            "type": data.action_type,
            "target_id": data.target_id,
            "reason": data.reason,
            "setting_value": data.setting_value,
            "status": "pending",
            "created_at": datetime.utcnow(),
            "source": "dashboard"
        }
        
        web_actions_collection.insert_one(action_doc)
        
        live_activity_collection.insert_one({
            "user": "Dashboard Admin",
            "guild": f"ID: {data.guild_id}",
            "action": f"Queued {data.action_type.upper()}",
            "timestamp": datetime.utcnow()
        })
        
        return JSONResponse({"status": f"Action '{data.action_type}' queued for execution."})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- WEBSOCKET ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Gather all real-time data
            overview, feed, logs = await asyncio.gather(
                run_sync_db(fetch_overview),
                run_sync_db(fetch_live_feed),
                run_sync_db(fetch_recent_logs)
            )
            
            payload = {
                "overview": overview,
                "activities": feed,
                "logs": logs
            }
            
            await websocket.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(2) # 2 second refresh rate
    except Exception: 
        pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)