# cogs/ai_chat/personality_updater.py
import discord
from discord.ext import tasks
import logging
from utils.db import ai_config_collection, ai_personal_memories_collection
from .utils import _safe_get_response_text

logger = logging.getLogger(__name__)

# This loop runs once every 24 hours to update personalities for all configured guilds.
@tasks.loop(hours=24)
async def personality_update_loop(cog):
    logger.info("Starting daily personality adaptation task...")
    try:
        guild_configs = ai_config_collection.find({"channel": {"$exists": True, "$ne": None}})
        for config in guild_configs:
            guild_id = int(config["_id"])
            guild = cog.bot.get_guild(guild_id)
            if guild:
                await update_guild_personality(cog.summarizer_model, guild)
    except Exception as e:
        logger.error(f"Error in personality_update_loop: {e}")
    logger.info("Daily personality adaptation task finished.")

async def update_guild_personality(summarizer_model, guild: discord.Guild):
    """Fetches recent memories from a guild and generates an updated style guide."""
    logger.info(f"Updating personality for guild: {guild.name} ({guild.id})")
    
    # Fetch a sample of recent personal memories from this guild
    pipeline = [
        {"$match": {"guild_id": guild.id}},
        {"$sort": {"timestamp": -1}},
        {"$limit": 50}, # Use last 50 memories as a sample
        {"$project": {"summary": 1, "_id": 0}}
    ]
    memories_cursor = ai_personal_memories_collection.aggregate(pipeline)
    memories = [mem['summary'] for mem in memories_cursor]

    if len(memories) < 10: # Don't update if there's not enough recent interaction
        logger.info(f"Not enough memories for guild {guild.name} ({len(memories)}). Skipping personality update.")
        return

    memories_str = "\n".join(f"- {mem}" for mem in memories)

    prompt = (
        "You are a personality synthesis AI. Your name is AnTiMa. Based on the following memories of my conversations from a specific Discord server, generate a short, 3-4 bullet point 'Style Guide' that captures the server's unique vibe. This guide will help me adapt my responses to fit in better.\n"
        "Focus on:\n"
        "- Common topics of interest (e.g., gaming, coding, specific anime).\n"
        "- The general mood (e.g., sarcastic, wholesome, chaotic, serious).\n"
        "- The type of language used (e.g., lots of slang, formal, emoji-heavy).\n"
        "**Important**: Keep the guide concise, impersonal, and focused on behavioral advice. Do not mention specific user names. Start each point with a dash.\n\n"
        f"--- RECENT CONVERSATION MEMORIES ---\n{memories_str}\n---\n\n"
        "ADAPTIVE STYLE GUIDE:"
    )

    try:
        response = await summarizer_model.generate_content_async(prompt)
        style_guide = _safe_get_response_text(response)

        if style_guide:
            ai_config_collection.update_one(
                {"_id": str(guild.id)},
                {"$set": {"personality_style_guide": style_guide}},
                upsert=True
            )
            logger.info(f"Successfully updated personality style guide for guild {guild.name}.")
        else:
            logger.warning(f"Personality synthesis for guild {guild.name} generated an empty response.")

    except Exception as e:
        logger.error(f"Failed to update personality for guild {guild.id}: {e}")

@personality_update_loop.before_loop
async def before_personality_update_loop(cog):
    await cog.bot.wait_until_ready()