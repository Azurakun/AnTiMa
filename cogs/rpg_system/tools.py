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
    """
    Simulates a D20 dice roll against a difficulty class (DC).
    Args:
        check_type: The name of the action (e.g., "Attack", "Persuasion").
        difficulty: The Target Number (DC).
        modifier: The bonus/penalty.
    """
    roll = random.randint(1, 20)
    total = roll + modifier
    return f"Rolled {roll} + {modifier} ({stat_label}) = {total} vs DC {difficulty}"

def update_world_entity(thread_id: str, category: str, name: str, details: str, status: str = "active"):
    """
    Updates the World Sheet (JSON Database) for NPCs, Locations, or Lore.
    Use this to remember detailed info about people and places.
    
    Args:
        category: "NPC", "Location", "Quest", or "Lore"
        name: The name of the entity (e.g., "Grom the Goblin", "Darkwood Tavern").
        details: Detailed description. MUST follow the prompt's formatting guidelines (Race, Gender, App, etc.)
        status: "active" (currently relevant) or "inactive" (moved to background).
    """
    try:
        # Sanitize keys to prevent MongoDB dot-notation errors
        safe_name = name.replace('.', '_').replace('$', '')
        key = f"{category.lower()}s.{safe_name}" 
        
        rpg_world_state_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {
                key: {
                    "name": name,
                    "details": details,
                    "status": status,
                    "last_updated": datetime.utcnow()
                }
            }},
            upsert=True
        )
        return f"System: Updated World Sheet for [{category}] {name}."
    except Exception as e: return f"System Error: {e}"

def update_journal(thread_id: str, log_entry: str):
    """Updates the simple chronological campaign log."""
    try:
        entry = f"[{datetime.utcnow().strftime('%H:%M')}] {log_entry}"
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)}, 
            {"$push": {"campaign_log": entry}}
        )
        return "System: Journal updated."
    except Exception as e: return f"System Error: {e}"