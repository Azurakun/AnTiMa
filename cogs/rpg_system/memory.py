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

    def save_turn(self, thread_id, user_name, user_input, ai_output):
        """Permanently saves the turn interaction to the database (UTC for storage)."""
        entry = {
            "timestamp": datetime.utcnow(),
            "user_name": user_name,
            "input": user_input,
            "output": ai_output
        }
        rpg_sessions_collection.update_one(
            {"thread_id": int(thread_id)},
            {"$push": {"turn_history": entry}}
        )

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
        Uses the owner's timezone for the 'WORLD DATE'.
        """
        owner_id = session_data.get('owner_id')
        # Format the date according to the user's timezone (or default GMT+7)
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

    async def get_token_count_and_footer(self, chat_session):
        try:
            if not chat_session.history: return "🧠 Memory: 0 Tokens"
            count_result = await self.model.count_tokens_async(chat_session.history)
            used = count_result.total_tokens
            percent = (used / self.max_tokens) * 100
            return f"🧠 Memory: {used:,} / {self.max_tokens:,} Tokens ({percent:.1f}%) | 💾 Auto-Saved"
        except Exception as e:
            return "🧠 Memory: Calc Error"