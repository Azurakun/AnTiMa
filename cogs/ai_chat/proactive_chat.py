# cogs/ai_chat/proactive_chat.py
import discord
import logging
import random
import asyncio
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from .utils import _find_member, _safe_get_response_text
from .memory_handler import load_user_memories

logger = logging.getLogger(__name__)

async def _initiate_conversation(cog, channel: discord.TextChannel, user: discord.Member) -> tuple[bool, str]:
    """Uses the AI to generate and send a conversation starter. Returns a tuple of (success, reason)."""
    try:
        memory_summary = await load_user_memories(user.id, channel.guild.id)

        now_gmt7 = datetime.now(ZoneInfo("Asia/Jakarta"))
        time_str = now_gmt7.strftime("%A, %B %d, %Y at %I:%M %p %Z")

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
            if not cog.model:
                 return False, "AI model not available."

            response = await cog.model.generate_content_async(prompt)
            starter_text = _safe_get_response_text(response)

            if not starter_text:
                return False, "AI failed to generate a message."

            def repl(match):
                name = match.group(1).strip()
                member = _find_member(channel.guild, name)
                return member.mention if member else name
            
            processed_text = re.sub(r"\[MENTION: (.+?)\]", repl, starter_text)

            message_parts = processed_text.split('|||')
            for part in message_parts:
                part = part.strip()
                if part:
                    await channel.send(part, allowed_mentions=discord.AllowedMentions(users=True))
                    await asyncio.sleep(random.uniform(0.5, 1.5))

            return True, "Success"

    except discord.errors.Forbidden:
        logger.error(f"Proactive chat: Bot lacks permissions in #{channel.name}.")
        return False, "Bot lacks permissions."
    except Exception as e:
        logger.error(f"Failed to initiate conversation with {user.name}: {e}", exc_info=True)
        return False, f"An internal error occurred: {e}"