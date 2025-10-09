# cogs/ai_chat/utils.py
import discord
import logging
import os
import random
import aiohttp

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
                return None
    except Exception as e:
        logger.error(f"An error occurred while fetching a GIF from Tenor: {e}")
        return None

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