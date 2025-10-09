# cogs/ai_chat/utils.py
import discord
import logging
import os
import random
import aiohttp
from .utils import _safe_get_response_text

logger = logging.getLogger(__name__)

TENOR_API_KEY = os.environ.get("TENOR_API_KEY")
TENOR_CLIENT_KEY = "AnTiMa-Discord-Bot" # A client key for Tenor's analytics

async def get_gif_url(http_session: aiohttp.ClientSession, search_term: str) -> str | None:
    """Fetches a random GIF URL from Tenor based on a search term."""
    if not TENOR_API_KEY:
        logger.warning("TENOR_API_KEY is not set in environment variables. Cannot fetch GIFs.")
        return None

    url = "https://tenor.googleapis.com/v2/search"
    params = {
        "q": search_term,
        "key": TENOR_API_KEY,
        "client_key": TENOR_CLIENT_KEY,
        "limit": 8,
        "media_filter": "minimal",
        "random": "true"
    }

    try:
        async with http_session.get(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("results"):
                    gif = random.choice(data["results"])
                    return gif["media_formats"]["gif"]["url"]
            else:
                logger.error(f"Tenor API request failed with status {response.status}: {await response.text()}")
    except Exception as e:
        logger.error(f"An error occurred while fetching a GIF from Tenor: {e}")
    return None

async def should_send_gif(summarizer_model, channel, bot_response_text, gif_search_term) -> bool:
    """Uses an AI agent to determine if sending a GIF is appropriate for the context."""
    try:
        history = [msg async for msg in channel.history(limit=5)]
        history.reverse()

        conversation_log = "\n".join([f"{msg.author.display_name}: {msg.clean_content}" for msg in history])

        prompt = (
            "You are a social context analysis AI. Your job is to decide if sending a GIF is appropriate for the current conversation mood. "
            "I will provide the recent chat history, my planned text response, and the GIF I want to send (as a search term).\n\n"
            "**Rules for your decision:**\n"
            "1. **APPROVE (yes)** if the conversation is casual, friendly, or emotional where a GIF would enhance the expression (e.g., sharing joy, offering comfort, making a joke).\n"
            "2. **REJECT (no)** if the conversation is serious, technical, formal, or argumentative. A GIF would be inappropriate or distracting.\n"
            "3. **REJECT (no)** if the user seems frustrated or angry. A GIF could escalate the situation unless it's clearly apologetic.\n"
            "4. **REJECT (no)** if the GIF's implied emotion (from the search term) clashes badly with the text response (e.g., text is sad, GIF is 'laughing').\n\n"
            f"--- CONTEXT ---\n"
            f"**Recent Chat:**\n{conversation_log}\n\n"
            f"**My Planned Text Response:**\n\"{bot_response_text}\"\n\n"
            f"**Proposed GIF Search Term:** `{gif_search_term}`\n"
            f"---------------\n\n"
            "Based on your rules, is sending this GIF appropriate right now? Answer with only 'yes' or 'no'."
        )

        response = await summarizer_model.generate_content_async(prompt)
        decision = _safe_get_response_text(response).strip().lower()
        logger.info(f"GIF Decision Agent for search term '{gif_search_term}': '{decision}'")
        return 'yes' in decision

    except Exception as e:
        logger.error(f"GIF Decision Agent failed: {e}")
        return False # Default to not sending GIF on error

def _safe_get_response_text(response) -> str:
    """Safely gets text from a Gemini response, handling blocked content."""
    try:
        return response.text
    except (ValueError, IndexError):
        logger.warning("Gemini response was empty or blocked.")
        return ""

def _find_member(guild: discord.Guild, name: str):
    """Finds a member in a guild by name or display name, case-insensitively."""
    name = name.lower()
    member = discord.utils.find(
        lambda m: m.name.lower() == name or m.display_name.lower() == name,
        guild.members
    )
    if member is None:
        logger.warning(f"Could not find member '{name}' in guild '{guild.name}'. The member might not be cached or the name is incorrect.")
    return member