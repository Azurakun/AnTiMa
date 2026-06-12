# cogs/rpg_system/tools.py
import random
import uuid
from datetime import datetime
from utils.db import rpg_sessions_collection, rpg_inventory_collection, rpg_world_state_collection

def grant_item_to_player(user_id: str, item_name: str, description: str):
    """Adds an item to the player's permanent inventory."""
    try:
        rpg_inventory_collection.update_one(
            {"user_id": int(user_id)},
            {"$push": {"items": {"name": item_name, "description": description}}},
            upsert=True
        )
        return f"System: Granted '{item_name}' to player."
    except Exception as e:
        return f"System Error: {e}"

def update_player_stats(thread_id: str, user_id: str, hp_change: int, mp_change: int):
    """Updates the HP and MP of a player."""
    try:
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session: 
            return "Session not found."
        
        stats = session.get("player_stats", {}).get(str(user_id))
        if not stats: 
            return "Player not found in session."
        
        new_hp = max(0, min(stats.get("max_hp", 100), stats.get("hp", 100) + hp_change))
        new_mp = max(0, min(stats.get("max_mp", 50), stats.get("mp", 50) + mp_change))
        
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {
                f"player_stats.{user_id}.hp": new_hp,
                f"player_stats.{user_id}.mp": new_mp
            }}
        )
        return f"System: Player stats updated. HP: {new_hp}, MP: {new_mp}."
    except Exception as e:
        return f"System Error: {e}"

def apply_damage(thread_id: str, user_id: str, damage_amount: int):
    """Applies damage to a player, reducing their HP."""
    return update_player_stats(thread_id, user_id, hp_change=-int(damage_amount), mp_change=0)

def apply_healing(thread_id: str, user_id: str, heal_amount: int):
    """Applies healing to a player, increasing their HP."""
    return update_player_stats(thread_id, user_id, hp_change=int(heal_amount), mp_change=0)

def deduct_mana(thread_id: str, user_id: str, mana_cost: int):
    """Deducts mana from a player."""
    return update_player_stats(thread_id, user_id, hp_change=0, mp_change=-int(mana_cost))

def roll_d20(check_type: str, difficulty: int, modifier: int = 0, stat_label: str = None):
    """Simulates rolling a 20-sided die for skill checks."""
    roll = random.randint(1, 20)
    total = roll + modifier
    success = total >= difficulty
    return f"Roll: {roll}, Total: {total}, DC: {difficulty}, Success: {success}"

def update_environment(thread_id: str, time_str: str, weather: str, minutes_passed: int = 0):
    """Updates the world's time and weather."""
    try:
        rpg_world_state_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {
                "environment.time": time_str,
                "environment.weather": weather
            }},
            upsert=True
        )
        return f"System: Environment updated to Time: {time_str}, Weather: {weather}."
    except Exception as e:
        return f"System Error: {e}"

def manage_story_log(thread_id: str, action: str, note: str, status: str = "pending"):
    """Manages the ongoing story events or pending actions."""
    try:
        log_entry = {
            "id": str(uuid.uuid4())[:8],
            "action": action,
            "note": note,
            "status": status,
            "timestamp": datetime.utcnow().isoformat()
        }
        rpg_world_state_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$push": {"story_log": log_entry}},
            upsert=True
        )
        return f"System: Story log updated with action '{action}'."
    except Exception as e:
        return f"System Error: {e}"

def update_world_entity(thread_id: str, category: str, name: str, details: str = None, status: str = "active", attributes: dict = None, memory_add: str = None, age: str = None, **kwargs):
    """
    Updates or creates an entity. Handles 'memory_add' to push memories to an NPC's history natively.
    """
    try:
        if attributes is None: attributes = {}
        
        # Ensures safe conversion of attributes to a standard dictionary
        if hasattr(attributes, 'items'):
            try:
                attributes = dict(attributes)
            except Exception:
                safe_attr = {}
                for k in getattr(attributes, 'keys', lambda: [])():
                    try: safe_attr[k] = attributes[k]
                    except Exception: pass
                attributes = safe_attr
        for key, val in kwargs.items():
            if val is not None: attributes[key] = val

        if age is not None:
            attributes["age"] = age

        safe_name = name.strip().replace('.', '_').replace('$', '')
        db_key = f"{category.lower()}s.{safe_name}"

        existing_data = (rpg_world_state_collection.find_one({"thread_id": int(thread_id)}, {db_key: 1}) or {}).get(category.lower() + "s", {}).get(safe_name, {})
        
        final_details = details if details is not None else existing_data.get("details", "")
        new_attributes = existing_data.get("attributes", {})
        
        # Handle 'memory_add' parameter safely to prevent KeyError
        mem_to_add = memory_add or attributes.pop('memory_add', None)
        
        if mem_to_add and category.lower() == 'npc':
            if 'history' not in new_attributes: new_attributes['history'] = []
            if not any(mem.get('text') == mem_to_add for mem in new_attributes['history']):
                new_attributes['history'].append({
                    "id": str(uuid.uuid4())[:8],
                    "text": mem_to_add,
                    "timestamp": datetime.utcnow().isoformat()
                })
        new_attributes.update(attributes)

        update_payload = {
            "name": name.strip(), "details": final_details, "status": status,
            "last_updated": datetime.utcnow(), "attributes": new_attributes 
        }

        rpg_world_state_collection.update_one(
            {"thread_id": int(thread_id)}, {"$set": {db_key: update_payload}}, upsert=True
        )
        return f"System: Updated {category} '{name.strip()}'."
    except Exception as e: 
        return f"System Error: {e}"

def update_journal(thread_id: str, log_entry: str):
    """Adds a new entry into the adventure journal."""
    try:
        entry = {
            "id": str(uuid.uuid4())[:8],
            "text": log_entry,
            "timestamp": datetime.utcnow().isoformat()
        }
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$push": {"campaign_log": entry}}
        )
        return "System: Journal updated successfully."
    except Exception as e:
        return f"System Error: {e}"

def propose_actions(actions: list[str]):
    """Propose a list of 2-4 distinct, relevant actions for the current player."""
    return f"System: Proposed actions received: {', '.join(actions)}"