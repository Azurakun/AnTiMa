# cogs/ai_chat/response_handler.py
import discord
import logging
from datetime import datetime
import io
import base64
from PIL import Image
import re
import asyncio
import json

from .memory_handler import summarize_and_save_memory
from .utils import (
    _find_member, _safe_get_response_text, get_gif_url,
    should_send_gif, perform_web_search, identify_visual_content,
    AI_CHAT_TOOLS,
)
from utils.db import ai_config_collection
from utils.ai_client import get_client, MAIN_MODEL, FAST_MODEL, throttled_create

logger = logging.getLogger(__name__)
MAX_HISTORY = 15


def _image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _build_history_messages(raw_history: list, bot_user) -> list[dict]:
    messages = []
    for m in reversed(raw_history):
        role = "assistant" if m.author == bot_user else "user"
        label = "" if m.author == bot_user else f"{m.author.display_name}: "
        messages.append({"role": role, "content": f"{label}{m.clean_content}"})
    return messages


async def _encode_attachment(attachment: discord.Attachment) -> dict | None:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        image_bytes = await attachment.read()
        b64 = _image_to_base64(image_bytes)
        mime = attachment.content_type.split(";")[0]
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    return None


async def detect_conversation_topic(channel) -> str | None:
    try:
        history = [msg async for msg in channel.history(limit=6)]
        history.reverse()
        chat_text = "\n".join([f"{msg.author.display_name}: {msg.clean_content}" for msg in history])
        prompt = f"Analyze chat. Identify the MAIN Subject or subjects (if multiple). Keep it very concise.\nChat:\n{chat_text}"
        client = get_client()
        response = await throttled_create(client.chat.completions.create(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=30,
        ))
        topic = (response.choices[0].message.content or "").strip()
        if "None" in topic or len(topic) > 50:
            return None
        return topic
    except Exception:
        return None


def is_server_context_needed(prompt, topic) -> bool:
    if topic and any(k in topic.lower() for k in ["server", "community", "chat"]):
        return True
    return any(k in prompt.lower() for k in ["this server", "here", "rules", "admins"])


async def _execute_tool(tool_name: str, args: dict, current_topic: str | None = None) -> str:
    if tool_name == "perform_web_search":
        query = args.get("query", "")
        if current_topic and current_topic.lower() not in query.lower():
            query = f"{current_topic} {query}"
        return await perform_web_search(query)
    elif tool_name == "identify_visual_content":
        return await identify_visual_content(args.get("visual_description", ""))
    return f"Unknown tool: {tool_name}"


async def _send_and_handle_tool_loop(
    messages: list[dict],
    message_channel,
    current_topic: str | None = None,
    max_loops: int = 5,
) -> tuple[str, list[dict]]:
    client = get_client()
    loop_count = 0

    while loop_count < max_loops:
        response = await throttled_create(client.chat.completions.create(
            model=MAIN_MODEL,
            messages=messages,
            tools=AI_CHAT_TOOLS,
            tool_choice="auto",
        ))

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            return msg.content or "", messages

        if message_channel:
            async with message_channel.typing():
                pass

        loop_count += 1

        tool_tasks = []
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            tool_tasks.append(_execute_tool(tc.function.name, args, current_topic))

        results = await asyncio.gather(*tool_tasks)

        for tc, result in zip(msg.tool_calls, results):
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

    response = await throttled_create(client.chat.completions.create(model=MAIN_MODEL, messages=messages))
    final = response.choices[0].message.content or ""
    messages.append({"role": "assistant", "content": final})
    return final, messages


async def should_bot_respond_ai_check(cog, bot, summarizer_model_unused, message: discord.Message) -> bool:
    guild_id = str(message.guild.id)
    guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
    is_chat_channel = message.channel.id == guild_config.get("channel")
    is_chat_forum = (
        isinstance(message.channel, discord.Thread)
        and message.channel.parent_id == guild_config.get("forum")
    )

    if is_chat_forum or bot.user in message.mentions:
        return True
    if message.reference and message.reference.resolved and message.reference.resolved.author == bot.user:
        return True
    if not is_chat_channel:
        return False

    history = [msg async for msg in message.channel.history(limit=6)]
    history.reverse()
    conversation_log = "\n".join([f"{m.author.display_name}: {m.clean_content}" for m in history])
    prompt = (
        f"Analyze chat. Should AnTiMa respond to the last message based on context?\n---\n{conversation_log}\n---\n"
        "Answer 'yes' or 'no'."
    )

    try:
        client = get_client()
        response = await throttled_create(client.chat.completions.create(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
        ))
        return "yes" in (response.choices[0].message.content or "").strip().lower()
    except Exception:
        return False


async def handle_single_user_response(cog, message, prompt, author):
    try:
        async with message.channel.typing():
            guild_config = ai_config_collection.find_one({"_id": str(message.guild.id)}) or {}

            raw_history = [m async for m in message.channel.history(limit=MAX_HISTORY) if m.id != message.id]
            history_msgs = _build_history_messages(raw_history, cog.bot.user)

            from .prompts import SYSTEM_PROMPT
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history_msgs

            user_content = [{"type": "text", "text": f"User {author.display_name} says: \"{prompt}\"."}]
            if message.attachments:
                for attachment in message.attachments:
                    img_part = await _encode_attachment(attachment)
                    if img_part:
                        user_content.append(img_part)
                    elif attachment.content_type and attachment.content_type.startswith("video/"):
                        logger.info(f"Skipping video attachment '{attachment.filename}' (video attachments are not supported).")

            messages.append({"role": "user", "content": user_content})

            current_topic = await detect_conversation_topic(message.channel)
            final_text, updated_messages = await _send_and_handle_tool_loop(
                messages, message.channel, current_topic=current_topic
            )

            if not final_text:
                return

            processed_text = re.sub(
                r"\[MENTION: (.+?)\]",
                lambda m: (
                    f"<@{_find_member(message.guild, m.group(1).strip()).id}>"
                    if _find_member(message.guild, m.group(1).strip())
                    else m.group(1).strip()
                ),
                final_text,
            )

            for part in processed_text.split("|||"):
                part = part.strip()
                if not part:
                    continue

                gif_url, gif_match = None, re.search(r"\[GIF: (.+?)\]", part)
                if gif_match:
                    search_term = gif_match.group(1).strip()
                    part = part.replace(gif_match.group(0), "").strip()
                    if await should_send_gif(None, message.channel, part, search_term):
                        gif_url = await get_gif_url(cog.http_session, search_term)

                if part:
                    async with message.channel.typing():
                        await asyncio.sleep(len(part) * 0.02)
                        await message.channel.send(part)
                if gif_url:
                    await message.channel.send(gif_url)

            cog.bot.loop.create_task(
                summarize_and_save_memory(None, author, message.guild.id, updated_messages)
            )

    except Exception as e:
        logger.error(f"Error in handle_single_user_response: {e}")


async def process_message_batch(cog, channel_id):
    batch = cog.message_batches.pop(channel_id, [])
    cog.batch_timers.pop(channel_id, None)
    if not batch:
        return

    last_message = batch[-1]
    unique_authors = list({msg.author for msg in batch})

    if len(unique_authors) == 1 and not any(msg.attachments for msg in batch):
        await handle_single_user_response(
            cog, last_message, "\n".join([m.clean_content for m in batch]), unique_authors[0]
        )
        return

    try:
        async with last_message.channel.typing():
            raw_history = [
                m async for m in last_message.channel.history(limit=MAX_HISTORY)
                if m.id not in [msg.id for msg in batch]
            ]
            history_msgs = _build_history_messages(raw_history, cog.bot.user)

            from .prompts import SYSTEM_PROMPT
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history_msgs

            messages_str_parts = []
            image_parts = []

            for msg in batch:
                messages_str_parts.append(f"- From {msg.author.display_name}: \"{msg.clean_content}\"")
                if msg.attachments:
                    for attachment in msg.attachments:
                        img_part = await _encode_attachment(attachment)
                        if img_part:
                            image_parts.append(img_part)

            user_content = [{"type": "text", "text": "Respond to:\n" + "\n".join(messages_str_parts)}]
            user_content.extend(image_parts)
            messages.append({"role": "user", "content": user_content})

            final_text, updated_messages = await _send_and_handle_tool_loop(messages, last_message.channel)

            if not final_text:
                return

            processed_text = re.sub(
                r"\[MENTION: (.+?)\]",
                lambda m: (
                    f"<@{_find_member(last_message.guild, m.group(1).strip()).id}>"
                    if _find_member(last_message.guild, m.group(1).strip())
                    else m.group(1).strip()
                ),
                final_text,
            )

            for part in processed_text.split("|||"):
                part = part.strip()
                if not part: continue

                gif_url, gif_match = None, re.search(r"\[GIF: (.+?)\]", part)
                if gif_match:
                    search_term = gif_match.group(1).strip()
                    part = part.replace(gif_match.group(0), "").strip()
                    if await should_send_gif(None, last_message.channel, part, search_term):
                        gif_url = await get_gif_url(cog.http_session, search_term)

                if part:
                    async with last_message.channel.typing():
                        await asyncio.sleep(len(part) * 0.02)
                        await last_message.channel.send(part)
                if gif_url:
                    await last_message.channel.send(gif_url)

            for author in unique_authors:
                cog.bot.loop.create_task(
                    summarize_and_save_memory(None, author, last_message.guild.id, updated_messages)
                )

    except Exception as e:
        logger.error(f"Error in process_message_batch: {e}")