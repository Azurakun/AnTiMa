# cogs/ai_chat/utils.py
import discord
import logging

logger = logging.getLogger(__name__)

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