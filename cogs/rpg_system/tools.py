# cogs/rpg_system/tools.py
import random
from datetime import datetime
from utils.db import rpg_sessions_collection, rpg_inventory_collection

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

def roll_d20(check_type: str, difficulty: int):
    return random.randint(1, 20) 

def update_journal(thread_id: str, log_entry: str, npc_update: str = None, quest_update: str = None):
    """Updates the campaign log and world state."""
    try:
        updates = {}
        if log_entry:
            entry = f"[{datetime.utcnow().strftime('%H:%M')}] {log_entry}"
            updates["$push"] = {"campaign_log": entry}
        if npc_update:
            if "$push" not in updates: updates["$push"] = {}
            updates["$push"]["npc_registry"] = npc_update
        if quest_update:
            if "$push" not in updates: updates["$push"] = {}
            updates["$push"]["quest_log"] = quest_update
        
        if updates:
            rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, updates)
            return "System: Journal updated."
        return "System: No updates."
    except Exception as e: return f"System Error: {e}"