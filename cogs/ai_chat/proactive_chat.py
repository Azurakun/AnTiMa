# cogs/ai_chat/proactive_chat.py
import discord
from discord.ext import tasks
import logging
import random
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from utils.db import ai_config_collection, ai_memories_collection
from .response_handler import _find_member, _safe_get_response_text
from .memory_handler import load_user_memories
import re

logger = logging.getLogger(__name__)

@tasks.loop(hours=3)
async def proactive_chat_loop(cog):
    """Periodically and randomly initiates a conversation in a quiet, configured chat channel."""
    try:
        await asyncio.sleep(random.uniform(3600, 10800)) # Wait between 1 to 3 hours

        guild_configs = list(ai_config_collection.find({"channel": {"$exists": True, "$ne": None}}))
        if not guild_configs:
            return

        config = random.choice(guild_configs)
        guild = cog.bot.get_guild(int(config['_id']))
        channel = cog.bot.get_channel(config['channel'])

        if not guild or not channel:
            return

        if channel.last_message_id:
            try:
                last_message = await channel.fetch_message(channel.last_message_id)
                if last_message and (datetime.now(datetime.timezone.utc) - last_message.created_at).total_seconds() < 7200: # 2 hours
                    return
            except discord.NotFound:
                pass

        potential_users = set()
        async for msg in channel.history(limit=100):
            # Find users who have either been replied to by the bot or have mentioned the bot
            if msg.author == cog.bot.user and msg.reference:
                try:
                    replied_to_message = await channel.fetch_message(msg.reference.message_id)
                    if not replied_to_message.author.bot:
                        potential_users.add(replied_to_message.author)
                except (discord.NotFound, discord.HTTPException):
                    continue
            elif cog.bot.user in msg.mentions and not msg.author.bot:
                potential_users.add(msg.author)
        
        # Filter out offline members
        online_users = [user for user in potential_users if isinstance(user, discord.Member) and user.status != discord.Status.offline]

        if not online_users:
            logger.info("Proactive chat: No recent, online users found to interact with.")
            return
        
        target_user = random.choice(online_users)
        logger.info(f"Proactive chat: Attempting to start a conversation with {target_user.name} in {guild.name}.")

        await _initiate_conversation(cog, channel, target_user)

    except Exception as e:
        logger.error(f"An error occurred in the proactive chat loop: {e}", exc_info=True)

@proactive_chat_loop.before_loop
async def before_proactive_chat_loop(cog):
    await cog.bot.wait_until_ready()

async def _initiate_conversation(cog, channel: discord.TextChannel, user: discord.Member) -> tuple[bool, str]:
    """Uses the AI to generate and send a conversation starter. Returns a tuple of (success, reason)."""
    try:
        memory_summary = await load_user_memories(user.id)
        
        # Add current time to the prompt
        now_gmt7 = datetime.now(ZoneInfo("Asia/Jakarta"))
        time_str = now_gmt7.strftime("%A, %B %d, %Y at %I:%M %p GMT+7")

        if memory_summary:
            prompt = (
                f"The current time is {time_str}. You are feeling a bit bored or reflective and want to start a casual conversation with a user named '{user.display_name}'. "
                "You remember some things about them. Based on the memories below, craft a natural-sounding conversation starter. "
                "It could be a question about something you discussed before, a follow-up, or just a random thought related to them. "
                "Keep it chill and not too intense. Remember to tag them using `[MENTION: {user.display_name}]`.\n\n"
                f"--- YOUR MEMORIES OF {user.display_name} ---\n{memory_summary}\n---"
            )
        else:
            # Fallback prompt if there are no memories
            prompt = (
                f"The current time is {time_str}. You want to start a random, casual conversation with '{user.display_name}' to make the chat more active. "
                "Ask a fun, open-ended question. For example, you could ask about their favorite game, what they had for lunch, or what they're currently listening to. "
                "Keep it friendly and engaging. Remember to tag them using `[MENTION: {user.display_name}]`."
            )
        
        async with channel.typing():
            response = await cog.model.generate_content_async(prompt)
            starter_text = _safe_get_response_text(response)

            if not starter_text:
                logger.warning("Proactive chat: AI generated an empty conversation starter.")
                return False, "AI failed to generate a message."
            
            def repl(match):
                name = match.group(1).strip()
                member = _find_member(channel.guild, name)
                return f"<@{member.id}>" if member else name
            processed_text = re.sub(r"\[MENTION: (.+?)\]", repl, starter_text)

            await channel.send(processed_text, allowed_mentions=discord.AllowedMentions(users=True))
            logger.info(f"Proactive chat: Sent a conversation starter to {user.name} in #{channel.name}.")
            return True, "Success"

    except Exception as e:
        logger.error(f"Failed to initiate conversation with {user.name}: {e}")
        return False, f"An internal error occurred: {e}"