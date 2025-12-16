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
import tempfile
import os
import functools
import google.generativeai as genai

from .memory_handler import load_user_memories, load_global_memories, summarize_and_save_memory
from .utils import _find_member, _safe_get_response_text, get_gif_url, should_send_gif, perform_web_search, identify_visual_content # Import new tool
from utils.db import ai_config_collection
from .rate_limiter import can_make_request
from .server_context_learner import get_server_lore

logger = logging.getLogger(__name__)
MAX_HISTORY = 15

async def detect_conversation_topic(summarizer_model, channel):
    try:
        history = [msg async for msg in channel.history(limit=6)]
        history.reverse()
        chat_text = "\n".join([f"{msg.author.display_name}: {msg.clean_content}" for msg in history])
        prompt = f"Analyze chat. Identify MAIN Subject.\nChat:\n{chat_text}"
        response = await summarizer_model.generate_content_async(prompt)
        topic = _safe_get_response_text(response).strip()
        if "None" in topic or len(topic) > 40: return None
        return topic
    except: return None

def is_server_context_needed(prompt, topic):
    if topic and any(k in topic.lower() for k in ['server', 'community', 'chat']): return True
    return any(k in prompt.lower() for k in ["this server", "here", "rules", "admins"])

async def process_video_attachment(attachment):
    logger.info(f"Processing video: {attachment.filename}")
    fd, temp_path = tempfile.mkstemp(suffix=f"_{attachment.filename}")
    os.close(fd)
    try:
        await attachment.save(temp_path)
        loop = asyncio.get_running_loop()
        file_obj = await loop.run_in_executor(None, functools.partial(genai.upload_file, path=temp_path, mime_type=attachment.content_type))
        while file_obj.state.name == "PROCESSING":
            await asyncio.sleep(2)
            file_obj = await loop.run_in_executor(None, functools.partial(genai.get_file, name=file_obj.name))
        return file_obj if file_obj.state.name != "FAILED" else None
    except: return None
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

async def _send_and_handle_tool_loop(chat, prompt_content, message_channel, summarizer_model, notify_message=None, current_topic=None):
    response = await chat.send_message_async(prompt_content)
    loop_count = 0
    max_loops = 3
    
    while loop_count < max_loops:
        function_call = None
        has_text = False
        if response.parts:
            for part in response.parts:
                if part.function_call: function_call = part.function_call
                if part.text: has_text = True
        
        # STOP CONDITION: If no function call, this is the final answer. 
        # Break loop so the caller sends it (prevents double sending).
        if not function_call: break

        # INTERMEDIATE MESSAGE: If there's text AND a tool call, send text now.
        if has_text and message_channel:
            for part in response.parts:
                if part.text:
                    clean = part.text.replace('|||', '\n').strip()
                    if clean: 
                        async with message_channel.typing(): await message_channel.send(clean)

        loop_count += 1
        
        if function_call.name == 'perform_web_search' or function_call.name == 'identify_visual_content':
            tool_name = function_call.name
            query = function_call.args.get('query') or function_call.args.get('visual_description', '')
            
            # Context enforcement only for text search
            if tool_name == 'perform_web_search' and current_topic and current_topic.lower() not in query.lower():
                query = f"{current_topic} {query}"

            # Dynamic Wait (only if no text sent)
            if not has_text and message_channel:
                try:
                    action = "looking that up" if tool_name == 'perform_web_search' else "analyzing that image"
                    wait_res = await summarizer_model.generate_content_async(f"AnTiMa. You are {action}: '{query}'. Casual short wait msg. Output only msg.")
                    await message_channel.send(f"🔍 {_safe_get_response_text(wait_res).strip()}")
                except: await message_channel.send(f"🔍 one sec, {action}...")

            logger.info(f"Tool {tool_name} requested: {query}")
            
            # Execute correct tool
            if tool_name == 'perform_web_search':
                result = await perform_web_search(query)
            else:
                result = await identify_visual_content(query)

            response = await chat.send_message_async({
                "function_response": {
                    "name": tool_name,
                    "response": {"result": result}
                }
            })
        else: break
    return response

async def should_bot_respond_ai_check(cog, bot, summarizer_model, message: discord.Message) -> bool:
    guild_id = str(message.guild.id)
    guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
    is_chat_channel = message.channel.id == guild_config.get("channel")
    is_chat_forum = isinstance(message.channel, discord.Thread) and message.channel.parent_id == guild_config.get("forum")

    if is_chat_forum: return True
    if bot.user in message.mentions: return True
    if message.reference and message.reference.message_id:
        try:
            replied_to = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
            if replied_to.author == bot.user: return True
        except: pass

    if message.reference and message.reference.message_id in cog.ignored_messages: return False
    if not is_chat_channel: return False

    history = [msg async for msg in message.channel.history(limit=6)]
    history.reverse()
    if len(history) <= 1:
        return 'antima' in message.content.lower() or 'anti' in message.content.lower()

    conversation_log = "\n".join([f"{'AnTiMa' if m.author==bot.user else m.author.display_name}: {m.clean_content}" for m in history])
    prompt = f"Analyze chat. Should AnTiMa respond to the last message based on context?\n---\n{conversation_log}\n---\nAnswer 'yes' or 'no'."
    
    try:
        response = await summarizer_model.generate_content_async(prompt)
        return 'yes' in _safe_get_response_text(response).strip().lower()
    except: return False

async def handle_single_user_response(cog, message, prompt, author, intervening_author=None, intervening_prompt=None):
    try:
        async with message.channel.typing():
            guild_config = ai_config_collection.find_one({"_id": str(message.guild.id)}) or {}
            style_guide = guild_config.get("personality_style_guide")
            daily_limit = guild_config.get("daily_rate_limit", 50)
            
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in message.channel.history(limit=MAX_HISTORY) if m.id != message.id]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            
            current_topic = await detect_conversation_topic(cog.summarizer_model, message.channel)
            
            # Context Variables
            content = [f"User {author.display_name} says: \"{prompt}\"."]
            if intervening_author: content = [f"{intervening_author.display_name} interrupted {author.display_name}. Respond to {author.display_name}."]
            
            uploaded_files_cleanup = []
            
            # --- DYNAMIC TOOL HINTS ---
            tools_active = []
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type.startswith('image/'):
                        image_data = await attachment.read()
                        image = Image.open(io.BytesIO(image_data))
                        content.append(image)
                        tools_active.append("`[VisualProcessor]: ENABLED`")
                        tools_active.append("`[VisualSearch]: ENABLED (Use identify_visual_content if needed)`")
                    elif attachment.content_type.startswith('video/'):
                        # DYNAMIC AI WAIT MESSAGE
                        try:
                            wait_prompt = "You are AnTiMa. Someone sent a video. Write a short, lowercase reaction. Ex: 'ooh a video? lemme watch', 'loading clip...'. Output ONLY the message."
                            wait_res = await cog.summarizer_model.generate_content_async(wait_prompt)
                            await message.channel.send(f"👀 {_safe_get_response_text(wait_res).strip()}")
                        except:
                            await message.channel.send("👀 ooh a video? lemme watch it...")

                        video_file = await process_video_attachment(attachment)
                        if video_file:
                            content.append(video_file)
                            uploaded_files_cleanup.append(video_file)
                            tools_active.append("`[VideoWatcher]: ENABLED`")
                            tools_active.append("`[VisualSearch]: ENABLED (Use identify_visual_content if needed)`")

            # Inject Tool Hints into Prompt
            if tools_active:
                content[0] += "\n\n**SYSTEM NOTIFICATION:**\n" + "\n".join(tools_active) + "\n"

            is_allowed, _, _ = can_make_request(str(message.guild.id), daily_limit)
            if not is_allowed: return

            response = await _send_and_handle_tool_loop(chat, content, message.channel, cog.summarizer_model, notify_message=message, current_topic=current_topic)
            
            if uploaded_files_cleanup:
                loop = asyncio.get_running_loop()
                for f in uploaded_files_cleanup:
                    try: await loop.run_in_executor(None, functools.partial(genai.delete_file, name=f.name))
                    except: pass

            final_text = _safe_get_response_text(response)
            if not final_text: return

            processed_text = re.sub(r"\[MENTION: (.+?)\]", lambda m: f"<@{_find_member(message.guild, m.group(1).strip()).id}>" if _find_member(message.guild, m.group(1).strip()) else m.group(1).strip(), final_text)
            
            # GIF Handling
            parts = processed_text.split('|||')
            for part in parts:
                if part.strip():
                    async with message.channel.typing():
                        await asyncio.sleep(len(part)*0.02)
                        await message.channel.send(part.strip())
            
            cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, message.guild.id, chat.history))

    except Exception as e:
        logger.error(f"Error in single user response: {e}")

async def process_message_batch(cog, channel_id):
    # Batch processing logic applying similar structure
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
            daily_limit = guild_config.get("daily_rate_limit", 50)
            
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in last_message.channel.history(limit=MAX_HISTORY) if m.id not in [msg.id for msg in batch]]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            
            current_topic = await detect_conversation_topic(cog.summarizer_model, last_message.channel)
            
            messages_str_parts = []
            content = []
            uploaded_files_cleanup = []
            tools_active = []

            for msg in batch:
                messages_str_parts.append(f"- From {msg.author.display_name}: \"{msg.clean_content}\"")
                if msg.attachments:
                    for attachment in msg.attachments:
                        if attachment.content_type.startswith('image/'):
                            image_data = await attachment.read()
                            image = Image.open(io.BytesIO(image_data))
                            content.append(image)
                            tools_active.append("`[VisualProcessor]: ENABLED`")
                        elif attachment.content_type.startswith('video/'):
                            video_file = await process_video_attachment(attachment)
                            if video_file:
                                content.append(video_file)
                                uploaded_files_cleanup.append(video_file)
                                tools_active.append("`[VideoWatcher]: ENABLED`")

            messages_str = "\n".join(messages_str_parts)
            prompt = f"Respond to:\n{messages_str}"
            if tools_active: prompt += "\n\n**SYSTEM:**\n" + "\n".join(tools_active)

            content.insert(0, prompt)
            
            is_allowed, _, _ = can_make_request(str(last_message.guild.id), daily_limit)
            if not is_allowed: return

            response = await _send_and_handle_tool_loop(chat, content, last_message.channel, cog.summarizer_model, current_topic=current_topic)
            
            if uploaded_files_cleanup:
                loop = asyncio.get_running_loop()
                for f in uploaded_files_cleanup:
                    try: await loop.run_in_executor(None, functools.partial(genai.delete_file, name=f.name))
                    except: pass

            final_text = _safe_get_response_text(response)
            if not final_text: return

            processed_text = re.sub(r"\[MENTION: (.+?)\]", lambda m: f"<@{_find_member(last_message.guild, m.group(1).strip()).id}>" if _find_member(last_message.guild, m.group(1).strip()) else m.group(1).strip(), final_text)
            
            parts = processed_text.split('|||')
            for part in parts:
                if part.strip():
                    async with last_message.channel.typing():
                        await asyncio.sleep(len(part)*0.02)
                        await last_message.channel.send(part.strip())
            
            for author in unique_authors: 
                cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, last_message.guild.id, chat.history))

    except Exception as e:
        logger.error(f"Error in batch API call: {e}")