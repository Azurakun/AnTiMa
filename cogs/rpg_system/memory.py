# cogs/rpg_system/memory.py
import discord
from datetime import datetime
from utils.db import rpg_sessions_collection
from utils.timezone_manager import get_local_time
import google.generativeai as genai

class RPGContextManager:
    def __init__(self, model):
        self.model = model
        self.max_tokens = 1_000_000

    def _format_list(self, title, items, empty_text="None"):
        if not items: return f"**{title}:** {empty_text}"
        unique_items = list(dict.fromkeys(items))
        content = "\n".join([f"- {item}" for item in unique_items])
        return f"**{title}:**\n{content}"

    def save_turn(self, thread_id, user_name, user_input, ai_output, user_message_id=None, bot_message_id=None):
        """
        Permanently saves the turn interaction to the database.
        Args:
            bot_message_id: Can be a single ID (int) or a list of IDs (list) if the response was chunked.
        """
        entry = {
            "timestamp": datetime.utcnow(),
            "user_name": user_name,
            "input": user_input,
            "output": ai_output,
            "user_message_id": user_message_id,
            "bot_message_id": bot_message_id 
        }
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$push": {"turn_history": entry}}
        )

    def delete_last_turn(self, thread_id):
        """Removes the last turn from the database history (used for single reroll)."""
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$pop": {"turn_history": 1}}
        )

    def trim_history(self, thread_id, target_index):
        """
        Slices the history to keep only turns up to target_index (0-based exclusive).
        Example: target_index=3 means keep indices 0, 1, 2.
        """
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session or "turn_history" not in session: return
        
        full_history = session["turn_history"]
        # Ensure we don't trim more than exists or invalid amounts
        if target_index < 0: target_index = 0
        if target_index >= len(full_history): return # Nothing to trim
        
        new_history = full_history[:target_index]
        
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$set": {"turn_history": new_history}}
        )
        return full_history[target_index:] # Return the deleted turns so we can wipe messages

    def load_full_history(self, session_data):
        """Reconstructs the entire story from the database logs."""
        history = session_data.get("turn_history", [])
        if not history: return "The adventure is just beginning."
        text_log = []
        for turn in history:
            text_log.append(f"[{turn['user_name']}]: {turn['input']}")
            text_log.append(f"[Dungeon Master]: {turn['output']}")
        return "\n\n".join(text_log)

    def build_context_block(self, session_data):
        """
        Aggregates ALL persistent data into a structured prompt block.
        """
        owner_id = session_data.get('owner_id')
        local_time_str = get_local_time(owner_id, fmt="%Y-%m-%d %H:%M %Z") if owner_id else "Unknown Date"

        quests = session_data.get("quest_log", [])
        npcs = session_data.get("npc_registry", [])
        campaign_summary = session_data.get("campaign_log", [])
        full_story_text = self.load_full_history(session_data)

        context = (
            f"=== 🧠 SYSTEM MEMORY START ===\n"
            f"**WORLD DATE (IRL):** {local_time_str}\n"
            f"**SCENARIO:** {session_data.get('scenario_type', 'Unknown')}\n\n"
            f"{self._format_list('ACTIVE QUESTS', quests, 'No active quests.')}\n\n"
            f"{self._format_list('KNOWN NPCs', npcs, 'No NPCs recorded.')}\n\n"
            f"**CAMPAIGN SUMMARY (Key Events):**\n"
            f"{self._format_list('Log', campaign_summary, 'No key events logged.')}\n\n"
            f"**FULL STORY HISTORY (Chronological):**\n"
            f"{full_story_text}\n"
            f"=== SYSTEM MEMORY END ==="
        )
        return context

    async def get_token_count_and_footer(self, chat_session, turn_id=None):
        try:
            if not chat_session.history: return "🧠 Memory: 0 Tokens"
            count_result = await self.model.count_tokens_async(chat_session.history)
            used = count_result.total_tokens
            percent = (used / self.max_tokens) * 100
            
            # Display Turn ID if provided
            turn_str = f" | 📜 Turn {turn_id}" if turn_id else ""
            
            return f"🧠 Memory: {used:,} / {self.max_tokens:,} Tokens ({percent:.1f}%){turn_str} | 💾 Auto-Saved"
        except Exception as e:
            return "🧠 Memory: Calc Error"