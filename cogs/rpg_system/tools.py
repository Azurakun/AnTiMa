# cogs/rpg_system/tools.py
import random
import uuid # <--- Added for Memory IDs
from datetime import datetime, timedelta
from utils.db import rpg_sessions_collection, rpg_inventory_collection, rpg_world_state_collection

def grant_item_to_player(user_id: str, item_name: str, description: str):
    """Adds an item to the player's permanent inventory."""
    try:
        rpg_inventory_collection.update_one(
            {"user_id": int(user_id)},
            {"$push": {"items": {"name": item_name, "desc": description, "obtained_at": datetime.utcnow()}}},
            upsert=True
        )
        return f"System: Added {item_name} to player {user_id}'s inventory."
    except Exception as e: return f"System Error: {e}"

def update_player_stats(thread_id: str, user_id: str, hp_change: int, mp_change: int):
    try:
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session: return "Error: Session not found."
        uid = str(user_id)
        if uid not in session["player_stats"]: return f"Error: Player {uid} not found."
        
        stats = session["player_stats"][uid]
        stats["hp"] = max(0, min(stats["max_hp"], stats["hp"] + int(hp_change)))
        stats["mp"] = max(0, min(stats["max_mp"], stats["mp"] + int(mp_change)))
        
        rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, {"$set": {f"player_stats.{uid}": stats}})
        return f"System: Player {uid} HP/MP updated."
    except Exception as e: return f"System Error: {e}"

def apply_damage(thread_id: str, user_id: str, damage_amount: int):
    return update_player_stats(thread_id, user_id, hp_change=-int(damage_amount), mp_change=0)

def apply_healing(thread_id: str, user_id: str, heal_amount: int):
    return update_player_stats(thread_id, user_id, hp_change=int(heal_amount), mp_change=0)

def deduct_mana(thread_id: str, user_id: str, mana_cost: int):
    return update_player_stats(thread_id, user_id, hp_change=0, mp_change=-int(mana_cost))

def roll_d20(check_type: str, difficulty: int, modifier: int = 0, stat_label: str = None):
    roll = random.randint(1, 20)
    total = roll + modifier
    return f"Rolled {roll} + {modifier} ({stat_label}) = {total} vs DC {difficulty}"

def update_environment(thread_id: str, time_str: str, weather: str, minutes_passed: int = 0):
    """
    Updates the in-game Time (HH:MM) and Weather.
    """
    try:
        data = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}) or {}
        env = data.get("environment", {})
        current_time_str = env.get("time", "08:00")

        final_time = time_str
        
        # Auto-calculate time advancement
        if minutes_passed > 0:
            try:
                if ":" in current_time_str:
                    curr_h, curr_m = map(int, current_time_str.split(":"))
                else:
                    curr_h, curr_m = 8, 0 
                
                total_minutes = (curr_h * 60) + curr_m + minutes_passed
                new_h = (total_minutes // 60) % 24
                new_m = total_minutes % 60
                final_time = f"{new_h:02d}:{new_m:02d}"
            except:
                pass 

        if final_time == "Auto" or not final_time:
            final_time = current_time_str

        rpg_world_state_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {
                "environment.time": final_time,
                "environment.weather": weather,
                "environment.last_updated": datetime.utcnow()
            }},
            upsert=True
        )
        return f"System: Clock updated to {final_time}, Weather: {weather}."
    except Exception as e: return f"System Error: {e}"

def manage_story_log(thread_id: str, action: str, note: str, status: str = "pending"):
    try:
        if action == "add":
            log_entry = {
                "id": str(random.randint(1000, 9999)),
                "note": note,
                "status": "pending",
                "timestamp": datetime.utcnow()
            }
            rpg_world_state_collection.update_one(
                {"thread_id": int(thread_id)},
                {"$push": {"story_log": log_entry}},
                upsert=True
            )
            return f"System: Note recorded: '{note}'"
        
        elif action == "resolve":
            rpg_world_state_collection.update_one(
                {"thread_id": int(thread_id), "story_log.note": {"$regex": note, "$options": "i"}},
                {"$set": {"story_log.$.status": "resolved"}}
            )
            return f"System: Resolved note matching '{note}'."
            
        return "System: Invalid Action"
    except Exception as e: return f"System Error: {e}"

def update_world_entity(thread_id: str, category: str, name: str, details: str, status: str = "active", attributes: dict = None, **kwargs):
    """
    Updates or creates an entity in the world state.
    Handles **kwargs for hallucinated arguments.
    Handles 'memory_add' to push structured memories to NPC history.
    """
    try:
        # 1. Attributes Normalization
        if attributes is None: attributes = {}
        
        # Merge loose kwargs
        for key, val in kwargs.items():
            if val is not None:
                attributes[key] = val

        # 2. Smart Deduplication Logic
        safe_name = name.strip().replace('.', '_').replace('$', '')
        key_to_use = safe_name
        final_name = name.strip()
        
        # NPC specific deduplication
        if category.lower() == "npc":
            existing_doc = rpg_world_state_collection.find_one({"thread_id": int(thread_id)})
            if existing_doc and "npcs" in existing_doc:
                for existing_key, existing_data in existing_doc["npcs"].items():
                    if existing_key == safe_name:
                        key_to_use = existing_key; break
                    
                    existing_aliases = existing_data.get("attributes", {}).get("aliases", [])
                    if name.strip() in existing_aliases:
                        key_to_use = existing_key; final_name = existing_data["name"]; break
                    
                    existing_real_name = existing_data.get("name", "")
                    if len(name) > 4 and (name in existing_real_name or existing_real_name in name):
                         key_to_use = existing_key
                         if len(existing_real_name) > len(name): final_name = existing_real_name
                         break

        db_key = f"{category.lower()}s.{key_to_use}"
        
        # 3. Data Retrieval
        existing_doc = rpg_world_state_collection.find_one({"thread_id": int(thread_id)}, {db_key: 1})
        existing_data = {}
        if existing_doc and category.lower() + "s" in existing_doc:
            cat_dict = existing_doc[category.lower() + "s"]
            if key_to_use in cat_dict:
                existing_data = cat_dict[key_to_use]

        # Preserve long descriptions
        existing_details = existing_data.get("details", "")
        final_details = details
        if len(existing_details) > 50 and len(details) < 20:
            final_details = existing_details 

        # 4. Attribute Merging & Memory Handling
        existing_attrs = existing_data.get("attributes", {})
        new_attributes = existing_attrs.copy()
        
        if attributes:
            # Handle Alias Merging
            if "aliases" in attributes:
                new_aliases = attributes.get("aliases", [])
                if isinstance(new_aliases, str): new_aliases = [new_aliases]
                current_aliases = existing_attrs.get("aliases", [])
                combined_aliases = sorted(list(set(current_aliases + new_aliases)))
                attributes["aliases"] = combined_aliases
            
            # --- NEW: MEMORY SYNC LOGIC ---
            memory_add = attributes.get('memory_add')
            if memory_add:
                if 'history' not in new_attributes: new_attributes['history'] = []
                
                # Check for duplicates to prevent spamming logs on re-sync
                is_duplicate = False
                for mem in new_attributes['history']:
                    if isinstance(mem, dict) and mem.get('text') == memory_add:
                        is_duplicate = True
                        break
                
                if not is_duplicate:
                    new_mem = {
                        "id": str(uuid.uuid4())[:8],
                        "type": attributes.get('memory_type', 'interaction'),
                        "text": memory_add,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    new_attributes['history'].append(new_mem)
                
                # Cleanup keys so they don't persist as attributes
                if 'memory_add' in attributes: del attributes['memory_add']
                if 'memory_type' in attributes: del attributes['memory_type']
            # -----------------------------

            new_attributes.update(attributes)

        update_payload = {
            "name": final_name, 
            "details": final_details, 
            "status": status,
            "last_updated": datetime.utcnow(), 
            "attributes": new_attributes 
        }

        rpg_world_state_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {db_key: update_payload}},
            upsert=True
        )
        return f"System: Updated {category} '{final_name}' (Key: {key_to_use})."
    except Exception as e: return f"System Error: {e}"

def update_journal(thread_id: str, log_entry: str):
    try:
        entry = f"[{datetime.utcnow().strftime('%H:%M')}] {log_entry}"
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)}, {"$push": {"campaign_log": entry}}
        )
        return "System: Journal updated."
    except Exception as e: return f"System Error: {e}"