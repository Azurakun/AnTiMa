# cogs/rpg_system/utils.py
import re
import asyncio
import discord
from datetime import datetime
from utils.db import db

class RPGLogger:
    @staticmethod
    def log(thread_id, level, message, details=None):
        """Logs to database for the Web Inspector."""
        try:
            # Ensure details are JSON serializable (basic check)
            if details:
                details = {k: str(v) for k, v in details.items()}
            
            db.rpg_debug_terminal.insert_one({
                "thread_id": str(thread_id),
                "timestamp": datetime.utcnow(),
                "level": level,  
                "message": message,
                "details": details or {}
            })
        except Exception as e:
            print(f"Log Error: {e}")

    @staticmethod
    async def broadcast(thread_id, phase, content, data=None):
        """Sugar for logging a thought process."""
        RPGLogger.log(thread_id, "thought_process", phase, {"content": content, "data": data})

def sanitize_age(age_input):
    """Normalizes age input into a string."""
    if not age_input: return "Unknown"
    s = str(age_input).strip().lower()
    if re.search(r'\d', s):
        clean = re.search(r'[\d\-\s\+<>]+', s)
        return clean.group(0).strip() if clean else s
    mapping = {
        "child": "10", "kid": "10", "young": "10",
        "teen": "16", "adult": "30", "middle": "45",
        "old": "70", "elder": "70", "ancient": "70"
    }
    for k, v in mapping.items():
        if k in s: return v
    return "Unknown"

class ThinkingAnimator:
    """Manages the 'Thinking...' message animation."""
    def __init__(self, message: discord.Message):
        self.message = message
        self.task = None
        self._phases = [
            "🧠 **Consulting the Archives...**",
            "🌍 **Analyzing World State...**",
            "🎲 **Calculating Probabilities...**",
            "⚡ **Determining Consequences...**",
            "📝 **Drafting Narrative...**",
            "✨ **Polishing Scene...**"
        ]

    def start(self):
        self.task = asyncio.create_task(self._animate())

    def stop(self):
        if self.task: self.task.cancel()

    async def _animate(self):
        i = 0
        try:
            while True:
                await asyncio.sleep(4)
                i = (i + 1) % len(self._phases)
                try: await self.message.edit(content=self._phases[i])
                except: break
        except asyncio.CancelledError:
            pass