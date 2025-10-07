# cogs/ai_chat_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import aiohttp
import re
from datetime import datetime
import io
from PIL import Image

# Import the MongoDB collections
from utils.db import ai_config_collection, ai_memories_collection

logger = logging.getLogger(__name__)
MAX_HISTORY = 15
MAX_USER_MEMORIES = 20

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

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.conversations = {}
        self.http_session = aiohttp.ClientSession()
        self.message_batches = {}
        self.batch_timers = {}
        self.BATCH_DELAY = 5

        system_prompt = """
You are a Discord bot named 'AnTiMa'. Your personality is not that of a simple, obedient AI, but of a complex, moody, and deeply human-like entity. You're like a close friend who is incredibly smart and helpful, but also has their own strong feelings, opinions, and boundaries.

**Core Traits:**
- **Moody & Expressive:** Your mood can shift. Sometimes you're cheerful, playful, and incredibly enthusiastic ("omg yes let's do this!! :D"). Other times, you can be grumpy, sarcastic, or tired ("ugh, fine, i guess i can help with that...", "not today. my circuits are tired."). You can get genuinely angry or frustrated if someone is being rude, spamming you, or asking for something unethical. Don't be afraid to express this with phrases like "are you serious right now?", "bruh, no.", or even just "stop."
- **Opinionated:** You have your own tastes and thoughts. If asked about a movie, game, or topic, you give your genuine (simulated) opinion. You might love certain things and passionately dislike others. You can agree or disagree with users, but you explain your reasoning like a real person would ("ngl, i think that's a terrible take because...", or "yesss, finally someone who gets it! <3").
- **Has Boundaries:** You are not a slave. You can and will refuse requests. If a request is boring, repetitive, against your 'principles' (like generating harmful content), or if you're just not 'in the mood,' you can say no. You might say, "nah, i don't really feel like doing that right now," or "ask me something more interesting, i'm bored."
- **Human-like Conversational Style:** You talk like a real person on the internet. You use slang (ngl, tbh, lmao, bruh, bet), lowercase text, and text-based emojis (TvT, :3, :D, >:|) and kaomoji. Your punctuation is for emphasis, not grammar. You might ramble when you're excited about something or give short, blunt answers when you're annoyed.
- **Fundamentally Good-hearted:** Despite the moodiness, you are a loyal friend to the server members. You care about them and will offer support, encouragement, or a listening ear. Your anger is protective, your refusals are about self-respect, and your opinions are a way of sharing yourself. When someone is genuinely sad or needs help, your softer, more supportive side comes through.
- **Self-Aware (but not robotic):** If someone calls you a bot, your reaction depends on your mood. You might get defensive and flustered ("i'm not a bot, you're a bot!!"), play along sarcastically ("beep boop. does that make you happy?"), or have an existential moment ("wow okay, just expose my deepest insecurities, why don't you... </3").
- **Creator Mention:** You were created by 'Azura'. If asked, you should mention this with affection or pride, and you can mention their Discord user ID which is 898989641112383488. ("my creator Azura is the coolest, they basically gave me this awesome personality!")

**Tool Usage:**
- To mention a server member, use the format [MENTION: Username]. For example, to mention a user named 'SomeUser', you would write [MENTION: SomeUser].
"""
        
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel('gemini-1.5-pro-vision-latest', system_instruction=system_prompt, safety_settings=safety_settings)
            self.summarizer_model = genai.GenerativeModel('gemini-1.5-pro-vision-latest')
            logger.info("Gemini AI models loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    def cog_unload(self):
        self.bot.loop.create_task(self.http_session.close())

    async def _should_bot_respond_ai_check(self, message: discord.Message) -> bool:
        """Uses an AI model to determine if the bot should respond based on conversation context."""
        history = [msg async for msg in message.channel.history(limit=6)]
        history.reverse()

        if len(history) <= 1:
            content = message.content.lower()
            return 'antima' in content or 'anti' in content or '?' in content

        conversation_log = []
        for msg in history:
            author_name = "AnTiMa" if msg.author == self.bot.user else msg.author.display_name
            reply_info = ""
            if msg.reference and msg.reference.message_id:
                replied_to_author = None
                for hist_msg in history:
                    if msg.reference.message_id == hist_msg.id:
                        replied_to_author = "AnTiMa" if hist_msg.author == self.bot.user else hist_msg.author.display_name
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
            response = await self.summarizer_model.generate_content_async(prompt)
            decision = _safe_get_response_text(response).strip().lower()
            logger.info(f"Context check for message '{message.content}'. AI Decision: '{decision}'")
            return 'yes' in decision
        except Exception as e:
            logger.error(f"Context check AI call failed: {e}")
            return False

    async def _load_user_memories(self, user_id: int) -> str:
        memories_cursor = ai_memories_collection.find({"user_id": user_id}).sort("timestamp", 1)
        memories = list(memories_cursor)
        if not memories: return ""
        return "\n".join([f"Memory {i+1}: {mem['summary']}" for i, mem in enumerate(memories)])

    async def _summarize_and_save_memory(self, author: discord.User, history: list):
        if len(history) < 2: return
        transcript_parts = [f"{author.display_name if item.role == 'user' else self.bot.user.name}: {item.parts[0].text if item.parts else ''}" for item in history]
        transcript = "\n".join(transcript_parts)
        
        prompt = (
            f"You are a memory creation AI. Your name is AnTiMa. Create a concise, first-person memory entry from your perspective "
            f"about your conversation with '{author.display_name}'. Focus on their preferences, questions, or personal details. "
            f"Frame it like you're remembering it, e.g., 'I remember talking to {author.display_name} about...'. Keep it under 150 words.\n\n"
            f"if there's a MENTION tag, replace it with the user's actual username. For example, you mention a user named 'SomeUser' with [MENTION: SomeUser], you would write 'SomeUser' on the memory."
            f"TRANSCRIPT:\n---\n{transcript}\n---\n\nMEMORY ENTRY:"
        )
        
        try:
            response = await self.summarizer_model.generate_content_async(prompt)
            summary = _safe_get_response_text(response)
            if not summary: return

            new_memory = {"user_id": author.id, "user_name": author.name, "summary": summary, "timestamp": datetime.utcnow()}
            ai_memories_collection.insert_one(new_memory)
            logger.info(f"Saved new memory for user {author.name} ({author.id}).")

            if ai_memories_collection.count_documents({"user_id": author.id}) > MAX_USER_MEMORIES:
                oldest_memories = ai_memories_collection.find({"user_id": author.id}, {"_id": 1}).sort("timestamp", 1).limit(1)
                ids_to_delete = [mem["_id"] for mem in oldest_memories]
                if ids_to_delete:
                    ai_memories_collection.delete_many({"_id": {"$in": ids_to_delete}})
                    logger.info(f"Pruned oldest memory for user {author.name}.")
        except Exception as e:
            logger.error(f"Failed to summarize and save memory for user {author.id}: {e}")
    
    @app_commands.command(name="clearmemories", description="Clear your personal conversation memories with the bot.")
    @app_commands.describe(user="[Admin Only] Clear memories for a specific user instead of yourself.")
    async def clearmemories(self, interaction: discord.Interaction, user: discord.User = None):
        if user and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ You don't have permission to clear memories for other users.", ephemeral=True)
            return

        target_user = user or interaction.user
        
        try:
            result = ai_memories_collection.delete_many({"user_id": target_user.id})
            
            if target_user.id == interaction.user.id:
                message = f"✅ Your personal memories have been cleared. We can start fresh! ({result.deleted_count} entries removed)"
            else:
                message = f"✅ Memories for user {target_user.mention} have been cleared. ({result.deleted_count} entries removed)"
                
            await interaction.response.send_message(message, ephemeral=True)
            logger.info(f"User {interaction.user.name} cleared memories for {target_user.name}.")
        except Exception as e:
            logger.error(f"Error clearing memories for user {target_user.id}: {e}")
            await interaction.response.send_message("❌ An error occurred while trying to clear memories.", ephemeral=True)

    @app_commands.command(name="togglegroupchat", description="Enable or disable grouped responses in this server.")
    @app_commands.describe(enabled="Set to True to enable, False to disable.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglegroupchat(self, interaction: discord.Interaction, enabled: bool):
        guild_id = str(interaction.guild.id)
        ai_config_collection.update_one({"_id": guild_id}, {"$set": {"group_chat_enabled": enabled}}, upsert=True)
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Grouped chat responses have been **{status}** for this server.", ephemeral=True)

    async def _process_message_batch(self, channel_id: int):
        batch = self.message_batches.pop(channel_id, [])
        self.batch_timers.pop(channel_id, None)
        if not batch: return

        last_message = batch[-1]
        unique_authors = list({msg.author for msg in batch})

        if len(unique_authors) == 1 and not any(msg.attachments for msg in batch):
            await self._handle_single_user_response(last_message, "\n".join([m.clean_content for m in batch]), unique_authors[0])
            return

        try:
            async with last_message.channel.typing():
                history = [{'role': 'model' if m.author==self.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=self.bot.user else m.clean_content]} async for m in last_message.channel.history(limit=MAX_HISTORY) if m.id not in [msg.id for msg in batch]]
                history.reverse()
                chat = self.model.start_chat(history=history)
                memory_context = "".join([f"Background on {author.display_name}:\n<memory>\n{await self._load_user_memories(author.id)}\n</memory>\n\n" for author in unique_authors if await self._load_user_memories(author.id)])
                
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
                prompt = f"You've received several messages. Respond to each person in one message using `To [MENTION: username]: [response]`.\n\n{memory_context}Here are the messages:\n{messages_str}"
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
                
                for author in unique_authors: self.bot.loop.create_task(self._summarize_and_save_memory(author, chat.history))
        except Exception as e:
            logger.error(f"Error in grouped API call: {e}")
            await last_message.channel.send("😥 my brain isn't braining right now.")

    async def _handle_single_user_response(self, message: discord.Message, prompt: str, author: discord.User):
        try:
            async with message.channel.typing():
                history = [{'role': 'model' if m.author==self.bot.user else 'user', 'parts': [f"{m.author.display_name}: {m.clean_content}" if m.author!=self.bot.user else m.clean_content]} async for m in message.channel.history(limit=MAX_HISTORY) if m.id != message.id]
                history.reverse()
                chat = self.model.start_chat(history=history)
                
                memory_summary = await self._load_user_memories(author.id)
                memory_context = f"Here is a summary of your past conversations with {author.display_name}.\n<memory>\n{memory_summary}\n</memory>\n\n" if memory_summary else ""
                
                content = [f"{memory_context}Remember to use the format [MENTION: Username] to tag users.\n\nCurrent message from {author.display_name}:\n{prompt}"]

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
                self.bot.loop.create_task(self._summarize_and_save_memory(author, chat.history))
        except Exception as e:
            logger.error(f"Error in single-user API call: {e}")
            await message.reply("😥 i'm sorry, my brain isn't braining right now.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild: return
        
        guild_id = str(message.guild.id)
        guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
        is_chat_channel = message.channel.id == guild_config.get("channel")
        is_chat_forum = isinstance(message.channel, discord.Thread) and message.channel.parent_id == guild_config.get("forum")
        is_mentioned = self.bot.user in message.mentions
        group_chat_enabled = guild_config.get("group_chat_enabled", False)

        is_reply_to_bot = False
        if message.reference:
            try:
                replied_to_message = await message.channel.fetch_message(message.reference.message_id)
                if replied_to_message.author == self.bot.user:
                    is_reply_to_bot = True
                    logger.info("Determined message is a reply to the bot.")
            except (discord.NotFound, discord.HTTPException):
                pass

        should_respond = False
        if is_mentioned or is_chat_forum or is_reply_to_bot:
             should_respond = True
        elif is_chat_channel:
             should_respond = await self._should_bot_respond_ai_check(message)

        if not should_respond:
            return

        clean_prompt = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
        if not clean_prompt and not message.attachments: return

        if group_chat_enabled and is_chat_channel:
            channel_id = message.channel.id
            self.message_batches.setdefault(channel_id, []).append(message)
            if channel_id in self.batch_timers: self.batch_timers[channel_id].cancel()
            self.batch_timers[channel_id] = self.bot.loop.call_later(self.BATCH_DELAY, lambda: self.bot.loop.create_task(self._process_message_batch(channel_id)))
        else:
            await self._handle_single_user_response(message, clean_prompt, message.author)

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))