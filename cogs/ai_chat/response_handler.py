# cogs/ai_chat/response_handler.py
import discord
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import io
from PIL import Image
import re
from .memory_handler import load_user_memories, summarize_and_save_memory

logger = logging.getLogger(__name__)
MAX_HISTORY = 15

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

def _safe_get_response_text(response) -> str:
    """Safely gets text from a Gemini response, handling blocked content."""
    try:
        return response.text
    except (ValueError, IndexError):
        logger.warning("Gemini response was empty or blocked.")
        return ""

async def should_bot_respond_ai_check(bot, summarizer_model, message: discord.Message) -> bool:
    """Uses an AI model to determine if the bot should respond based on conversation context."""
    history = [msg async for msg in message.channel.history(limit=6)]
    history.reverse()

    if len(history) <= 1:
        content = message.content.lower()
        return 'antima' in content or 'anti' in content or '?' in content

    conversation_log = []
    for msg in history:
        author_name = "AnTiMa" if msg.author == bot.user else msg.author.display_name
        reply_info = ""
        if msg.reference and msg.reference.message_id:
            replied_to_author = None
            # This is a simplified check; a full fetch would be too slow here.
            for hist_msg in history:
                if msg.reference.message_id == hist_msg.id:
                    replied_to_author = "AnTiMa" if hist_msg.author == bot.user else hist_msg.author.display_name
                    break
            if replied_to_author:
                reply_info = f"(in reply to {replied_to_author}) "
        conversation_log.append(f"{author_name}: {reply_info}{msg.clean_content}")
    
    conversation_str = "\n".join(conversation_log)

    prompt = (
        "You are a context analysis AI. Your name is AnTiMa. Below is a Discord conversation. "
        "Based ONLY on the context and the content of the VERY LAST message, determine if AnTiMa should respond. "
        "Rules for responding:\n"
        "1. Respond if the last message directly addresses AnTiMa by name (e.g., 'AnTiMa', 'Anti').\n"
        "2. Respond if the last message asks a general question that AnTiMa could answer (like about code, trivia, or an opinion), especially if no one else is being asked.\n"
        "3. DO NOT respond if users are clearly having a one-on-one conversation with each other that does not involve AnTiMa.\n"
        "4. DO NOT respond if the last message is a reply to another user and doesn't mention AnTiMa.\n\n"
        f"--- CONVERSATION ---\n{conversation_str}\n---\n\n"
        "Based on these rules and the final message, should AnTiMa join in? Answer with only 'yes' or 'no'."
    )

    try:
        response = await summarizer_model.generate_content_async(prompt)
        decision = _safe_get_response_text(response).strip().lower()
        logger.info(f"Context check for message '{message.content}'. AI Decision: '{decision}'")
        return 'yes' in decision
    except Exception as e:
        logger.error(f"Context check AI call failed: {e}")
        return False

async def process_message_batch(cog, channel_id: int):
    batch = cog.message_batches.pop(channel_id, [])
    cog.batch_timers.pop(channel_id, None)
    if not batch: return

    last_message = batch[-1]
    unique_authors = list({msg.author for msg in batch})

    if len(unique_authors) == 1 and not any(msg.attachments for msg in batch):
        await handle_single_user_response(cog, last_message, "\n".join([m.clean_content for m in batch]), unique_authors[0])
        return

    try:
        async with last_message.channel.typing():
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in last_message.channel.history(limit=MAX_HISTORY) if m.id not in [msg.id for msg in batch]]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            memory_context = "".join([f"Background on {author.display_name}:\n<memory>\n{await load_user_memories(author.id)}\n</memory>\n\n" for author in unique_authors if await load_user_memories(author.id)])
            
            messages_str_parts = []
            content = []
            for msg in batch:
                messages_str_parts.append(f"- From {msg.author.display_name}: \"{msg.clean_content}\"")
                if msg.attachments:
                    for attachment in msg.attachments:
                        if attachment.content_type.startswith('image/'):
                            image_data = await attachment.read()
                            image = Image.open(io.BytesIO(image_data))
                            content.append(image)

            messages_str = "\n".join(messages_str_parts)
            
            # Add current time to the prompt
            now_gmt7 = datetime.now(ZoneInfo("Asia/Jakarta"))
            time_str = now_gmt7.strftime("%A, %B %d, %Y at %I:%M %p GMT+7")
            
            prompt = f"The current time is {time_str}. You've received several messages. Respond to each person in one message using `To [MENTION: username]: [response]`.\n\n{memory_context}Here are the messages:\n{messages_str}"
            content.insert(0, prompt)
            
            response = await chat.send_message_async(content)
            final_text = _safe_get_response_text(response)
            if not final_text: return

            def repl(match):
                name = match.group(1).strip()
                member = _find_member(last_message.guild, name)
                return f"<@{member.id}>" if member else name
            processed_text = re.sub(r"\[MENTION: (.+?)\]", repl, final_text)

            if processed_text:
                for chunk in [processed_text[i:i+2000] for i in range(0, len(processed_text), 2000)]:
                    await last_message.channel.send(chunk, allowed_mentions=discord.AllowedMentions(users=True))
            
            for author in unique_authors: cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, chat.history))
    except Exception as e:
        logger.error(f"Error in grouped API call: {e}")
        await last_message.channel.send("😥 my brain isn't braining right now.")

async def handle_single_user_response(cog, message: discord.Message, prompt: str, author: discord.User):
    try:
        async with message.channel.typing():
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in message.channel.history(limit=MAX_HISTORY) if m.id != message.id]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            
            memory_summary = await load_user_memories(author.id)
            memory_context = f"Here is a summary of your past conversations with {author.display_name}.\n<memory>\n{memory_summary}\n</memory>\n\n" if memory_summary else ""
            
            # --- NEW, MORE CONTEXTUAL PROMPT ---
            contextual_prompt_text = ""
            if message.reference:
                try:
                    replied_to_message = await message.channel.fetch_message(message.reference.message_id)
                    replied_to_author_name = "you (AnTiMa)" if replied_to_message.author == cog.bot.user else replied_to_message.author.display_name
                    contextual_prompt_text = (
                        f"The user {author.display_name} is replying to {replied_to_author_name}.\n"
                        f"The original message was: \"{replied_to_message.clean_content}\"\n"
                        f"Their reply is: \"{prompt}\"\n\n"
                        f"Based on this context, and your memories of {author.display_name}, formulate your response to them."
                    )
                except (discord.NotFound, discord.HTTPException):
                    contextual_prompt_text = f"The user {author.display_name} is replying to a previous message and says: \"{prompt}\"."
            else:
                contextual_prompt_text = f"The user {author.display_name} is talking to you and says: \"{prompt}\"."

            # Add current time to the prompt
            now_gmt7 = datetime.now(ZoneInfo("Asia/Jakarta"))
            time_str = now_gmt7.strftime("%A, %B %d, %Y at %I:%M %p GMT+7")

            full_prompt = (
                f"The current time is {time_str}.\n"
                f"{memory_context}"
                f"Remember your personality and the rules. Remember to use `[MENTION: Username]` to tag users when needed.\n\n"
                f"--- Current Conversation Turn ---\n"
                f"{contextual_prompt_text}"
            )
            
            content = [full_prompt]

            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type.startswith('image/'):
                        image_data = await attachment.read()
                        image = Image.open(io.BytesIO(image_data))
                        content.append(image)
            
            response = await chat.send_message_async(content)
            final_text = _safe_get_response_text(response)
            if not final_text:
                await message.reply("i wanted to say something, but my brain filters went 'nope!' try rephrasing that?")
                return

            def repl(match):
                name = match.group(1).strip()
                member = _find_member(message.guild, name)
                return f"<@{member.id}>" if member else name
            processed_text = re.sub(r"\[MENTION: (.+?)\]", repl, final_text)
            
            if processed_text:
                await message.reply(processed_text[:2000], allowed_mentions=discord.AllowedMentions(users=True))
            cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, chat.history))
    except Exception as e:
        logger.error(f"Error in single-user API call: {e}")
        await message.reply("😥 i'm sorry, my brain isn't braining right now.")