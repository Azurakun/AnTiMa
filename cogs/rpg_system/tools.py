# cogs/rpg_system/tools.py
import random
from datetime import datetime
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
    """Updates HP/MP in the active session."""
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

def update_world_entity(thread_id: str, category: str, name: str, details: str, status: str = "active", attributes: dict = None):
    """
    Updates the World Sheet.
    Merges attributes and appends aliases/details if the entity exists.
    """
    try:
        # Standardize key generation to prevent "Arthur " vs "Arthur"
        safe_name = name.strip().replace('.', '_').replace('$', '')
        key = f"{category.lower()}s.{safe_name}" 
        
        # 1. Fetch existing data to merge attributes safely
        existing_doc = rpg_world_state_collection.find_one(
            {"thread_id": int(thread_id)}, 
            {key: 1}
        )
        
        existing_attrs = {}
        existing_details = ""
        
        if existing_doc and category.lower() + "s" in existing_doc:
            cat_dict = existing_doc[category.lower() + "s"]
            if safe_name in cat_dict:
                entity_data = cat_dict[safe_name]
                existing_attrs = entity_data.get("attributes", {})
                existing_details = entity_data.get("details", "")

        # 2. Merge Attributes
        new_attributes = existing_attrs.copy()
        if attributes:
            new_attributes.update(attributes)

        # 3. Handle Details (Append Alias info if not duplicate)
        # If the new details are short (just a status update), we might not want to overwrite a long bio.
        # However, the user specifically asked to put aliases in details.
        # We'll allow the Scribe's output to take precedence, but we can do a smart merge if needed.
        # For now, we trust the Scribe to provide the full summary including aliases.
        final_details = details

        update_payload = {
            "name": name.strip(), # Ensure clean display name
            "details": final_details,
            "status": status,
            "last_updated": datetime.utcnow(),
            "attributes": new_attributes 
        }

        rpg_world_state_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {key: update_payload}},
            upsert=True
        )
        return f"System: Updated {category} '{name}'."
    except Exception as e: return f"System Error: {e}"

def update_journal(thread_id: str, log_entry: str):
    try:
        entry = f"[{datetime.utcnow().strftime('%H:%M')}] {log_entry}"
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)}, 
            {"$push": {"campaign_log": entry}}
        )
        return "System: Journal updated."
    except Exception as e: return f"System Error: {e}"