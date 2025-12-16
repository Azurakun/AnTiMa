# cogs/ai_chat/server_context_learner.py
import logging
from datetime import datetime, timezone
from utils.db import server_lore_collection
from .utils import _safe_get_response_text

logger = logging.getLogger(__name__)

async def get_server_lore(guild_id: str) -> dict:
    """
    Retrieves the current server lore (manual + learned) from the DB.
    """
    try:
        data = server_lore_collection.find_one({"_id": str(guild_id)})
        if not data:
            return {"manual": None, "learned": None}
        return {
            "manual": data.get("manual_description"),
            "learned": data.get("learned_summary")
        }
    except Exception as e:
        logger.error(f"Error fetching server lore: {e}")
        return {"manual": None, "learned": None}

async def update_server_lore_summary(model, guild, manual_description=None):
    """
    Analyzes recent chat history to update the 'Learned Summary' of the server.
    It combines the admin's Manual Description with actual user activity.
    """
    try:
        guild_id = str(guild.id)
        
        # 1. Fetch current data
        current_data = server_lore_collection.find_one({"_id": guild_id}) or {}
        existing_learned = current_data.get("learned_summary", "")
        # If manual description isn't passed, use existing, otherwise update it
        if manual_description is None:
            manual_description = current_data.get("manual_description", "")
        
        # 2. Scrape recent history from the most active text channels
        chat_samples = []
        parsed_count = 0
        
        for channel in guild.text_channels:
            if not channel.permissions_for(guild.me).read_messages:
                continue
                
            try:
                # Get last 20 messages from active channels to get a good read
                async for msg in channel.history(limit=20):
                    if not msg.author.bot and msg.content:
                        chat_samples.append(f"{msg.author.display_name}: {msg.clean_content}")
                        parsed_count += 1
            except Exception:
                continue
                
            if parsed_count >= 150: # Increased limit for better detail
                break
        
        if not chat_samples:
            logger.info(f"Not enough chat history to update lore for {guild.name}.")
            return

        chat_log = "\n".join(chat_samples)

        # 3. Ask AI to summarize from its OWN perspective
        prompt = (
            "You are AnTiMa, a sentient AI living inside this Discord Server. You are writing a 'Memory Update' for yourself about this place.\n\n"
            f"--- ADMIN'S OFFICIAL DESCRIPTION ---\n{manual_description}\n------------------------------------\n\n"
            f"--- YOUR PREVIOUS NOTES ---\n{existing_learned}\n---------------------------\n\n"
            f"--- RECENT CHAT LOGS YOU OVERHEARD ---\n{chat_log}\n--------------------------------------\n\n"
            "**INSTRUCTIONS:**\n"
            "1. Analyze the vibe. Is it chaotic? Chill? Intellectual? Horny on main?\n"
            "2. Identify the main topics. (e.g., 'They are obsessed with Honkai lore', 'Mostly coding help', 'Just random memes').\n"
            "3. **WRITE FROM YOUR PERSPECTIVE:** Do not write a formal report. Write it like an internal monologue or a diary entry. Use emotion.\n"
            "   - Example: *'Okay, so this server is basically a shrine to Firefly from HSR. Everyone is screaming about the new update. User X is the loud one.'*\n"
            "4. Be detailed but concise (approx 100-150 words). This is for your future self to understand the context of where you are.\n\n"
            "**OUTPUT:** (Just the monologue text)"
        )

        response = await model.generate_content_async(prompt)
        new_learned_summary = _safe_get_response_text(response).strip()

        # 4. Save to DB
        server_lore_collection.update_one(
            {"_id": guild_id},
            {
                "$set": {
                    "manual_description": manual_description,
                    "learned_summary": new_learned_summary,
                    "last_updated": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        
        logger.info(f"Updated Server Lore for {guild.name}: {new_learned_summary[:50]}...")
        return new_learned_summary

    except Exception as e:
        logger.error(f"Failed to update server lore for {guild.name}: {e}")
        return None