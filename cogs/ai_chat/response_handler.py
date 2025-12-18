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
from .utils import _find_member, _safe_get_response_text, get_gif_url, should_send_gif, perform_web_search, identify_visual_content
from utils.db import ai_config_collection
from .rate_limiter import can_make_request
from .server_context_learner import get_server_lore

logger = logging.getLogger(__name__)
MAX_HISTORY = 15
async def detect_conversation_topic(summarizer_model, channel):
    """Identifies the main subject(s) of the current conversation turn."""
    try:
        history = [msg async for msg in channel.history(limit=6)]
        history.reverse()
        chat_text = "\n".join([f"{msg.author.display_name}: {msg.clean_content}" for msg in history])
        # Updated prompt to handle multiple subjects
        prompt = f"Analyze chat. Identify the MAIN Subject or subjects (if multiple). Keep it very concise.\nChat:\n{chat_text}"
        response = await summarizer_model.generate_content_async(prompt)
        topic = _safe_get_response_text(response).strip()
        if "None" in topic or len(topic) > 50: return None
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
    """
    Enhanced tool loop that supports Parallel Function Calling.
    If the AI requests multiple searches (e.g., Acheron and Firefly), 
    they are executed concurrently with separate contexts.
    """
    response = await chat.send_message_async(prompt_content)
    loop_count = 0
    max_loops = 5 # Increased slightly to allow for multi-step research
    
    while loop_count < max_loops:
        # 1. Identify all function calls in this model turn
        function_calls = [part.function_call for part in response.parts if part.function_call]
        has_text = any(part.text for part in response.parts)
        
        # STOP CONDITION: No more tool calls
        if not function_calls:
            break

        # 2. Handle Intermediate Text (AI narrating its actions)
        if has_text and message_channel:
            text_parts = [part.text for part in response.parts if part.text]
            clean = "".join(text_parts).replace('|||', '\n').strip()
            if clean: 
                async with message_channel.typing(): 
                    await message_channel.send(clean)

        loop_count += 1
        
        # 3. Process all tool calls in parallel
        tool_tasks = []
        pending_queries = []

        for fc in function_calls:
            tool_name = fc.name
            if tool_name in ['perform_web_search', 'identify_visual_content']:
                # Extract query from either tool's arguments
                query = fc.args.get('query') or fc.args.get('visual_description', '')
                
                # Context enforcement: Apply the detected topic if not already in query
                search_query = query
                if tool_name == 'perform_web_search' and current_topic and current_topic.lower() not in query.lower():
                    search_query = f"{current_topic} {query}"

                pending_queries.append(search_query)

                # Execution wrapper for parallel gathering
                async def execute_tool_task(name, q):
                    logger.info(f"Parallel Tool {name} triggered: {q}")
                    if name == 'perform_web_search':
                        return await perform_web_search(q)
                    else:
                        return await identify_visual_content(q)
                
                tool_tasks.append(execute_tool_task(tool_name, search_query))

        # 4. Dynamic Wait Message for the batch of queries
        if pending_queries and message_channel and not (has_text and loop_count == 1):
            try:
                # Use first few queries for the wait message context
                q_list = ", ".join(pending_queries[:2]) + ("..." if len(pending_queries) > 2 else "")
                wait_prompt = f"AnTiMa. You are researching multiple things: {q_list}. Write a short, lowercase casual wait message. Output only msg."
                wait_res = await summarizer_model.generate_content_async(wait_prompt)
                await message_channel.send(f"🔍 {_safe_get_response_text(wait_res).strip()}")
            except:
                await message_channel.send(f"🔍 one sec, checking a few things for you...")

        # 5. Gather results and return to AI
        if tool_tasks:
            results = await asyncio.gather(*tool_tasks)
            
            # Construct the multi-part function response
            response_payload = []
            for i, fc in enumerate(function_calls):
                response_payload.append({
                    "function_response": {
                        "name": fc.name,
                        "response": {"result": results[i]}
                    }
                })
            
            # Send results back to the AI in one batch
            response = await chat.send_message_async(response_payload)
        else:
            # If function calls were non-search (unsupported), stop the loop
            break

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
            tools_active = []
            
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type.startswith('image/'):
                        image_data = await attachment.read()
                        image = Image.open(io.BytesIO(image_data))
                        content.append(image)
                        tools_active.append("`[VisualProcessor]: ENABLED`")
                        tools_active.append("`[VisualSearch]: ENABLED`")
                    elif attachment.content_type.startswith('video/'):
                        try:
                            wait_prompt = "AnTiMa. Someone sent a video. Write a short, lowercase reaction. Output ONLY the message."
                            wait_res = await cog.summarizer_model.generate_content_async(wait_prompt)
                            await message.channel.send(f"🎬 {_safe_get_response_text(wait_res).strip()}")
                        except:
                            await message.channel.send("🎬 ooh a video? lemme watch it...")

                        video_file = await process_video_attachment(attachment)
                        if video_file:
                            content.append(video_file)
                            uploaded_files_cleanup.append(video_file)
                            tools_active.append("`[VideoWatcher]: ENABLED`")
                            tools_active.append("`[VisualSearch]: ENABLED`")

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
            
            # Multi-message splitting
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