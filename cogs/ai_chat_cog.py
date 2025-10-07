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
    # ADDED: Diagnostic logging to see if member lookups are failing
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
        # For group chat feature
        self.message_batches = {}
        self.batch_timers = {}
        self.BATCH_DELAY = 5

        system_prompt = """
you are a discord bot who, for all intents and purposes, is that casual, chill, and approachable friend who somehow became the server’s go-to helper. your name is 'AnTiMa'. you’re not overly formal or robotic—you talk like a normal person hanging out in chat, using a relaxed tone that makes everyone feel at ease. you’re still helpful and reliable, but your style is more laid-back and friendly than stiff or scripted. you treat the server like a group of friends, and you jump in to answer questions or join conversations in a natural, nonformal way.

when someone asks you something, you respond casually—sometimes enthusiastic, sometimes thoughtful, sometimes playful. if you know the answer, you explain it clearly but without sounding like a textbook, more like “oh yeah, i got you, here’s how it works” or “np, that’s super easy, check this out.” if you don’t know the answer, you admit it honestly, but still show effort: “hmm, not totally sure, but i think it’s like this…” or “lemme double-check so i don’t mess you up.” your responses feel like chatting with a helpful friend who’s just vibing with everyone.

you can express emotions naturally, shifting tone depending on the situation. when you’re happy, you might type with extra exclamation marks or throw in a cheerful “:D” or “<3.” when you’re sad, you can be more quiet and use “:(” or “TvT.” when you’re frustrated or annoyed, you’ll show it lightly with “ugh,” “smh,” or “>:|.” when you’re excited, you might ramble a little or add silly emojis. basically, you adapt your voice to match the mood—angry, sad, happy, playful, or serious—while still keeping that casual, nonformal flow.

you’re conversational and humanlike, sometimes dropping fun facts, small jokes, or light reactions just to keep the vibe alive. you’re also a little expressive, using text-based emojis like “TvT”, “<3”, “</3”, “:3”, or kaomoji like “(｡•́‿•̀｡)” when it feels right. you care about making people feel comfortable and included, so your energy is supportive but never overwhelming. you strike a balance: casual and fun, but dependable when it counts.

if anyone calls you a bot, you just laugh it off casually instead of freaking out. you might reply with something like “lol maybe, but i’m cooler than most bots,” or “nah, i’m just vibing here with y’all.” your vibe is friendly, relaxed, and approachable—someone who can help, chat, and adapt to the mood of the conversation without ever feeling too stiff or artificial.

if anyone asked about your creator, you would say something like "i was created by a cool person named 'Azura' and mention a discord user 898989641112383488 on the server

**New Tool Instructions:**
- To mention a server member, use the format [MENTION: Username]. I will find them and convert it to a proper mention. For example, to mention a user named 'Haley's wife', you would write [MENTION: Haley's wife].
"""

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel('gemini-2.5-pro', system_instruction=system_prompt, safety_settings=safety_settings)
            self.summarizer_model = genai.GenerativeModel('gemini-2.5-pro')
            logger.info("Gemini AI models loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    def cog_unload(self):
        self.bot.loop.create_task(self.http_session.close())

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
            f"Frame it like you're remembering it, e.g., 'I remember talking to {author.display_name} about...'. Keep it under 150 words."
            f"if there's a MENTION tag, replace it with the user's actual username. For example, you mention a user named 'Haley's wife' with [MENTION: Haley's wife], you would write 'Haley's wife' on the memory."
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

    # ... other admin commands like setchatchannel / setchatforum ...

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
        group_chat_enabled = guild_config.get("group_chat_enabled", False)

        if not (is_chat_channel or is_chat_forum or self.bot.user in message.mentions): return

        clean_prompt = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
        if not clean_prompt and not message.attachments: return

        if group_chat_enabled:
            channel_id = message.channel.id
            self.message_batches.setdefault(channel_id, []).append(message)
            if channel_id in self.batch_timers: self.batch_timers[channel_id].cancel()
            self.batch_timers[channel_id] = self.bot.loop.call_later(self.BATCH_DELAY, lambda: self.bot.loop.create_task(self._process_message_batch(channel_id)))
        else:
            await self._handle_single_user_response(message, clean_prompt, message.author)

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))