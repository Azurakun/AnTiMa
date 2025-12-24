# dashboard.py
from fastapi import FastAPI, WebSocket, Request, HTTPException
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
    ai_config_collection,
    db 
)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- COLLECTIONS ---
web_actions_collection = db["web_actions"]
rpg_web_tokens_collection = db["rpg_web_tokens"]

# --- ASYNC DATABASE HELPER ---
async def run_sync_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

# --- DATA MODELS ---
class ConfigRequest(BaseModel):
    guild_id: str
    channel_id: str | None = None
    frequency: str | None = None
    bot_status: str | None = None 
    group_chat: str | None = None 
    rpg_channel_id: str | None = None

class ActionRequest(BaseModel):
    guild_id: str
    action_type: str 
    target_id: str 
    reason: str | None = "Action requested via Dashboard"
    setting_value: int | str | None = None 

class RPGSetupData(BaseModel):
    token: str
    title: str
    scenario: str
    lore: str
    story_mode: bool
    character: dict 

# --- DATA FETCHING FUNCTIONS ---
def fetch_overview():
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
    data = []
    if data_type == "users":
        cursor = stats_collection.find({"_id": {"$regex": "^user_"}}).sort("messages", -1).limit(50)
        for doc in cursor: 
            data.append({"id": doc["_id"], "name": doc.get("name", "Unknown"), "messages": doc.get("messages", 0)})
    elif data_type == "guilds":
        cursor = stats_collection.find({"_id": {"$regex": "^guild_"}}).sort("messages", -1).limit(50)
        for doc in cursor: 
            data.append({"id": doc["_id"], "name": doc.get("name", "Unknown"), "messages": doc.get("messages", 0)})
    elif data_type == "rpgs":
        cursor = rpg_sessions_collection.find().sort([("active", -1), ("last_active", -1)])
        for doc in cursor:
            if doc.get("delete_requested"): continue
            data.append({
                "thread_id": str(doc.get("thread_id")),
                "title": doc.get("title"), 
                "host": doc.get("owner_name"), 
                "scenario": doc.get("scenario_type"),
                "is_active": doc.get("active", True),
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
    cursor = live_activity_collection.find().sort("timestamp", -1).limit(10)
    return [{
        "user": d.get("user"), "guild": d.get("guild"), "action": d.get("action"), 
        "timestamp": d.get("timestamp").strftime("%H:%M:%S") if d.get("timestamp") else ""
    } for d in cursor]

def fetch_recent_logs():
    cursor = logs_collection.find().sort("created_at", -1).limit(2)
    logs = []
    for bucket in cursor: logs.extend(bucket.get("logs", []))
    logs.sort(key=lambda x: x["timestamp"]) 
    return [{
        "time": l["timestamp"].strftime("%H:%M:%S"), "level": l["level"], 
        "logger": l["logger"], "message": l["message"]
    } for l in logs[-50:]]

def fetch_log_history_dates():
    pipeline = [
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "count": {"$sum": {"$size": "$logs"}}}},
        {"$sort": {"_id": -1}}
    ]
    return [{"date": r["_id"], "count": r["count"]} for r in logs_collection.aggregate(pipeline)]

def fetch_logs_by_date(date_str: str):
    cursor = logs_collection.find({"_id": {"$regex": f"^{date_str}"}})
    logs = []
    for doc in cursor: logs.extend(doc.get("logs", []))
    logs.sort(key=lambda x: x["timestamp"])
    return [{"time": l["timestamp"].strftime("%H:%M:%S"), "level": l["level"], "logger": l["logger"], "message": l["message"]} for l in logs]

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
    try:
        rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, {"$set": {"delete_requested": True}})
        return JSONResponse({"status": "Marked for deletion"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- RPG WEB SETUP ROUTES ---

@app.get("/rpg/setup", response_class=HTMLResponse)
async def rpg_setup_page(request: Request, token: str):
    """Serves the RPG creation form if token is valid."""
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one({"token": token, "status": "pending"}))
    if not token_doc:
        return HTMLResponse("<h1>Invalid or Expired Link</h1><p>Generate a new link in Discord with <code>/rpg web_new</code>.</p>", status_code=404)
    return templates.TemplateResponse("rpg_setup.html", {"request": request, "token": token})

@app.post("/api/rpg/submit")
async def submit_rpg_setup(data: RPGSetupData):
    """Handles the form submission and queues the creation action for the bot."""
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one_and_update(
        {"token": data.token, "status": "pending"},
        {"$set": {"status": "submitted"}}
    ))
    
    if not token_doc:
        raise HTTPException(status_code=400, detail="Invalid or used token.")

    action_doc = {
        "type": "create_rpg_web",
        "guild_id": token_doc["guild_id"],
        "user_id": token_doc["user_id"],
        "status": "pending",
        "timestamp": datetime.utcnow(),
        "data": {
            "title": data.title,
            "scenario": data.scenario,
            "lore": data.lore,
            "story_mode": data.story_mode,
            "character": data.character
        }
    }
    
    await run_sync_db(lambda: web_actions_collection.insert_one(action_doc))
    return JSONResponse({"status": "success", "message": "Adventure queued."})

# --- CONTROL PANEL ROUTES ---

@app.post("/api/control/config/update")
async def update_bot_config(data: ConfigRequest):
    try:
        update_fields = {}
        if data.channel_id: update_fields["channel"] = int(data.channel_id)
        if data.frequency: 
            update_fields["chat_frequency"] = data.frequency
            update_fields["next_chat_time"] = datetime.utcnow()
        if data.bot_status: update_fields["bot_disabled"] = (data.bot_status == "off")
        if data.group_chat: update_fields["group_chat_enabled"] = (data.group_chat == "allow")
        if data.rpg_channel_id: update_fields["rpg_channel_id"] = int(data.rpg_channel_id)

        if not update_fields: return JSONResponse({"error": "No valid fields"}, status_code=400)

        ai_config_collection.update_one({"_id": str(data.guild_id)}, {"$set": update_fields}, upsert=True)
        live_activity_collection.insert_one({
            "user": "Dashboard Admin", "guild": f"ID: {data.guild_id}",
            "action": "Updated Config", "timestamp": datetime.utcnow()
        })
        return JSONResponse({"status": "Configuration updated"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/control/action/queue")
async def queue_admin_action(data: ActionRequest):
    try:
        action_doc = {
            "guild_id": data.guild_id, "type": data.action_type, "target_id": data.target_id,
            "reason": data.reason, "setting_value": data.setting_value,
            "status": "pending", "created_at": datetime.utcnow(), "source": "dashboard"
        }
        web_actions_collection.insert_one(action_doc)
        live_activity_collection.insert_one({
            "user": "Dashboard Admin", "guild": f"ID: {data.guild_id}",
            "action": f"Queued {data.action_type.upper()}", "timestamp": datetime.utcnow()
        })
        return JSONResponse({"status": f"Action '{data.action_type}' queued."})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- WEBSOCKET ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            overview, feed, logs = await asyncio.gather(
                run_sync_db(fetch_overview), run_sync_db(fetch_live_feed), run_sync_db(fetch_recent_logs)
            )
            payload = {"overview": overview, "activities": feed, "logs": logs}
            await websocket.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(2)
    except Exception: pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)