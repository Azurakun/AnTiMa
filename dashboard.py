# dashboard.py
from fastapi import FastAPI, WebSocket, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from fastapi.encoders import jsonable_encoder
import uvicorn
import asyncio
import json
import sys
import os
import functools
import uuid
import random
import re
import traceback
from datetime import datetime
from collections import namedtuple

from utils.db import (
    stats_collection, 
    live_activity_collection, 
    rpg_sessions_collection, 
    logs_collection,
    ai_config_collection,
    user_personas_collection,
    rpg_web_tokens_collection,
    web_actions_collection,
    rpg_world_state_collection,
    rpg_vector_memory_collection,
    ai_personal_memories_collection,
    ai_global_memories_collection,
    rpg_inventory_collection,
    db 
)
from cogs.rpg_system.config import SCENARIOS, PREMADE_CHARACTERS, RPG_CLASSES
from cogs.ai_chat.prompts import SYSTEM_PROMPT
from cogs.ai_chat.memory_handler import load_user_memories, load_global_memories, summarize_and_save_memory
from cogs.rpg_system.web_engine import WebRPGEngine
from cogs.rpg_system import prompts, tools

web_chat_sessions_collection = db["web_chat_sessions"]
WebUserMock = namedtuple("WebUserMock", ["id", "name"])
web_rpg_engine = WebRPGEngine()

# --- WEB DATA MODELS ---

class WebChatMessageRequest(BaseModel):
    user_id: str
    user_name: str
    message: str
    image_base64: str | None = None
    mime_type: str | None = None

class WebRPGCreateRequest(BaseModel):
    user_id: str
    user_name: str
    title: str
    scenario: str
    lore: str
    story_mode: bool
    character: dict

class WebRPGTurnRequest(BaseModel):
    thread_id: str
    user_id: str
    user_name: str
    prompt: str
    is_reroll: bool = False

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- ASYNC DATABASE HELPER ---
async def run_sync_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

# --- DATA MODELS ---

class RPGSetupData(BaseModel):
    token: str
    title: str
    scenario: str
    lore: str
    story_mode: bool
    character: dict  

class PersonaModel(BaseModel):
    token: str
    id: str | None = None 
    name: str
    class_name: str
    age: int
    pronouns: str
    appearance: str
    personality: str
    hobbies: str
    backstory: str
    alignment: str
    stats: dict

class ConfigRequest(BaseModel):
    guild_id: str
    channel_id: str | None = None
    frequency: str | None = None
    bot_status: str | None = None 
    group_chat: str | None = None 
    rpg_channel_id: str | None = None

class ManageEntityRequest(BaseModel):
    thread_id: str
    category: str # 'npc', 'quest', 'location', 'event'
    action: str  # 'add', 'edit', 'delete'
    original_name: str | None = None 
    data: dict | None = None 

class ManageLogRequest(BaseModel):
    thread_id: str
    action: str # 'add', 'edit', 'delete', 'resolve'
    log_id: str | None = None
    note: str | None = None
    status: str | None = "pending"

# --- HELPER FUNCTIONS ---

def serialize_persona(persona):
    if "created_at" in persona and isinstance(persona["created_at"], datetime):
        persona["created_at"] = persona["created_at"].isoformat()
    if "updated_at" in persona and isinstance(persona["updated_at"], datetime):
        persona["updated_at"] = persona["updated_at"].isoformat()
    return persona

def serialize_world_entity(entity):
    if not entity: return entity
    if "last_updated" in entity and isinstance(entity["last_updated"], datetime):
        entity["last_updated"] = entity["last_updated"].isoformat()
    if "attributes" not in entity: entity["attributes"] = {}
    return entity

def fetch_rpg_debug_logs(thread_id: str):
    """Fetches specific debug logs for the command prompt UI."""
    logs = list(db.rpg_debug_terminal.find({"thread_id": str(thread_id)}).sort("timestamp", -1).limit(50))
    logs.reverse()
    return [{
        "time": l["timestamp"].strftime("%H:%M:%S"),
        "level": l.get("level", "info"),
        "message": l.get("message", ""),
        "details": l.get("details", {})
    } for l in logs]

def fetch_rpg_full_memory(thread_id: str):
    tid = int(thread_id)
    session = rpg_sessions_collection.find_one({"thread_id": tid})
    if not session: return None

    world_state = rpg_world_state_collection.find_one({"thread_id": tid}) or {}
    vectors = list(rpg_vector_memory_collection.find({"thread_id": tid}).sort("timestamp", -1).limit(50))
    
    clean_vectors = []
    for v in vectors:
        clean_vectors.append({
            "text": v.get("text", "No text"),
            "timestamp": v.get("timestamp", datetime.utcnow()).isoformat()
        })

    def process_category(category_key):
        items = []
        if category_key in world_state:
            for key, val in world_state[category_key].items():
                items.append(serialize_world_entity(val))
        return items

    env = world_state.get("environment", {})
    if "last_updated" in env and isinstance(env["last_updated"], datetime):
        env["last_updated"] = env["last_updated"].isoformat()

    raw_logs = world_state.get("story_log", [])
    for l in raw_logs:
        if isinstance(l.get("timestamp"), datetime):
            l["timestamp"] = l["timestamp"].isoformat()
    
    raw_logs.sort(key=lambda x: (x.get("status") != "pending", x.get("timestamp", "")), reverse=False)

    return {
        "meta": {
            "title": session.get("title"),
            "scenario": session.get("scenario_type"),
            "active": session.get("active"),
            "turn_count": len(session.get("turn_history", [])),
            "owner": session.get("owner_name", "Unknown")
        },
        "environment": env, 
        "players": session.get("player_stats", {}),
        "quests": process_category("quests"),
        "npcs": process_category("npcs"),
        "locations": process_category("locations"),
        "events": process_category("events"),
        "campaign_log": session.get("campaign_log", [])[-50:], 
        "story_log": raw_logs,
        "memories": clean_vectors
    }

def generate_campaign_document(thread_id: str):
    tid = int(thread_id)
    session = rpg_sessions_collection.find_one({"thread_id": tid})
    if not session: return None

    world = rpg_world_state_collection.find_one({"thread_id": tid}) or {}

    doc = []
    separator = "=" * 60
    sub_separator = "-" * 40

    doc.append(separator)
    doc.append(f"CAMPAIGN CHRONICLE: {session.get('title', 'Untitled Adventure')}")
    doc.append(separator)
    doc.append(f"Host/Owner: {session.get('owner_name', 'Unknown')}")
    doc.append(f"Scenario: {session.get('scenario_type', 'Custom')}")
    doc.append(f"Export Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    doc.append(f"Status: {'Active' if session.get('active') else 'Concluded'}")
    doc.append("")
    
    doc.append(separator)
    doc.append("SETTING & LORE")
    doc.append(separator)
    doc.append(session.get("lore", "No specific lore recorded."))
    doc.append("")

    doc.append(separator)
    doc.append("PARTY ROSTER")
    doc.append(separator)
    player_stats = session.get("player_stats", {})
    if not player_stats:
        doc.append("No players recorded.")
    else:
        for uid, p in player_stats.items():
            doc.append(f"Name: {p.get('name', 'Unknown')}")
            doc.append(f"Class: {p.get('class', 'Freelancer')}")
            doc.append(f"Race: {p.get('race', 'Unknown')}")
            doc.append(f"Description: {p.get('appearance', 'N/A')}")
            doc.append(f"Background: {p.get('backstory', 'N/A')}")
            doc.append(sub_separator)
    doc.append("")

    doc.append(separator)
    doc.append("QUEST LOG")
    doc.append(separator)
    quests = world.get("quests", {})
    if not quests:
        doc.append("No quests recorded.")
    else:
        for qid, q in quests.items():
            status = q.get("status", "unknown").upper()
            doc.append(f"[{status}] {q.get('name')}")
            doc.append(f"Details: {q.get('details')}")
            attrs = q.get("attributes", {})
            if attrs.get("rewards"): doc.append(f"Rewards: {attrs.get('rewards')}")
            if attrs.get("issuer"): doc.append(f"Issuer: {attrs.get('issuer')}")
            doc.append("")

    doc.append(separator)
    doc.append("NPC REGISTRY")
    doc.append(separator)
    npcs = world.get("npcs", {})
    if not npcs:
        doc.append("No NPCs recorded.")
    else:
        for nid, n in npcs.items():
            doc.append(f"Name: {n.get('name')}")
            attrs = n.get("attributes", {})
            doc.append(f"Role: {attrs.get('role', 'Character')} | State: {attrs.get('state', 'Unknown')}")
            doc.append(f"Gender: {attrs.get('gender', '?')} | Age: {attrs.get('age', '?')} | Race: {attrs.get('race', '?')}")
            doc.append(f"Appearance: {attrs.get('appearance', 'N/A')}")
            doc.append(f"Personality: {attrs.get('personality', 'N/A')}")
            doc.append(f"Relationships: {attrs.get('relationships', attrs.get('relationship', 'None'))}")
            doc.append(f"Summary: {n.get('details')}")
            doc.append(sub_separator)

    doc.append(separator)
    doc.append("LOCATIONS & EVENTS")
    doc.append(separator)
    locations = world.get("locations", {})
    if locations:
        doc.append("--- Locations ---")
        for l in locations.values():
            doc.append(f"• {l.get('name')} ({l.get('status')}): {l.get('details')}")
    
    events = world.get("events", {})
    if events:
        doc.append("\n--- Timeline ---")
        for e in events.values():
            doc.append(f"• {e.get('name')}: {e.get('details')}")
    doc.append("")

    doc.append(separator)
    doc.append("THE CHRONICLE (FULL NARRATIVE)")
    doc.append(separator)
    doc.append("Note: Reconstructed from active turns and archived memory banks.\n")

    archives = list(rpg_vector_memory_collection.find({
        "thread_id": tid, 
        "metadata.type": {"$in": ["archived_history", "historical_sync"]}
    }).sort("timestamp", 1))

    for arc in archives:
        text = arc.get("text", "")
        doc.append(text)
        doc.append("\n" + sub_separator + "\n")

    active_history = session.get("turn_history", [])
    for turn in active_history:
        timestamp = turn.get("timestamp")
        if isinstance(timestamp, datetime): timestamp = timestamp.strftime("%H:%M")
        
        doc.append(f"[{timestamp}] {turn.get('user_name', 'Player')}:")
        doc.append(f"{turn.get('input')}\n")
        
        doc.append(f"[DM]:")
        doc.append(f"{turn.get('output')}\n")
        doc.append(sub_separator + "\n")

    return "\n".join(doc)

# --- FETCH FUNCTIONS ---

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
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/details/{data_type}")
async def get_details_api(data_type: str):
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

@app.get("/api/rpg/export/{thread_id}")
async def export_rpg_session(thread_id: str):
    try:
        content = await run_sync_db(generate_campaign_document, thread_id)
        if not content: return JSONResponse({"error": "Session not found"}, status_code=404)
        
        filename = f"Campaign_Export_{thread_id}.txt"
        return Response(content=content, media_type="text/plain", headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/rpg/memory/{thread_id}")
async def get_rpg_memory(thread_id: str):
    try:
        data = await run_sync_db(fetch_rpg_full_memory, thread_id)
        if not data: return JSONResponse({"error": "Session not found"}, status_code=404)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/rpg/debug/{thread_id}")
async def get_rpg_debug(thread_id: str):
    try:
        data = await run_sync_db(fetch_rpg_debug_logs, thread_id)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# [NEW] GENERIC ENTITY MANAGEMENT API
@app.post("/api/rpg/manage/entity")
async def manage_entity(req: ManageEntityRequest):
    try:
        tid = int(req.thread_id)
        category_map = {
            "npc": "npcs",
            "quest": "quests",
            "location": "locations",
            "event": "events"
        }
        
        if req.category not in category_map:
            return JSONResponse({"error": "Invalid category"}, status_code=400)
            
        collection_key = category_map[req.category]

        if req.action == "delete":
            if not req.original_name: return JSONResponse({"error": "Missing name"}, status_code=400)
            key_name = req.original_name.strip().replace('.', '_').replace('$', '')
            await run_sync_db(lambda: rpg_world_state_collection.update_one(
                {"thread_id": tid},
                {"$unset": {f"{collection_key}.{key_name}": ""}}
            ))
            return JSONResponse({"status": "deleted"})

        elif req.action in ["add", "edit"]:
            if not req.data or "name" not in req.data: return JSONResponse({"error": "Missing data"}, status_code=400)
            
            name = req.data["name"].strip()
            safe_name = name.replace('.', '_').replace('$', '')
            key = f"{collection_key}.{safe_name}"

            if req.action == "edit" and req.original_name and req.original_name != name:
                old_key = req.original_name.strip().replace('.', '_').replace('$', '')
                await run_sync_db(lambda: rpg_world_state_collection.update_one(
                    {"thread_id": tid}, {"$unset": {f"{collection_key}.{old_key}": ""}}
                ))

            # Default structure for any entity
            update_payload = {
                "name": name,
                "details": req.data.get("details", ""),
                "status": req.data.get("status", "active"),
                "last_updated": datetime.utcnow(),
                "attributes": req.data.get("attributes", {})
            }

            await run_sync_db(lambda: rpg_world_state_collection.update_one(
                {"thread_id": tid},
                {"$set": {key: update_payload}},
                upsert=True
            ))
            return JSONResponse({"status": "updated", "name": name})
            
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# [NEW] STORY LOG MANAGEMENT API
@app.post("/api/rpg/manage/log")
async def manage_log(req: ManageLogRequest):
    try:
        tid = int(req.thread_id)
        if req.action == "add":
            if not req.note: return JSONResponse({"error": "Missing note"}, status_code=400)
            entry = {
                "id": str(random.randint(10000, 99999)),
                "note": req.note,
                "status": req.status or "pending",
                "timestamp": datetime.utcnow()
            }
            await run_sync_db(lambda: rpg_world_state_collection.update_one(
                {"thread_id": tid}, {"$push": {"story_log": entry}}, upsert=True
            ))
            return JSONResponse({"status": "added"})

        elif req.action == "edit":
            if not req.log_id: return JSONResponse({"error": "Missing ID"}, status_code=400)
            await run_sync_db(lambda: rpg_world_state_collection.update_one(
                {"thread_id": tid, "story_log.id": req.log_id},
                {"$set": {"story_log.$.note": req.note, "story_log.$.status": req.status}}
            ))
            return JSONResponse({"status": "updated"})

        elif req.action == "delete":
            if not req.log_id: return JSONResponse({"error": "Missing ID"}, status_code=400)
            await run_sync_db(lambda: rpg_world_state_collection.update_one(
                {"thread_id": tid},
                {"$pull": {"story_log": {"id": req.log_id}}}
            ))
            return JSONResponse({"status": "deleted"})
            
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/rpg/inspect/{thread_id}", response_class=HTMLResponse)
async def inspect_rpg_page(request: Request, thread_id: str):
    return templates.TemplateResponse(request=request, name="memory_inspector.html", context={"thread_id": thread_id})

# --- SSE STREAMING ENDPOINT ---
# Added to fix the 404 error and provide live updates to the System Debug Terminal

async def log_stream_generator(thread_id: str):
    """Yields new logs as they appear in the database."""
    last_check = datetime.utcnow()
    # Initial buffer (send last 10 logs so terminal isn't empty)
    initial_logs = await run_sync_db(fetch_rpg_debug_logs, thread_id)
    # fetch_rpg_debug_logs returns formatted logs in chrono order (oldest -> newest)
    for log in initial_logs[-10:]:
        yield f"data: {json.dumps(log)}\n\n"
    
    while True:
        # Check for logs newer than last_check
        new_logs = await run_sync_db(lambda: list(db.rpg_debug_terminal.find({
            "thread_id": str(thread_id),
            "timestamp": {"$gt": last_check}
        }).sort("timestamp", 1)))
        
        if new_logs:
            last_check = new_logs[-1]["timestamp"]
            for log in new_logs:
                # Format for frontend
                formatted = {
                    "time": log["timestamp"].strftime("%H:%M:%S"),
                    "level": log.get("level", "info"),
                    "message": log.get("message", ""),
                    "details": log.get("details", {})
                }
                yield f"data: {json.dumps(formatted)}\n\n"
        
        await asyncio.sleep(1) # Polling interval to reduce DB load

@app.get("/rpg/inspect/{thread_id}/stream")
async def stream_rpg_logs(thread_id: str):
    return StreamingResponse(log_stream_generator(thread_id), media_type="text/event-stream")

# --- RPG SETUP & PERSONAS ---

@app.get("/rpg/setup", response_class=HTMLResponse)
async def rpg_setup_page(request: Request, token: str):
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one({"token": token, "status": "pending"}))
    if not token_doc:
        return HTMLResponse("<h1>Invalid or Expired Link</h1>", status_code=404)
    user_id = token_doc["user_id"]
    personas = await run_sync_db(lambda: list(user_personas_collection.find({"user_id": user_id}, {"_id": 0})))
    personas = [serialize_persona(p) for p in personas]
    return templates.TemplateResponse(request=request, name="rpg_setup.html", context={
        "token": token, "scenarios": SCENARIOS, "premades": PREMADE_CHARACTERS, "personas": personas
    })

@app.get("/rpg/personas", response_class=HTMLResponse)
async def rpg_personas_page(request: Request, token: str):
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one({"token": token, "status": "pending"}))
    if not token_doc:
        return HTMLResponse("<h1>Invalid Link</h1>", status_code=404)
    user_id = token_doc["user_id"]
    personas = await run_sync_db(lambda: list(user_personas_collection.find({"user_id": user_id}, {"_id": 0})))
    personas = [serialize_persona(p) for p in personas]
    return templates.TemplateResponse(request=request, name="personas.html", context={"token": token, "personas": personas})

@app.post("/api/rpg/persona/save")
async def save_persona(data: PersonaModel):
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one({"token": data.token}))
    if not token_doc: raise HTTPException(403, "Invalid Token")
    user_id = token_doc["user_id"]
    if data.id:
        update_data = {
            "name": data.name, "class": data.class_name, "age": data.age, "pronouns": data.pronouns,
            "appearance": data.appearance, "personality": data.personality, "hobbies": data.hobbies,
            "backstory": data.backstory, "alignment": data.alignment, "stats": data.stats, "updated_at": datetime.utcnow()
        }
        await run_sync_db(lambda: user_personas_collection.update_one({"id": data.id, "user_id": user_id}, {"$set": update_data}))
        return JSONResponse({"status": "updated", "id": data.id})
    else:
        new_id = str(uuid.uuid4())
        persona_doc = {
            "id": new_id, "user_id": user_id, "name": data.name, "class": data.class_name,
            "age": data.age, "pronouns": data.pronouns, "appearance": data.appearance,
            "personality": data.personality, "hobbies": data.hobbies, "backstory": data.backstory,
            "alignment": data.alignment, "stats": data.stats, "created_at": datetime.utcnow()
        }
        await run_sync_db(lambda: user_personas_collection.insert_one(persona_doc))
        return JSONResponse({"status": "created", "id": new_id})

@app.delete("/api/rpg/persona/delete/{persona_id}")
async def delete_persona(persona_id: str, token: str):
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one({"token": token}))
    if not token_doc: raise HTTPException(403, "Invalid Token")
    res = await run_sync_db(lambda: user_personas_collection.delete_one({"id": persona_id, "user_id": token_doc["user_id"]}))
    if res.deleted_count == 0: return JSONResponse({"error": "Persona not found"}, status_code=404)
    return JSONResponse({"status": "deleted"})

@app.post("/api/rpg/submit")
async def submit_rpg_setup(data: RPGSetupData):
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one_and_update(
        {"token": data.token, "status": "pending"}, {"$set": {"status": "submitted"}}
    ))
    if not token_doc: raise HTTPException(status_code=400, detail="Invalid token.")
    if data.character.get("save_as_persona"):
        persona_doc = {
            "id": str(uuid.uuid4()), "user_id": token_doc["user_id"], "name": data.character["name"],
            "class": data.character["class"], "age": data.character["age"], "pronouns": data.character.get("pronouns", "They/Them"),
            "appearance": data.character.get("appearance", ""), "personality": data.character.get("personality", ""),
            "hobbies": data.character.get("hobbies", ""), "backstory": data.character["backstory"],
            "alignment": data.character["alignment"], "stats": data.character["stats"], "created_at": datetime.utcnow()
        }
        await run_sync_db(lambda: user_personas_collection.insert_one(persona_doc))
    action_doc = {
        "type": "create_rpg_web",
        "status": "pending",
        "guild_id": token_doc["guild_id"],
        "user_id": token_doc["user_id"],
        "created_at": datetime.utcnow(),
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


# --- WEB CHATBOT ROUTES ---

@app.get("/chat", response_class=HTMLResponse)
async def get_chat_page(request: Request):
    return templates.TemplateResponse(request=request, name="chat.html")

@app.get("/api/chat/history")
async def get_chat_history(user_id: str):
    try:
        uid = int(user_id)
        session = await run_sync_db(lambda: web_chat_sessions_collection.find_one({"user_id": uid}))
        if not session:
            return JSONResponse([])
        messages = session.get("messages", [])
        clean_messages = [
            {"role": m["role"], "content": m["content"], "timestamp": m.get("timestamp"), "gif_url": m.get("gif_url")}
            for m in messages if m["role"] != "system"
        ]
        return JSONResponse(clean_messages)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/chat/clear")
async def clear_chat(user_id: str):
    try:
        uid = int(user_id)
        await run_sync_db(lambda: web_chat_sessions_collection.delete_one({"user_id": uid}))
        await run_sync_db(lambda: ai_personal_memories_collection.delete_many({"user_id": uid, "guild_id": 999999}))
        return JSONResponse({"status": "cleared"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/chat/message")
async def send_chat_message(req: WebChatMessageRequest):
    try:
        uid = int(req.user_id)
        session = await run_sync_db(lambda: web_chat_sessions_collection.find_one({"user_id": uid}))
        if not session:
            session = {
                "user_id": uid,
                "user_name": req.user_name,
                "messages": [],
                "created_at": datetime.utcnow()
            }
            await run_sync_db(lambda: web_chat_sessions_collection.insert_one(session))

        personal_mem = await load_user_memories(uid, 999999, limit=5)
        global_mem = await load_global_memories(limit=5)

        context_text = ""
        if personal_mem:
            context_text += f"\n\n### YOUR MEMORIES OF THE USER:\n{personal_mem}"
        if global_mem:
            context_text += f"\n\n### GLOBAL MEMORIES / WORLD FACTS:\n{global_mem}"

        system_content = SYSTEM_PROMPT + context_text
        messages = [{"role": "system", "content": system_content}]

        history_msgs = session.get("messages", [])[-10:]
        for m in history_msgs:
            messages.append({"role": m["role"], "content": m["content"]})

        user_content = [{"type": "text", "text": f"User {req.user_name} says: \"{req.message}\"."}]

        if req.image_base64 and req.mime_type:
            b64_str = req.image_base64
            if "," in b64_str:
                b64_str = b64_str.split(",")[1]
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{req.mime_type};base64,{b64_str}"}
            })

        messages.append({"role": "user", "content": user_content})

        current_topic = None
        words = [w for w in req.message.split() if len(w) > 3 and w.isalpha()]
        if words:
            current_topic = " ".join(words[:5])

        from cogs.ai_chat.response_handler import _send_and_handle_tool_loop
        final_text, updated_messages = await _send_and_handle_tool_loop(
            messages, message_channel=None, current_topic=current_topic
        )

        processed_text = re.sub(
            r"\[MENTION: (.+?)\]",
            lambda m: m.group(1).strip(),
            final_text
        )

        gif_url = None
        gif_match = re.search(r"\[GIF: (.+?)\]", processed_text)
        if gif_match:
            search_term = gif_match.group(1).strip()
            processed_text = processed_text.replace(gif_match.group(0), "").strip()
            if random.random() < 0.5:
                import aiohttp
                async with aiohttp.ClientSession() as client_session:
                    from cogs.ai_chat.utils import get_gif_url
                    gif_url = await get_gif_url(client_session, search_term)

        timestamp = datetime.utcnow().isoformat()
        await run_sync_db(lambda: web_chat_sessions_collection.update_one(
            {"user_id": uid},
            {"$push": {
                "messages": {
                    "$each": [
                        {"role": "user", "content": req.message, "timestamp": timestamp},
                        {"role": "assistant", "content": processed_text, "timestamp": timestamp, "gif_url": gif_url}
                    ]
                }
            }}
        ))

        msg_count = len(session.get("messages", [])) + 2
        if msg_count % 5 == 0:
            user_mock = WebUserMock(id=uid, name=req.user_name)
            asyncio.create_task(summarize_and_save_memory(None, user_mock, 999999, updated_messages))

        return JSONResponse({
            "role": "assistant",
            "content": processed_text,
            "gif_url": gif_url,
            "timestamp": timestamp
        })
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


# --- WEB RPG ROUTES ---

@app.get("/play", response_class=HTMLResponse)
async def get_rpg_play_page(request: Request):
    return templates.TemplateResponse(request=request, name="rpg_play.html")

@app.get("/api/web_rpg/campaigns")
async def get_web_campaigns(user_id: str):
    try:
        uid = int(user_id)
        cursor = await run_sync_db(lambda: list(rpg_sessions_collection.find({"players": uid, "is_web": True}).sort("last_active", -1)))
        campaigns = []
        for doc in cursor:
            campaigns.append({
                "thread_id": str(doc.get("thread_id")),
                "title": doc.get("title"),
                "scenario": doc.get("scenario_type"),
                "last_active": doc.get("last_active", datetime.utcnow()).strftime("%Y-%m-%d %H:%M"),
                "is_active": doc.get("active", True),
                "turn_count": len(doc.get("turn_history", []))
            })
        return JSONResponse(campaigns)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/web_rpg/config")
async def get_rpg_config():
    try:
        return JSONResponse({
            "scenarios": SCENARIOS,
            "premades": PREMADE_CHARACTERS,
            "classes": RPG_CLASSES
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/web_rpg/create")
async def create_web_campaign(req: WebRPGCreateRequest):
    try:
        uid = int(req.user_id)
        thread_id = random.randint(10**17, 9 * 10**17)
        c = req.character
        profile = {
            "name": c.get("name", req.user_name),
            "class": c.get("class", "Freelancer"),
            "hp": 100, "max_hp": 100,
            "mp": 50, "max_mp": 50,
            "stats": c.get("stats", {"STR": 10, "DEX": 10, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10}),
            "skills": ["Custom Action"],
            "alignment": c.get("alignment", "Neutral Good"),
            "backstory": c.get("backstory", "An adventurer seeking glory."),
            "age": c.get("age", "20"),
            "pronouns": c.get("pronouns", "They/Them"),
            "appearance": c.get("appearance", "Standard adventurer clothing."),
            "personality": c.get("personality", "Curious and brave."),
            "hobbies": c.get("hobbies", "None")
        }
        session_data = {
            "thread_id": thread_id,
            "guild_id": 999999,
            "owner_id": uid,
            "owner_name": req.user_name,
            "title": req.title,
            "players": [uid],
            "player_stats": {str(uid): profile},
            "scenario_type": req.scenario,
            "lore": req.lore,
            "campaign_log": [],
            "turn_history": [],
            "created_at": datetime.utcnow(),
            "last_active": datetime.utcnow(),
            "active": True,
            "delete_requested": False,
            "story_mode": req.story_mode,
            "total_turns": 0,
            "is_web": True
        }
        await run_sync_db(lambda: rpg_sessions_collection.insert_one(session_data))
        
        world_state = {
            "thread_id": thread_id,
            "environment": {
                "time": "08:00",
                "weather": "Clear sky with mild breeze"
            },
            "story_log": [],
            "quests": {},
            "npcs": {},
            "locations": {
                "starting_point": {
                    "name": "Starting Area",
                    "details": "Where the adventure begins.",
                    "status": "active"
                }
            },
            "events": {}
        }
        await run_sync_db(lambda: rpg_world_state_collection.insert_one(world_state))
        
        await web_rpg_engine.get_or_create_session(thread_id, session_data, "Start")
        
        mechanics = "2. **Story Mode Active:** NO DICE." if req.story_mode else "2. **Standard Mode:** Use `roll_d20` for risks."
        sys_prompt = prompts.ADVENTURE_START.format(
            scenario_name=req.scenario,
            lore=req.lore,
            mechanics=mechanics
        )
        
        turn_result = await web_rpg_engine.process_web_turn(
            thread_id=thread_id,
            prompt=sys_prompt,
            user_id=uid,
            user_name="System"
        )
        
        turn_result["thread_id"] = str(thread_id)
        turn_result["title"] = req.title
        
        return JSONResponse(jsonable_encoder(turn_result))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/web_rpg/session/{thread_id}")
async def get_web_rpg_session(thread_id: str, user_id: str):
    try:
        tid = int(thread_id)
        uid = int(user_id)
        session = await run_sync_db(lambda: rpg_sessions_collection.find_one({"thread_id": tid}))
        if not session:
            return JSONResponse({"error": "Campaign not found."}, status_code=404)
            
        world = await run_sync_db(lambda: rpg_world_state_collection.find_one({"thread_id": tid})) or {}
        inv = await run_sync_db(lambda: rpg_inventory_collection.find_one({"user_id": uid}))
        
        clean_turns = []
        for t in session.get("turn_history", []):
            clean_turns.append({
                "turn_id": t.get("turn_id"),
                "user_name": t.get("user_name"),
                "input": t.get("input"),
                "output": t.get("output"),
                "timestamp": t.get("timestamp").isoformat() if isinstance(t.get("timestamp"), datetime) else t.get("timestamp")
            })
            
        suggested_actions = []
        if clean_turns:
            suggested_actions = await web_rpg_engine._generate_suggested_options(clean_turns[-1]["output"])
        else:
            suggested_actions = ["Begin the journey", "Check stats", "Observe surroundings"]
            
        return JSONResponse(jsonable_encoder({
            "thread_id": str(tid),
            "title": session.get("title"),
            "scenario": session.get("scenario_type"),
            "lore": session.get("lore"),
            "active": session.get("active", True),
            "story_mode": session.get("story_mode", False),
            "player_stats": session.get("player_stats", {}),
            "suggested_actions": suggested_actions,
            "turn_history": clean_turns,
            "world_state": {
                "environment": world.get("environment", {}),
                "quests": list(world.get("quests", {}).values()),
                "npcs": list(world.get("npcs", {}).values()),
                "locations": list(world.get("locations", {}).values()),
                "events": list(world.get("events", {}).values()),
                "story_log": world.get("story_log", [])
            },
            "inventory": inv.get("items", []) if inv else []
        }))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/web_rpg/turn")
async def send_web_rpg_turn(req: WebRPGTurnRequest):
    try:
        tid = int(req.thread_id)
        uid = int(req.user_id)
        
        turn_result = await web_rpg_engine.process_web_turn(
            thread_id=tid,
            prompt=req.prompt,
            user_id=uid,
            user_name=req.user_name,
            is_reroll=req.is_reroll
        )
        
        await run_sync_db(lambda: rpg_sessions_collection.update_one(
            {"thread_id": tid},
            {"$set": {"last_active": datetime.utcnow()}}
        ))
        
        return JSONResponse(jsonable_encoder(turn_result))
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/web_rpg/rewind")
async def rewind_web_campaign(thread_id: str, turn_id: int):
    try:
        tid = int(thread_id)
        deleted_turns, rewind_ts = await run_sync_db(web_rpg_engine.memory_manager.trim_history, tid, int(turn_id))
        
        if rewind_ts:
            await web_rpg_engine.memory_manager.purge_memories(tid, rewind_ts, from_turn_id=int(turn_id))
            
        return JSONResponse({"status": "success", "turn_id": turn_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/web_rpg/end")
async def end_web_campaign(thread_id: str):
    try:
        tid = int(thread_id)
        await run_sync_db(lambda: rpg_sessions_collection.update_one({"thread_id": tid}, {"$set": {"active": False}}))
        return JSONResponse({"status": "archived"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# --- CONTROL ROUTES ---

@app.post("/api/control/config/update")
async def update_bot_config(data: ConfigRequest):
    try:
        update_fields = {}
        if data.channel_id: update_fields["channel"] = int(data.channel_id)
        if data.frequency: 
            update_fields["chat_frequency"] = data.frequency
            if data.frequency != "disabled":
                update_fields["next_chat_time"] = datetime.utcnow()
        if data.bot_status: update_fields["bot_disabled"] = (data.bot_status == "off")
        if data.group_chat: update_fields["group_chat_enabled"] = (data.group_chat == "allow")
        if data.rpg_channel_id: update_fields["rpg_channel_id"] = int(data.rpg_channel_id)
        if not update_fields: return JSONResponse({"error": "No valid fields"}, status_code=400)
        ai_config_collection.update_one({"_id": str(data.guild_id)}, {"$set": update_fields}, upsert=True)
        live_activity_collection.insert_one({
            "user": "Dashboard Admin", "guild": f"ID: {data.guild_id}", "action": "Updated Config", "timestamp": datetime.utcnow()
        })
        return JSONResponse({"status": "Configuration updated"})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/control/reload_chat")
async def reload_chat_module():
    try:
        action_doc = {
            "type": "reload_chat", "guild_id": "global", "status": "pending", 
            "created_at": datetime.utcnow(), "source": "dashboard"
        }
        web_actions_collection.insert_one(action_doc)
        live_activity_collection.insert_one({
            "user": "Dashboard Admin", "guild": "Global", "action": "Triggered Reload", "timestamp": datetime.utcnow()
        })
        return JSONResponse({"status": "Reload signal sent."})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

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
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)