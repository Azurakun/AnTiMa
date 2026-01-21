# dashboard.py
from fastapi import FastAPI, WebSocket, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import asyncio
import json
import sys
import os
import functools
import uuid
from datetime import datetime
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
    db 
)
from cogs.rpg_system.config import SCENARIOS, PREMADE_CHARACTERS

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

class ActionRequest(BaseModel):
    guild_id: str
    action_type: str 
    target_id: str 
    reason: str | None = "Action requested via Dashboard"
    setting_value: int | str | None = None 

# [NEW] NPC Management Models
class ManageNPCRequest(BaseModel):
    thread_id: str
    action: str  # 'add', 'edit', 'delete'
    original_name: str | None = None # For identifying which NPC to edit/delete
    data: dict | None = None # For new/updated data

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
    # Reverse to show chronological order in terminal
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

    # --- HEADER ---
    doc.append(separator)
    doc.append(f"CAMPAIGN CHRONICLE: {session.get('title', 'Untitled Adventure')}")
    doc.append(separator)
    doc.append(f"Host/Owner: {session.get('owner_name', 'Unknown')}")
    doc.append(f"Scenario: {session.get('scenario_type', 'Custom')}")
    doc.append(f"Export Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    doc.append(f"Status: {'Active' if session.get('active') else 'Concluded'}")
    doc.append("")
    
    # --- LORE ---
    doc.append(separator)
    doc.append("SETTING & LORE")
    doc.append(separator)
    doc.append(session.get("lore", "No specific lore recorded."))
    doc.append("")

    # --- PLAYERS ---
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

    # --- WORLD STATE: QUESTS ---
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

    # --- WORLD STATE: NPCS ---
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

    # --- WORLD STATE: LOCATIONS & EVENTS ---
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

    # --- STORY CHRONICLE ---
    doc.append(separator)
    doc.append("THE CHRONICLE (FULL NARRATIVE)")
    doc.append(separator)
    doc.append("Note: Reconstructed from active turns and archived memory banks.\n")

    # 1. Fetch Archived History (Stored in Vectors)
    archives = list(rpg_vector_memory_collection.find({
        "thread_id": tid, 
        "metadata.type": {"$in": ["archived_history", "historical_sync"]}
    }).sort("timestamp", 1))

    for arc in archives:
        text = arc.get("text", "")
        # Basic cleanup if stored with metadata headers inside text
        doc.append(text)
        doc.append("\n" + sub_separator + "\n")

    # 2. Fetch Active Turn History
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
    return templates.TemplateResponse("index.html", {"request": request})

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
    """API Endpoint for the Command Prompt Interface."""
    try:
        data = await run_sync_db(fetch_rpg_debug_logs, thread_id)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# [NEW] NPC MANAGEMENT API
@app.post("/api/rpg/manage/npc")
async def manage_npc(req: ManageNPCRequest):
    """
    Directly modifies the World State to Add, Edit, or Delete NPCs.
    This simulates the tool 'update_world_entity' but from the dashboard.
    """
    try:
        tid = int(req.thread_id)
        if req.action == "delete":
            if not req.original_name: return JSONResponse({"error": "Missing original_name"}, status_code=400)
            key_name = req.original_name.strip().replace('.', '_').replace('$', '')
            
            # Using $unset to remove the key entirely
            await run_sync_db(lambda: rpg_world_state_collection.update_one(
                {"thread_id": tid},
                {"$unset": {f"npcs.{key_name}": ""}}
            ))
            return JSONResponse({"status": "deleted", "name": req.original_name})

        elif req.action in ["add", "edit"]:
            if not req.data or "name" not in req.data: return JSONResponse({"error": "Missing data or name"}, status_code=400)
            
            name = req.data["name"].strip()
            safe_name = name.replace('.', '_').replace('$', '')
            key = f"npcs.{safe_name}"

            # If renaming (Edit mode where name changed), delete old key first
            if req.action == "edit" and req.original_name and req.original_name != name:
                old_key = req.original_name.strip().replace('.', '_').replace('$', '')
                await run_sync_db(lambda: rpg_world_state_collection.update_one(
                    {"thread_id": tid}, {"$unset": {f"npcs.{old_key}": ""}}
                ))

            # Construct the payload similar to tools.py
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

@app.get("/rpg/inspect/{thread_id}", response_class=HTMLResponse)
async def inspect_rpg_page(request: Request, thread_id: str):
    return templates.TemplateResponse("memory_inspector.html", {"request": request, "thread_id": thread_id})

# --- RPG SETUP & PERSONAS ---

@app.get("/rpg/setup", response_class=HTMLResponse)
async def rpg_setup_page(request: Request, token: str):
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one({"token": token, "status": "pending"}))
    if not token_doc:
        return HTMLResponse("<h1>Invalid or Expired Link</h1>", status_code=404)
    user_id = token_doc["user_id"]
    personas = await run_sync_db(lambda: list(user_personas_collection.find({"user_id": user_id}, {"_id": 0})))
    personas = [serialize_persona(p) for p in personas]
    return templates.TemplateResponse("rpg_setup.html", {
        "request": request, "token": token, "scenarios": SCENARIOS, "premades": PREMADE_CHARACTERS, "personas": personas
    })

@app.get("/rpg/personas", response_class=HTMLResponse)
async def rpg_personas_page(request: Request, token: str):
    token_doc = await run_sync_db(lambda: rpg_web_tokens_collection.find_one({"token": token, "status": "pending"}))
    if not token_doc:
        return HTMLResponse("<h1>Invalid Link</h1>", status_code=404)
    user_id = token_doc["user_id"]
    personas = await run_sync_db(lambda: list(user_personas_collection.find({"user_id": user_id}, {"_id": 0})))
    personas = [serialize_persona(p) for p in personas]
    return templates.TemplateResponse("personas.html", {"request": request, "token": token, "personas": personas})

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
        "type": "create_rpg_web", "guild_id": token_doc["guild_id"], "user_id": token_doc["user_id"],
        "status": "pending", "timestamp": datetime.utcnow(),
        "data": {
            "title": data.title, "scenario": data.scenario, "lore": data.lore,
            "story_mode": data.story_mode, "character": data.character
        }
    }
    await run_sync_db(lambda: web_actions_collection.insert_one(action_doc))
    return JSONResponse({"status": "success", "message": "Adventure queued."})

# --- CONTROL ROUTES ---

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
            "user": "Dashboard Admin", "guild": f"ID: {data.guild_id}", "action": "Updated Config", "timestamp": datetime.utcnow()
        })
        return JSONResponse({"status": "Configuration updated"})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

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
            "user": "Dashboard Admin", "guild": f"ID: {data.guild_id}", "action": f"Queued {data.action_type.upper()}", "timestamp": datetime.utcnow()
        })
        return JSONResponse({"status": f"Action '{data.action_type}' queued."})
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