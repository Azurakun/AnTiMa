# cogs/ai_chat/proactive_chat.py
import discord
from discord.ext import tasks
import logging
import random
import asyncio
import re
from datetime import datetime, timedelta # Added timedelta
from zoneinfo import ZoneInfo
from utils.db import ai_config_collection
from ..utils import _find_member, _safe_get_response_text # <-- UPDATED IMPORT
from ..memory_handler import load_user_memories

logger = logging.getLogger(__name__)

# --- CHANGE THIS LINE TO MODIFY THE INTERVAL ---
@tasks.loop(minutes=5) # Changed from hours=3 to minutes=5
async def proactive_chat_loop(cog):
    """Periodically and randomly initiates a conversation in a quiet, configured chat channel."""
    try:
        # Removed the long asyncio.sleep(random.uniform(3600, 10800))

        guild_configs = list(ai_config_collection.find({"channel": {"$exists": True, "$ne": None}}))
        if not guild_configs:
            # logger.info("Proactive chat: No guilds configured with a chat channel.")
            return

        config = random.choice(guild_configs)
        guild = cog.bot.get_guild(int(config['_id']))
        # Ensure channel ID is treated as int
        channel_id = config.get('channel')
        if not channel_id:
            # logger.warning(f"Proactive chat: Guild {config.get('_id')} has no channel ID set.")
            return
        channel = cog.bot.get_channel(int(channel_id)) # Cast to int

        if not guild or not channel:
            # logger.warning(f"Proactive chat: Could not find guild {config.get('_id')} or channel {channel_id}.")
            return

        # Check if the channel has been inactive for a shorter period (e.g., 15 minutes)
        # to avoid interrupting recent conversations
        if channel.last_message_id:
            try:
                last_message = await channel.fetch_message(channel.last_message_id)
                # Check if last message exists and is older than 15 minutes
                if last_message and (datetime.now(datetime.timezone.utc) - last_message.created_at) < timedelta(minutes=15):
                    # logger.info(f"Proactive chat: Channel #{channel.name} in {guild.name} is recently active. Skipping.")
                    return
            except discord.NotFound:
                # No last message found, channel is likely empty, proceed.
                pass
            except Exception as e:
                logger.error(f"Proactive chat: Error fetching last message in #{channel.name}: {e}")
                return # Don't proceed if there's an error checking activity

        potential_users = set()
        async for msg in channel.history(limit=50): # Reduced history limit slightly
            # Find users who have either been replied to by the bot or have mentioned the bot
            # Ensure the author is a Member object before adding
            if msg.author == cog.bot.user and msg.reference:
                try:
                    # Make sure replied_to_message exists before accessing author
                    replied_to_message = msg.reference.resolved or await channel.fetch_message(msg.reference.message_id)
                    if replied_to_message and not replied_to_message.author.bot and isinstance(replied_to_message.author, discord.Member):
                         potential_users.add(replied_to_message.author)
                except (discord.NotFound, discord.HTTPException):
                    continue # Skip if the replied message can't be found
            elif cog.bot.user in msg.mentions and not msg.author.bot and isinstance(msg.author, discord.Member):
                 potential_users.add(msg.author)

        # Filter out offline members and ensure they are still in the guild
        online_users = [
            member for member in potential_users
            if member.status != discord.Status.offline and guild.get_member(member.id) is not None
        ]


        if not online_users:
            # logger.info(f"Proactive chat: No recent, online users found to interact with in #{channel.name}.")
            return

        target_user = random.choice(online_users)
        logger.info(f"Proactive chat: Attempting to start a conversation with {target_user.name} in {guild.name} (#{channel.name}).")

        await _initiate_conversation(cog, channel, target_user)

    except Exception as e:
        logger.error(f"An error occurred in the proactive chat loop: {e}", exc_info=True)

@proactive_chat_loop.before_loop
async def before_proactive_chat_loop(cog):
    await cog.bot.wait_until_ready()

async def _initiate_conversation(cog, channel: discord.TextChannel, user: discord.Member) -> tuple[bool, str]:
    """Uses the AI to generate and send a conversation starter. Returns a tuple of (success, reason)."""
    try:
        # Load user-specific memories for the target guild
        memory_summary = await load_user_memories(user.id, channel.guild.id) # Added guild_id

        now_gmt7 = datetime.now(ZoneInfo("Asia/Jakarta")) # Assuming WIB is desired
        time_str = now_gmt7.strftime("%A, %B %d, %Y at %I:%M %p %Z") # Added %Z for timezone

        if memory_summary:
            prompt = (
                f"The current time is {time_str}. You are feeling a bit bored or reflective and want to start a casual conversation with a user named '{user.display_name}'. "
                "You remember some things about them from this server. Based on the memories below, craft a natural-sounding conversation starter. "
                "Keep it chill and not too intense. Remember to tag them using `[MENTION: {user.display_name}]`.\n\n"
                f"--- YOUR MEMORIES OF {user.display_name} IN THIS SERVER ---\n{memory_summary}\n---"
            )
        else:
            prompt = (
                f"The current time is {time_str}. You want to start a random, casual conversation with '{user.display_name}' in this server to make the chat more active. "
                "Ask a fun, open-ended question. For example, you could ask about their favorite game, what they had for lunch, or what they're currently listening to. "
                "Keep it friendly and engaging. Remember to tag them using `[MENTION: {user.display_name}]`."
            )

        async with channel.typing():
            # Ensure the model exists before generating content
            if not cog.model:
                 logger.error("Proactive chat: AI model not loaded.")
                 return False, "AI model not available."

            response = await cog.model.generate_content_async(prompt)
            starter_text = _safe_get_response_text(response)

            if not starter_text:
                logger.warning(f"Proactive chat: AI generated an empty conversation starter for {user.name}.")
                # Optionally, add a fallback message here
                # starter_text = f"[MENTION: {user.display_name}] hey! what's up? :)"
                return False, "AI failed to generate a message."

            # Use _find_member for robust mention resolution
            def repl(match):
                name = match.group(1).strip()
                member = _find_member(channel.guild, name)
                # Fallback to name if member not found, though ideally it should be found
                return member.mention if member else name
            processed_text = re.sub(r"\[MENTION: (.+?)\]", repl, starter_text)

             # Split message if needed and send
            message_parts = processed_text.split('|||')
            for part in message_parts:
                part = part.strip()
                if part: # Ensure part is not empty
                    await channel.send(part, allowed_mentions=discord.AllowedMentions(users=True))
                    await asyncio.sleep(random.uniform(0.5, 1.5)) # Small delay between parts


            # Original implementation before splitting:
            # await channel.send(processed_text, allowed_mentions=discord.AllowedMentions(users=True))

            logger.info(f"Proactive chat: Sent a conversation starter to {user.name} in #{channel.name}.")
            return True, "Success"

    except discord.errors.Forbidden:
        logger.error(f"Proactive chat: Bot lacks permissions to send messages in #{channel.name} ({channel.guild.name}).")
        return False, "Bot lacks permissions in the channel."
    except Exception as e:
        logger.error(f"Failed to initiate conversation with {user.name}: {e}", exc_info=True)
        return False, f"An internal error occurred: {e}"