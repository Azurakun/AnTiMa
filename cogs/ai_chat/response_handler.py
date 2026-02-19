# cogs/ai_chat/response_handler.py
import discord
import logging
from datetime import datetime
import io
from PIL import Image
import re
import asyncio
import tempfile
import os
import functools
import google.generativeai as genai

from .memory_handler import summarize_and_save_memory
from .utils import _find_member, _safe_get_response_text, get_gif_url, should_send_gif, perform_web_search, identify_visual_content
from utils.db import ai_config_collection


logger = logging.getLogger(__name__)
MAX_HISTORY = 15

async def detect_conversation_topic(summarizer_model, channel):
    """Identifies the main subject(s) of the current conversation turn."""
    try:
        history = [msg async for msg in channel.history(limit=6)]
        history.reverse()
        chat_text = "\n".join([f"{msg.author.display_name}: {msg.clean_content}" for msg in history])
        prompt = f"Analyze chat. Identify the MAIN Subject or subjects (if multiple). Keep it very concise.\nChat:\n{chat_text}"
        response = await summarizer_model.generate_content_async(prompt)
        topic = _safe_get_response_text(response).strip()
        if "None" in topic or len(topic) > 50: return None
        return topic
    except: return None

def is_server_context_needed(prompt, topic):
    """Checks if server lore or community context is required for the response."""
    if topic and any(k in topic.lower() for k in ['server', 'community', 'chat']): return True
    return any(k in prompt.lower() for k in ["this server", "here", "rules", "admins"])

async def process_video_attachment(attachment):
    """Saves and uploads a video to Gemini for analysis."""
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

async def _send_and_handle_tool_loop(chat, prompt_content, message_channel, summarizer_model, current_topic=None):
    """
    Enhanced tool loop that executes search tools silently in the background.
    Supports Parallel Function Calling for multiple research tasks.
    """
    response = await chat.send_message_async(prompt_content)
    loop_count = 0
    max_loops = 5 
    
    while loop_count < max_loops:
        function_calls = [part.function_call for part in response.parts if part.function_call]
        has_text = any(part.text for part in response.parts)
        
        if not function_calls: break

        # Background processing: Show typing instead of "I don't know" admissions
        if message_channel:
            async with message_channel.typing(): pass

        loop_count += 1
        tool_tasks = []

        for fc in function_calls:
            tool_name = fc.name
            if tool_name in ['perform_web_search', 'identify_visual_content']:
                query = fc.args.get('query') or fc.args.get('visual_description', '')
                
                # Context enforcement for the deep-search utility
                search_query = query
                if tool_name == 'perform_web_search' and current_topic and current_topic.lower() not in query.lower():
                    search_query = f"{current_topic} {query}"

                async def execute_tool_task(name, q):
                    if name == 'perform_web_search':
                        return await perform_web_search(q)
                    return await identify_visual_content(q)
                
                tool_tasks.append(execute_tool_task(tool_name, search_query))

        if tool_tasks:
            results = await asyncio.gather(*tool_tasks)
            response_payload = []
            for i, fc in enumerate(function_calls):
                response_payload.append({
                    "function_response": {
                        "name": fc.name,
                        "response": {"result": results[i]}
                    }
                })
            response = await chat.send_message_async(response_payload)
        else: break

    return response

async def should_bot_respond_ai_check(cog, bot, summarizer_model, message: discord.Message) -> bool:
    """Uses a lightweight model to decide if AnTiMa should join the conversation."""
    guild_id = str(message.guild.id)
    guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
    is_chat_channel = message.channel.id == guild_config.get("channel")
    is_chat_forum = isinstance(message.channel, discord.Thread) and message.channel.parent_id == guild_config.get("forum")

    if is_chat_forum or bot.user in message.mentions: return True
    if message.reference and message.reference.resolved and message.reference.resolved.author == bot.user: return True

    if not is_chat_channel: return False

    history = [msg async for msg in message.channel.history(limit=6)]
    history.reverse()
    conversation_log = "\n".join([f"{m.author.display_name}: {m.clean_content}" for m in history])
    prompt = f"Analyze chat. Should AnTiMa respond to the last message based on context?\n---\n{conversation_log}\n---\nAnswer 'yes' or 'no'."
    
    try:
        response = await summarizer_model.generate_content_async(prompt)
        return 'yes' in _safe_get_response_text(response).strip().lower()
    except: return False

async def handle_single_user_response(cog, message, prompt, author):
    """Processes a single message, handles attachments, and manages the output flow."""
    try:
        async with message.channel.typing():
            guild_config = ai_config_collection.find_one({"_id": str(message.guild.id)}) or {}

            
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in message.channel.history(limit=MAX_HISTORY) if m.id != message.id]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            
            current_topic = await detect_conversation_topic(cog.summarizer_model, message.channel)
            content = [f"User {author.display_name} says: \"{prompt}\"."]
            
            uploaded_files_cleanup = []
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.content_type.startswith('image/'):
                        image_data = await attachment.read()
                        content.append(Image.open(io.BytesIO(image_data)))
                    elif attachment.content_type.startswith('video/'):
                        video_file = await process_video_attachment(attachment)
                        if video_file:
                            content.append(video_file)
                            uploaded_files_cleanup.append(video_file)



            response = await _send_and_handle_tool_loop(chat, content, message.channel, cog.summarizer_model, current_topic=current_topic)
            
            if uploaded_files_cleanup:
                for f in uploaded_files_cleanup:
                    try: genai.delete_file(name=f.name)
                    except: pass

            final_text = _safe_get_response_text(response)
            if not final_text: return

            # Final Processing: Mentions, Splitting, and GIF Resolution
            processed_text = re.sub(r"\[MENTION: (.+?)\]", lambda m: f"<@{_find_member(message.guild, m.group(1).strip()).id}>" if _find_member(message.guild, m.group(1).strip()) else m.group(1).strip(), final_text)
            
            for part in processed_text.split('|||'):
                part = part.strip()
                if not part: continue

                gif_url, gif_match = None, re.search(r"\[GIF: (.+?)\]", part)
                if gif_match:
                    search_term = gif_match.group(1).strip()
                    part = part.replace(gif_match.group(0), "").strip()
                    if await should_send_gif(cog.summarizer_model, message.channel, part, search_term):
                        gif_url = await get_gif_url(cog.http_session, search_term)

                if part:
                    async with message.channel.typing():
                        await asyncio.sleep(len(part)*0.02)
                        await message.channel.send(part)
                if gif_url: await message.channel.send(gif_url)
            
            cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, message.guild.id, chat.history))
    except Exception as e: logger.error(f"Error: {e}")

async def process_message_batch(cog, channel_id):
    """Processes a group of messages from multiple users as a single conversation turn."""
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

            
            history = [{'role': 'model' if m.author==cog.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=cog.bot.user else m.clean_content]} async for m in last_message.channel.history(limit=MAX_HISTORY) if m.id not in [msg.id for msg in batch]]
            history.reverse()
            chat = cog.model.start_chat(history=history)
            
            content, uploaded_files_cleanup, messages_str_parts = [], [], []
            for msg in batch:
                messages_str_parts.append(f"- From {msg.author.display_name}: \"{msg.clean_content}\"")
                if msg.attachments:
                    for attachment in msg.attachments:
                        if attachment.content_type.startswith('image/'):
                            content.append(Image.open(io.BytesIO(await attachment.read())))
                        elif attachment.content_type.startswith('video/'):
                            video_file = await process_video_attachment(attachment)
                            if video_file:
                                content.append(video_file)
                                uploaded_files_cleanup.append(video_file)

            content.insert(0, f"Respond to:\n" + "\n".join(messages_str_parts))
            


            response = await _send_and_handle_tool_loop(chat, content, last_message.channel, cog.summarizer_model)
            
            if uploaded_files_cleanup:
                for f in uploaded_files_cleanup:
                    try: genai.delete_file(name=f.name)
                    except: pass

            final_text = _safe_get_response_text(response)
            if not final_text: return

            processed_text = re.sub(r"\[MENTION: (.+?)\]", lambda m: f"<@{_find_member(last_message.guild, m.group(1).strip()).id}>" if _find_member(last_message.guild, m.group(1).strip()) else m.group(1).strip(), final_text)
            
            for part in processed_text.split('|||'):
                part = part.strip()
                if not part: continue

                gif_url, gif_match = None, re.search(r"\[GIF: (.+?)\]", part)
                if gif_match:
                    search_term = gif_match.group(1).strip()
                    part = part.replace(gif_match.group(0), "").strip()
                    if await should_send_gif(cog.summarizer_model, last_message.channel, part, search_term):
                        gif_url = await get_gif_url(cog.http_session, search_term)

                if part:
                    async with last_message.channel.typing():
                        await asyncio.sleep(len(part)*0.02)
                        await last_message.channel.send(part)
                if gif_url: await last_message.channel.send(gif_url)
            
            for author in unique_authors: 
                cog.bot.loop.create_task(summarize_and_save_memory(cog.summarizer_model, author, last_message.guild.id, chat.history))
    except Exception as e: logger.error(f"Error in batch: {e}")