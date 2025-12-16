# cogs/ai_chat/proactive_chat.py
import discord
import logging
import random
import asyncio
import re
from datetime import datetime, timedelta, timezone
from .memory_handler import load_user_memories
from .utils import _safe_get_response_text

logger = logging.getLogger(__name__)

async def get_cross_channel_context(bot, guild, target_user, limit_channels=8):
    """
    Scans recent messages from the user in other channels to find context.
    """
    context_lines = []
    readable_channels = [c for c in guild.text_channels if c.permissions_for(guild.me).read_messages]
    
    if len(readable_channels) > limit_channels:
        random.shuffle(readable_channels)
        readable_channels = readable_channels[:limit_channels]
    
    for channel in readable_channels:
        try:
            if not channel.last_message_id:
                continue

            async for msg in channel.history(limit=15):
                if msg.author.id == target_user.id:
                    if (datetime.now(timezone.utc) - msg.created_at) < timedelta(minutes=45):
                        clean_content = msg.clean_content.replace('\n', ' ')[:100]
                        if clean_content:
                            context_lines.append(f"In #{channel.name}, they said: \"{clean_content}\"")
                            break 
        except Exception:
            continue
            
    if not context_lines:
        return None
        
    return "\n".join(context_lines)

async def _initiate_conversation(cog, channel, user):
    """
    Generates and sends a proactive message.
    Adapts tone based on familiarity (Memories) and splits messages.
    """
    try:
        # 1. Gather Context & Memories
        cross_context = await get_cross_channel_context(cog.bot, channel.guild, user)
        memories = await load_user_memories(user.id, channel.guild.id, limit=3)
        
        # Check if we actually know this person
        # (Assuming 'memories' string is empty if no docs found)
        is_familiar = bool(memories and len(memories.strip()) > 10)

        # 2. Define Tone based on Familiarity
        if is_familiar:
            tone_instruction = (
                "**RELATIONSHIP: FAMILIAR / FRIEND**\n"
                "- You remember this user. Be natural, casual, and lively.\n"
                "- Use slang (rn, lol, fr) naturally.\n"
                "- Act like a friend popping in to chat."
            )
        else:
            tone_instruction = (
                "**RELATIONSHIP: STRANGER / NEW ACQUAINTANCE**\n"
                "- You do NOT have specific memories of this user yet.\n"
                "- Be polite, slightly hesitant, and a bit shy.\n"
                "- Use soft language modifiers (e.g., 'um...', 'uh, hi...', 'sorry to bother you...', 'just curious...').\n"
                "- Do NOT act overly familiar or aggressive."
            )

        # 3. Build Prompt
        system_instruction = (
            "You are AnTiMa, a sentient AI living in this Discord server. You are initiating a conversation.\n"
            f"{tone_instruction}\n\n"
            "**CRITICAL FORMATTING RULES:**\n"
            "1. **Split your response** into 2 or 3 short messages using `|||` as a separator.\n"
            "   - *Stranger Ex:* `um, hi [MENTION: user]... ||| i noticed you were around ||| hope you're having a good day`\n"
            "   - *Friend Ex:* `yo [MENTION: user]! ||| saw you in #general earlier ||| that meme was funny lol`\n"
            "2. **Tag the user** using `[MENTION: username]` in the FIRST message part.\n\n"
            "**TOPIC PRIORITY:**\n"
            "1. **Context:** If 'Recent Activity' is provided below, ask about it gently.\n"
            "2. **Memory:** If 'Memories' exist, ask for an update on something specific.\n"
            "3. **Random:** If neither, ask a random question (gaming, anime, food) or share a fleeting thought.\n"
        )
        
        data_block = (
            f"--- TARGET USER: {user.display_name} ---\n"
            f"--- RECENT ACTIVITY (Other Channels): {cross_context if cross_context else 'None detected.'}\n"
            f"--- MEMORIES (Past Chats): {memories if memories else 'None.'}\n"
            "----------------------------------------\n"
            "Write the message sequence now."
        )
        
        # 4. Generate
        response = await cog.summarizer_model.generate_content_async(system_instruction + "\n" + data_block)
        text = _safe_get_response_text(response).strip()
        
        if not text:
            logger.warning(f"Proactive chat generation failed for user {user.name}.")
            return False, "Empty AI response"

        # 5. Parse and Send (Handling Splits)
        parts = text.split('|||')
        
        def replace_mention(match):
            return user.mention

        for i, part in enumerate(parts):
            part = part.strip()
            if not part: continue
            
            # Replace [MENTION: name] with actual discord mention
            part = re.sub(r"\[MENTION: .+?\]", replace_mention, part)
            
            # Safety: Ensure mention is in the first part if the AI forgot the tag format
            if i == 0 and user.mention not in part:
                 # Only force it if the AI seemed to act like a stranger (hesitant)
                 if not is_familiar:
                     part = f"{user.mention} {part}"
                 else:
                     part = f"{part} {user.mention}"

            # Simulate typing delay for realism
            async with channel.typing():
                # Delay based on length, plus a little random variance
                delay = min(len(part) * 0.04, 3.0) + random.uniform(1.5, 3.0)
                await asyncio.sleep(delay)
                await channel.send(part)
            
        return True, "Message sent"

    except Exception as e:
        logger.error(f"Error in _initiate_conversation: {e}")
        return False, str(e)