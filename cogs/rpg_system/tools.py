# cogs/rpg_system/tools.py
import random
import uuid
from datetime import datetime
from google.generativeai.tool import tool
from utils.db import rpg_sessions_collection, rpg_inventory_collection, rpg_world_state_collection

@tool
def grant_item_to_player(user_id: str, item_name: str, description: str):
    """Adds an item to the player's permanent inventory."""
    # Implementation ...

@tool
def update_player_stats(thread_id: str, user_id: str, hp_change: int, mp_change: int):
    # Implementation ...

@tool
def apply_damage(thread_id: str, user_id: str, damage_amount: int):
    return update_player_stats(thread_id, user_id, hp_change=-int(damage_amount), mp_change=0)

@tool
def apply_healing(thread_id: str, user_id: str, heal_amount: int):
    return update_player_stats(thread_id, user_id, hp_change=int(heal_amount), mp_change=0)

@tool
def deduct_mana(thread_id: str, user_id: str, mana_cost: int):
    return update_player_stats(thread_id, user_id, hp_change=0, mp_change=-int(mana_cost))

@tool
def roll_d20(check_type: str, difficulty: int, modifier: int = 0, stat_label: str = None):
    # Implementation ...

@tool
def update_environment(thread_id: str, time_str: str, weather: str, minutes_passed: int = 0):
    # Implementation ...

@tool
def manage_story_log(thread_id: str, action: str, note: str, status: str = "pending"):
    # Implementation ...

@tool
def update_world_entity(thread_id: str, category: str, name: str, details: str = None, status: str = "active", attributes: dict = None, **kwargs):
    """
    Updates or creates an entity. Handles 'memory_add' to push memories to an NPC's history.
    """
    try:
        if attributes is None: attributes = {}
        for key, val in kwargs.items():
            if val is not None: attributes[key] = val

        safe_name = name.strip().replace('.', '_').replace('$', '')
        db_key = f"{category.lower()}s.{safe_name}"

        existing_data = (rpg_world_state_collection.find_one({"thread_id": int(thread_id)}, {db_key: 1}) or {}).get(category.lower() + "s", {}).get(safe_name, {})
        
        final_details = details if details is not None else existing_data.get("details", "")

        new_attributes = existing_data.get("attributes", {})
        if attributes:
            memory_add = attributes.pop('memory_add', None)
            if memory_add and category.lower() == 'npc':
                if 'history' not in new_attributes: new_attributes['history'] = []
                if not any(mem.get('text') == memory_add for mem in new_attributes['history']):
                    new_attributes['history'].append({
                        "id": str(uuid.uuid4())[:8],
                        "text": memory_add,
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
    except Exception as e: return f"System Error: {e}"

@tool
def update_journal(thread_id: str, log_entry: str):
    # Implementation ...

@tool
def propose_actions(actions: list[str]):
    """Propose a list of 2-4 distinct, relevant actions for the current player."""
    # This is a stub for the AI model to call.
    pass
