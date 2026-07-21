# cogs/rpg_system/utils.py
import discord
import asyncio
import time
import re
from datetime import datetime

class RPGLogger:
    @staticmethod
    def log(thread_id, level, message, details=None):
        print(f"[{level.upper()}] Thread {thread_id}: {message}")
        if details:
            print(f"   Details: {details}")

    @staticmethod
    async def broadcast(thread_id, event_type, message, data=None):
        # Optional: Hook for a Web Dashboard or Console Websocket
        pass

class StatusManager:
    """
    Manages the 'Thinking...' message with dynamic updates.
    Includes a debouncer to prevent hitting Discord API rate limits (5 updates/5s).
    """
    def __init__(self, message):
        self.message = message
        self.last_update = 0
        self.current_text = ""
        self._task = None

    async def set(self, text):
        """Request a status update. Handles rate limiting automatically."""
        self.current_text = text
        now = time.time()
        
        # If we haven't updated in 1.5s, update immediately
        if now - self.last_update > 1.5:
            await self._do_update()
        else:
            # Otherwise, schedule a background update if one isn't already pending
            if not self._task or self._task.done():
                self._task = asyncio.create_task(self._delayed_update())

    async def _delayed_update(self):
        # Wait the remaining time to satisfy the 1.5s window
        delay = 1.5 - (time.time() - self.last_update)
        if delay > 0: await asyncio.sleep(delay)
        await self._do_update()

    async def _do_update(self):
        try:
            await self.message.edit(content=f"🧠 **{self.current_text}**")
            self.last_update = time.time()
        except (discord.NotFound, discord.Forbidden):
            pass # Message was deleted or we lost permissions
        except Exception as e:
            print(f"Status Update Error: {e}")

    async def delete(self):
        """Clean up the status message."""
        if self._task: self._task.cancel()
        try:
            await self.message.delete()
        except:
            pass

def sanitize_age(age_input):
    """Sanitizes age input to ensure it's a valid string/int."""
    if not age_input: return "Unknown"
    return str(age_input)[:10]

def parse_narrative_response(text: str) -> tuple[str, list[str]]:
    """
    Parses the narrative text and suggested choices from an XML-like response.
    Returns:
        tuple[narrative_text, list_of_choices]
    """
    narrative = ""
    choices = []
    
    # Try parsing <narrative> tag
    narrative_match = re.search(r"<narrative>(.*?)</narrative>", text, re.DOTALL | re.IGNORECASE)
    if narrative_match:
        narrative = narrative_match.group(1).strip()
    else:
        # Fallback if tag is missing: strip off suggested_actions if present
        if "<suggested_actions>" in text:
            narrative = text.split("<suggested_actions>")[0].strip()
        else:
            narrative = text.strip()
            
    # Try parsing <suggested_actions> tag
    choices_match = re.search(r"<suggested_actions>(.*?)</suggested_actions>", text, re.DOTALL | re.IGNORECASE)
    if choices_match:
        choices_block = choices_match.group(1).strip()
        # Parse bullet points (e.g. "- Action" or "* Action" or "1. Action")
        for line in choices_block.split("\n"):
            line = re.sub(r"^[-*#\s\d.]+", "", line).strip()
            if line:
                choices.append(line)
                
    return narrative, choices