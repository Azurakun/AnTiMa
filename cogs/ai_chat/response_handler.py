# cogs/ai_chat/response_handler.py
import discord
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import io
from PIL import Image
import re
import asyncio
import random
from .memory_handler import load_user_memories, load_global_memories, summarize_and_save_memory
from .utils import _find_member, _safe_get_response_text, get_gif_url, should_send_gif
from utils.db import ai_config_collection

logger = logging.getLogger(__name__)
MAX_HISTORY = 15

async def should_bot_respond_ai_check(bot, summarizer_model, message: discord.Message) -> bool:
    """Uses an AI model to determine if the bot should respond based on conversation context, especially for follow-ups."""
    if message.reference and message.reference.message_id:
        try:
            replied_to = await message.channel.fetch_message(message.reference.message_id)
            if replied_to.author == bot.user:
                logger.info(f"Direct reply to AnTiMa detected for message '{message.content}'. Responding.")
                return True
        except (discord.NotFound, discord.HTTPException):
            pass

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
        "3. Respond if the last message is a follow-up or a direct reply to AnTiMa's previous message.\n"
        "4. DO NOT respond if users are clearly having a one-on-one conversation with each other that does not involve AnTiMa.\n"
        "5. DO NOT respond if the last message is a reply to another user and doesn't mention AnTiMa.\n\n"
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
            guild_config = ai_config_collection.find_one({"_id": str(last_message.guild.id)}) or {}
            style_guide = guild_config.get("personality_style_guide")

            style_guide_context = f"--- ADAPTIVE STYLE GUIDE FOR THIS SERVER ---\n{style_guide}\n--------------------------------------------\n\n" if style_guide else ""
            
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in last_message.channel.history(limit=MAX_HISTORY) if m.id not in [msg.id for msg in batch]]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            
            memory_context = ""
            global_memory_summary = await load_global_memories()
            if global_memory_summary:
                memory_context += f"Here is some general knowledge you have:\n<global_knowledge>\n{global_memory_summary}\n</global_knowledge>\n\n"

            for author in unique_authors:
                user_memory_summary = await load_user_memories(author.id, last_message.guild.id)
                if user_memory_summary:
                    memory_context += f"Background on {author.display_name}:\n<personal_memories>\n{user_memory_summary}\n</personal_memories>\n\n"
            
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
            
            now_gmt7 = datetime.now(ZoneInfo("Asia/Jakarta"))
            time_str = now_gmt7.strftime("%A, %B %d, %Y at %I:%M %p GMT+7")
            
            prompt = (
                f"The current time is {time_str}.\n"
                f"{style_guide_context}"
                f"{memory_context}"
                f"You've received several messages. Respond to each person in one message using `To [MENTION: username]: [response]`.\n\n"
                f"Here are the messages:\n{messages_str}"
            )
            content.insert(0, prompt)
            
            response = await chat.send_message_async(content)
            final_text = _safe_get_response_text(response)
            if not final_text: return

            processed_text = re.sub(r"\[MENTION: (.+?)\]", lambda m: f"<@{_find_member(last_message.guild, m.group(1).strip()).id}>" if _find_member(last_message.guild, m.group(1).strip()) else m.group(1).strip(), final_text)

            gif_url = None
            gif_match = re.search(r"\[GIF: (.+?)\]", processed_text)
            if gif_match:
                search_term = gif_match.group(1).strip()
                text_without_gif_tag = processed_text.replace(gif_match.group(0), "").strip()
                
                if await should_send_gif(cog.summarizer_model, last_message.channel, text_without_gif_tag, search_term):
                    gif_url = await get_gif_url(cog.http_session, search_term)
                else:
                    logger.info(f"GIF agent decided NOT to send a GIF for '{search_term}'.")
                
                processed_text = text_without_gif_tag

            processed_text = processed_text.replace('|||', '\n').strip()
            if processed_text:
                await last_message.channel.send(processed_text, allowed_mentions=discord.AllowedMentions(users=True))
            
            if gif_url:
                await last_message.channel.send(gif_url)
            
            for author in unique_authors: 
                cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, last_message.guild.id, chat.history))
    except Exception as e:
        logger.error(f"Error in grouped API call: {e}")
        await last_message.channel.send("😥 my brain isn't braining right now.")

async def handle_single_user_response(cog, message: discord.Message, prompt: str, author: discord.User, intervening_author: discord.User = None, intervening_prompt: str = None):
    try:
        async with message.channel.typing():
            guild_config = ai_config_collection.find_one({"_id": str(message.guild.id)}) or {}
            style_guide = guild_config.get("personality_style_guide")
            
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in message.channel.history(limit=MAX_HISTORY) if m.id != message.id]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            
            user_memory_summary = await load_user_memories(author.id, message.guild.id)
            global_memory_summary = await load_global_memories()

            memory_context = ""
            if global_memory_summary:
                memory_context += f"Here is some general knowledge you have:\n<global_knowledge>\n{global_memory_summary}\n</global_knowledge>\n\n"
            if user_memory_summary:
                memory_context += f"Here is a summary of your past conversations with {author.display_name} in this server.\n<personal_memories>\n{user_memory_summary}\n</personal_memories>\n\n"
            
            style_guide_context = ""
            if style_guide:
                style_guide_context = f"--- ADAPTIVE STYLE GUIDE FOR THIS SERVER ---\n{style_guide}\n--------------------------------------------\n\n"
            
            contextual_prompt_text = ""
            if intervening_author:
                contextual_prompt_text = (
                    f"The user {intervening_author.display_name} has mentioned you in a reply to {author.display_name}, asking you to respond to them.\n"
                    f"The original message from {author.display_name} was: \"{prompt}\"\n"
                    f"The comment from {intervening_author.display_name} was: \"{intervening_prompt}\"\n\n"
                    f"Based on this context, and your memories of {author.display_name}, formulate your response directly to {author.display_name}."
                )
            elif message.reference:
                try:
                    replied_to_message = await message.channel.fetch_message(message.reference.message_id)
                    replied_to_author_name = "you (AnTiMa)" if replied_to_message.author == cog.bot.user else replied_to_message.author.display_name
                    contextual_prompt_text = (
                        f"The user {author.display_name} is DIRECTLY REPLYING to {replied_to_author_name}.\n"
                        f"The original message they replied to was: \"{replied_to_message.clean_content}\"\n"
                        f"Their reply is: \"{prompt}\"\n\n"
                        f"Based on this direct reply context, and your memories of {author.display_name}, formulate your response to them."
                    )
                except (discord.NotFound, discord.HTTPException):
                    contextual_prompt_text = f"The user {author.display_name} is replying to a previous message and says: \"{prompt}\". (The message they replied to could not be fetched, so use general conversation context.)"
            else:
                contextual_prompt_text = f"The user {author.display_name} is talking to you and says: \"{prompt}\"."

            now_gmt7 = datetime.now(ZoneInfo("Asia/Jakarta"))
            time_str = now_gmt7.strftime("%A, %B %d, %Y at %I:%M %p GMT+7")

            full_prompt = (
                f"The current time is {time_str}.\n"
                f"{memory_context}"
                f"{style_guide_context}"
                f"Remember your core personality and the rules, especially the rule to break up your messages with '|||'. Remember to use `[MENTION: Username]` to tag users when needed.\n\n"
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

            processed_text = re.sub(r"\[MENTION: (.+?)\]", lambda m: f"<@{_find_member(message.guild, m.group(1).strip()).id}>" if _find_member(message.guild, m.group(1).strip()) else m.group(1).strip(), final_text)
            
            gif_url = None
            gif_match = re.search(r"\[GIF: (.+?)\]", processed_text)
            if gif_match:
                search_term = gif_match.group(1).strip()
                text_without_gif_tag = processed_text.replace(gif_match.group(0), "").strip()
                
                if await should_send_gif(cog.summarizer_model, message.channel, text_without_gif_tag, search_term):
                    gif_url = await get_gif_url(cog.http_session, search_term)
                else:
                    logger.info(f"GIF agent decided NOT to send a GIF for '{search_term}'.")
                
                processed_text = text_without_gif_tag
            
            message_parts = processed_text.split('|||')
            is_first_part = True

            for part in message_parts:
                part = part.strip()
                if not part:
                    continue

                delay = max(1.0, min(len(part) * 0.02, 3.0)) + random.uniform(0.2, 0.5)

                async with message.channel.typing():
                    await asyncio.sleep(delay)
                    if is_first_part:
                        await message.reply(part, allowed_mentions=discord.AllowedMentions(users=True))
                        is_first_part = False
                    else:
                        await message.channel.send(part)
            
            if gif_url:
                await message.channel.send(gif_url)
            
            cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, message.guild.id, chat.history))
    except Exception as e:
        logger.error(f"Error in single-user API call: {e}")
        await message.reply("😥 i'm sorry, my brain isn't braining right now.")