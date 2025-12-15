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
from .utils import _find_member, _safe_get_response_text, get_gif_url, should_send_gif, perform_web_search
from utils.db import ai_config_collection
from .rate_limiter import can_make_request

logger = logging.getLogger(__name__)
MAX_HISTORY = 15

async def _send_and_handle_tool_loop(chat, prompt_content, message_channel, notify_message=None):
    """
    Sends a message to the chat model and handles any resulting function calls (tools)
    in a loop until a text response is received or the loop breaks.
    """
    # Initial request
    response = await chat.send_message_async(prompt_content)
    
    # Loop to check for function calls
    loop_count = 0
    max_loops = 3  # Prevent infinite loops
    
    while loop_count < max_loops:
        # Check all parts for a function call, not just the first one
        function_call = None
        if response.parts:
            for part in response.parts:
                if part.function_call:
                    function_call = part.function_call
                    break
        
        if not function_call:
            break

        loop_count += 1
        
        if function_call.name == 'perform_web_search':
            if notify_message:
                # Notify the user that we are searching
                try:
                    await notify_message.reply("🔍 wait up, let me search that up real quick... 🌐")
                except Exception:
                    pass # Ignore if we can't reply
            elif message_channel:
                 try:
                    await message_channel.send("🔍 searching the web for latest info... give me a sec.")
                 except Exception:
                    pass

            # Execute the search
            query = function_call.args.get('query', '')
            logger.info(f"AI requested web search for: {query}")
            
            # --- Perform Search ---
            search_result = await perform_web_search(query)
            # ----------------------
            
            # Log the result so we know what the AI is seeing
            preview = search_result[:200].replace('\n', ' ') + "..." if len(search_result) > 200 else search_result.replace('\n', ' ')
            logger.info(f"DEBUG: Tool Output sent to AI: {preview}")

            # Send the tool output back to the model
            response = await chat.send_message_async(
                {
                    "function_response": {
                        "name": "perform_web_search",
                        "response": {"result": search_result}
                    }
                }
            )
        else:
            # Unknown tool or other function call we don't handle explicitly here
            break
            
    return response

# vvvvvv REWRITTEN vvvvvv
async def should_bot_respond_ai_check(cog, bot, summarizer_model, message: discord.Message) -> bool:
    """
    Determines if the bot should respond to a given message using a tiered logic system.
    This is the single source of truth for the response decision.
    """
    guild_id = str(message.guild.id)
    guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
    is_chat_channel = message.channel.id == guild_config.get("channel")
    is_chat_forum = isinstance(message.channel, discord.Thread) and message.channel.parent_id == guild_config.get("forum")

    # --- Step 1: High-priority "YES" conditions (explicit involvement) ---
    if is_chat_forum:
        return True

    if bot.user in message.mentions:
        logger.info(f"Direct mention of AnTiMa detected in '{message.content}'. Responding.")
        return True

    if message.reference and message.reference.message_id:
        try:
            replied_to = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
            if replied_to.author == bot.user:
                logger.info(f"Direct reply to AnTiMa detected for '{message.content}'. Responding.")
                return True
        except (discord.NotFound, discord.HTTPException):
            pass

    # --- Step 2: High-priority "NO" condition (part of an ignored conversation) ---
    if message.reference and message.reference.message_id in cog.ignored_messages:
        logger.info(f"Message '{message.content}' is a reply to an ignored message. Ignoring.")
        return False
        
    # --- Step 3: AI-based decision for ambiguous cases in the main chat channel ---
    if not is_chat_channel:
        return False

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
            replied_to_author = next((("AnTiMa" if h.author == bot.user else h.author.display_name) for h in history if h.id == msg.reference.message_id), None)
            if replied_to_author:
                reply_info = f"(in reply to {replied_to_author}) "
        conversation_log.append(f"{author_name}: {reply_info}{msg.clean_content}")

    conversation_str = "\n".join(conversation_log)

    prompt = (
        "You are a context analysis AI named AnTiMa. Analyze the following Discord conversation. "
        "Based ONLY on the context and the content of the VERY LAST message, should AnTiMa respond? "
        "Your decision rules:\n"
        "1. **Respond (yes)** if the last message asks a general question that AnTiMa could answer (about code, trivia, opinions), especially if no one else is specifically asked.\n"
        "2. **Respond (yes)** if the last message seems to be a follow-up to something AnTiMa said earlier in the context.\n"
        "3. **DO NOT Respond (no)** if users are clearly having a direct, one-on-one conversation that doesn't involve or mention AnTiMa.\n"
        "4. **DO NOT Respond (no)** if the last message is a reply to another user and has no indication it's meant for AnTiMa.\n\n"
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
# ^^^^^^ REWRITTEN ^^^^^^

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
            
            # --- RATE LIMIT CHECK ---
            is_allowed, count = can_make_request()
            if not is_allowed:
                logger.warning(f"Gemini Pro request (batch) denied. Daily limit (50) reached.")
                await last_message.channel.send("i'm feeling a bit tired... my brain needs a break for today. 😵‍💫 try again tomorrow!")
                return
            logger.info(f"Gemini Pro request (batch) #{count} for the day.")
            # --- END RATE LIMIT CHECK ---
            
            # Use the helper to handle the response and potential tool calls
            response = await _send_and_handle_tool_loop(chat, content, message_channel=last_message.channel)
            
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
                        
            # --- RATE LIMIT CHECK ---
            is_allowed, count = can_make_request()
            if not is_allowed:
                logger.warning(f"Gemini Pro request denied. Daily limit (50) reached. User: {author.name}")
                await message.reply("i'm feeling a bit tired... my brain needs a break for today. 😵‍💫 try again tomorrow!")
                return
            logger.info(f"Gemini Pro request #{count} for the day.")
            # --- END RATE LIMIT CHECK ---
            
            # Use the helper to handle response + tool loop
            # We pass 'message' so we can reply with the "Searching..." notification
            response = await _send_and_handle_tool_loop(chat, content, message_channel=message.channel, notify_message=message)
            
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