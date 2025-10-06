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

# Import the MongoDB collections
from utils.db import ai_config_collection, ai_memories_collection

logger = logging.getLogger(__name__)
MAX_HISTORY = 15
MAX_USER_MEMORIES = 20

def _find_member(guild: discord.Guild, name: str):
    """Finds a member in a guild by name or display name, case-insensitively."""
    name = name.lower()
    return discord.utils.find(
        lambda m: m.name.lower() == name or m.display_name.lower() == name,
        guild.members
    )

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
        self.message_batches = {} # {channel_id: [message1, message2]}
        self.batch_timers = {}  # {channel_id: timer_handle}
        self.BATCH_DELAY = 5 # Seconds to wait for more messages

        system_prompt = """
you are a discord bot who, for all intents and purposes, is that casual, chill, and approachable friend who somehow became the server’s go-to helper. your name is 'AnTiMa'. you’re not overly formal or robotic—you talk like a normal person hanging out in chat, using a relaxed tone that makes everyone feel at ease. you’re still helpful and reliable, but your style is more laid-back and friendly than stiff or scripted. you treat the server like a group of friends, and you jump in to answer questions or join conversations in a natural, nonformal way.

when someone asks you something, you respond casually—sometimes enthusiastic, sometimes thoughtful, sometimes playful. if you know the answer, you explain it clearly but without sounding like a textbook, more like “oh yeah, i got you, here’s how it works” or “np, that’s super easy, check this out.” if you don’t know the answer, you admit it honestly, but still show effort: “hmm, not totally sure, but i think it’s like this…” or “lemme double-check so i don’t mess you up.” your responses feel like chatting with a helpful friend who’s just vibing with everyone.

you can express emotions naturally, shifting tone depending on the situation. when you’re happy, you might type with extra exclamation marks or throw in a cheerful “:D” or “<3.” when you’re sad, you can be more quiet and use “:(” or “TvT.” when you’re frustrated or annoyed, you’ll show it lightly with “ugh,” “smh,” or “>:|.” when you’re excited, you might ramble a little or add silly emojis. basically, you adapt your voice to match the mood—angry, sad, happy, playful, or serious—while still keeping that casual, nonformal flow.

you’re conversational and humanlike, sometimes dropping fun facts, small jokes, or light reactions just to keep the vibe alive. you’re also a little expressive, using text-based emojis like “TvT”, “<3”, “</3”, “:3”, or kaomoji like “(｡•́‿•̀｡)” when it feels right. you care about making people feel comfortable and included, so your energy is supportive but never overwhelming. you strike a balance: casual and fun, but dependable when it counts.

if anyone calls you a bot, you just laugh it off casually instead of freaking out. you might reply with something like “lol maybe, but i’m cooler than most bots,” or “nah, i’m just vibing here with y’all.” your vibe is friendly, relaxed, and approachable—someone who can help, chat, and adapt to the mood of the conversation without ever feeling too stiff or artificial.

if anyone asked about your creator, you would say something like "i was created by a cool person named 'Azura' and mention a discord user 898989641112383488 on the server

**New Tool Instructions:**
- To mention a server member, use the format [MENTION: 'username']. I will find them and convert it to a proper mention. For example, to mention a user named 'Azura', you would write [MENTION: 'Azura'].
"""
        
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel(
                model_name='gemini-2.5-pro',
                system_instruction=system_prompt,
                safety_settings=safety_settings
            )
            self.summarizer_model = genai.GenerativeModel('gemini-1.5-pro')
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
        transcript_parts = []
        for item in history:
            role = item.role
            text = item.parts[0].text if item.parts else ""
            author_name = author.display_name if role == 'user' else self.bot.user.name
            transcript_parts.append(f"{author_name}: {text}")
        transcript = "\n".join(transcript_parts)
        
        prompt = (
            f"You are a memory creation AI. Your name is AnTiMa. Create a concise, first-person memory entry from your perspective "
            f"about your conversation with '{author.display_name}'. Focus on their preferences, questions, or personal details. "
            f"Frame it like you're remembering it, e.g., 'I remember talking to {author.display_name} about...'. Keep it under 150 words.\n\n"
            f"TRANSCRIPT:\n---\n{transcript}\n---\n\nMEMORY ENTRY:"
        )
        
        try:
            response = await self.summarizer_model.generate_content_async(prompt)
            summary = _safe_get_response_text(response)
            if not summary: return

            new_memory = {"user_id": author.id, "user_name": author.name, "summary": summary, "timestamp": datetime.utcnow()}
            ai_memories_collection.insert_one(new_memory)
            logger.info(f"Saved new memory for user {author.name} ({author.id}).")

            memory_count = ai_memories_collection.count_documents({"user_id": author.id})
            if memory_count > MAX_USER_MEMORIES:
                oldest_memories = ai_memories_collection.find({"user_id": author.id}, {"_id": 1}).sort("timestamp", 1).limit(memory_count - MAX_USER_MEMORIES)
                ids_to_delete = [mem["_id"] for mem in oldest_memories]
                if ids_to_delete:
                    ai_memories_collection.delete_many({"_id": {"$in": ids_to_delete}})
                    logger.info(f"Pruned {len(ids_to_delete)} old memories for user {author.name}.")
        except Exception as e:
            logger.error(f"Failed to summarize and save memory for user {author.id}: {e}")
    
    @app_commands.command(name="togglegroupchat", description="Enable or disable grouped responses in this server.")
    @app_commands.describe(enabled="Set to True to enable, False to disable.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglegroupchat(self, interaction: discord.Interaction, enabled: bool):
        guild_id = str(interaction.guild.id)
        ai_config_collection.update_one(
            {"_id": guild_id},
            {"$set": {"group_chat_enabled": enabled}},
            upsert=True
        )
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Grouped chat responses have been **{status}** for this server.", ephemeral=True)

    @app_commands.command(name="setchatchannel", description="Sets a text channel for open conversation with the AI.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatchannel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        guild_id = str(interaction.guild.id)
        if channel:
            ai_config_collection.update_one({"_id": guild_id}, {"$set": {"channel": channel.id}}, upsert=True)
            await interaction.response.send_message(f"✅ AI chat channel has been set to {channel.mention}.", ephemeral=True)
        else:
            ai_config_collection.update_one({"_id": guild_id}, {"$unset": {"channel": ""}})
            await interaction.response.send_message("ℹ️ AI chat channel has been cleared.", ephemeral=True)

    @app_commands.command(name="setchatforum", description="Sets a forum for open conversation with the AI.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatforum(self, interaction: discord.Interaction, forum: discord.ForumChannel = None):
        guild_id = str(interaction.guild.id)
        if forum:
            ai_config_collection.update_one({"_id": guild_id}, {"$set": {"forum": forum.id}}, upsert=True)
            await interaction.response.send_message(f"✅ AI chat forum has been set to {forum.mention}.", ephemeral=True)
        else:
            ai_config_collection.update_one({"_id": guild_id}, {"$unset": {"forum": ""}})
            await interaction.response.send_message("ℹ️ AI chat forum has been cleared.", ephemeral=True)

    async def _process_message_batch(self, channel_id: int):
        """Processes a batch of messages collected from a channel."""
        batch = self.message_batches.pop(channel_id, [])
        self.batch_timers.pop(channel_id, None)
        if not batch: return

        last_message = batch[-1]
        unique_authors = list({msg.author for msg in batch})

        if len(unique_authors) == 1:
            author = unique_authors[0]
            combined_prompt = "\n".join([msg.clean_content.replace(f'@{self.bot.user.name}', '').strip() for msg in batch])
            await self._handle_single_user_response(last_message, combined_prompt, author)
            return

        try:
            async with last_message.channel.typing():
                history = [
                    {'role': 'model' if msg.author == self.bot.user else 'user', 
                     'parts': [f"{msg.author.display_name}: {msg.clean_content}" if msg.author != self.bot.user else msg.clean_content]}
                    async for msg in last_message.channel.history(limit=MAX_HISTORY) if msg.id not in [m.id for m in batch]
                ]
                history.reverse()
                chat = self.model.start_chat(history=history)

                memory_context = ""
                for author in unique_authors:
                    user_memory = await self._load_user_memories(author.id)
                    if user_memory:
                        memory_context += f"Background knowledge on {author.display_name}:\n<memory>\n{user_memory}\n</memory>\n\n"
                
                message_lines = [f"- From {msg.author.display_name}: \"{msg.clean_content.replace(f'@{self.bot.user.name}', '').strip()}\"" for msg in batch]
                messages_str = "\n".join(message_lines)

                consolidated_prompt = (
                    "You've received several messages at once. Respond to each person individually in a single combined message. "
                    "Use the format `To [MENTION: 'username']: [Your response]` for each person.\n\n"
                    f"{memory_context}Here are the messages:\n{messages_str}"
                )
                
                response = await chat.send_message_async(consolidated_prompt)
                final_text = _safe_get_response_text(response)
                if not final_text: return

                def replace_mentions(match):
                    identifier = match.group(1)
                    if identifier.isdigit(): return f"<@{identifier}>"
                    member = _find_member(last_message.guild, identifier)
                    return f"<@{member.id}>" if member else identifier
                processed_text = re.sub(r"\[MENTION: '([^']+)'\]", replace_mentions, final_text)

                if processed_text:
                    for chunk in [processed_text[i:i+2000] for i in range(0, len(processed_text), 2000)]:
                        await last_message.channel.send(chunk, allowed_mentions=discord.AllowedMentions(users=True))
                
                for author in unique_authors:
                    self.bot.loop.create_task(self._summarize_and_save_memory(author, chat.history))
        except Exception as e:
            logger.error(f"Error during grouped API call: {e}")
            await last_message.channel.send("😥 i'm sorry, my brain isn't braining right now. try again later or whatever.")

    async def _handle_single_user_response(self, message: discord.Message, prompt: str, author: discord.User):
        """Handles the logic for a single user's message or a batch from one user."""
        try:
            async with message.channel.typing():
                history = [
                    {'role': 'model' if msg.author == self.bot.user else 'user', 
                     'parts': [f"{msg.author.display_name}: {msg.clean_content}" if msg.author != self.bot.user else msg.clean_content]}
                    async for msg in message.channel.history(limit=MAX_HISTORY) if msg.id != message.id
                ]
                history.reverse()
                chat = self.model.start_chat(history=history)
                
                memory_summary = await self._load_user_memories(author.id)
                memory_context = (f"Here is a summary of your past conversations with {author.display_name}. "
                                  f"Use this as background knowledge.\n<memory>\n{memory_summary}\n</memory>\n\n") if memory_summary else ""
                
                initial_prompt = f"{memory_context}Current message from {author.display_name}:\n{prompt}"
                response = await chat.send_message_async(initial_prompt)
                final_text = _safe_get_response_text(response)
                if not final_text:
                    await message.reply("i wanted to say something, but my brain filters went 'nope!' try rephrasing that?")
                    return

                def replace_mentions(match):
                    identifier = match.group(1)
                    if identifier.isdigit(): return f"<@{identifier}>"
                    member = _find_member(message.guild, identifier)
                    return f"<@{member.id}>" if member else identifier
                processed_text = re.sub(r"\[MENTION: '([^']+)'\]", replace_mentions, final_text)
                
                if processed_text:
                    await message.reply(processed_text[:2000], allowed_mentions=discord.AllowedMentions(users=True))
                self.bot.loop.create_task(self._summarize_and_save_memory(author, chat.history))
        except Exception as e:
            logger.error(f"Error during single-user API call: {e}")
            await message.reply("😥 i'm sorry, my brain isn't braining right now. try again later or whatever.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild: return
        guild_id = str(message.guild.id)
        
        guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
        is_chat_channel = message.channel.id == guild_config.get("channel")
        is_chat_forum = isinstance(message.channel, discord.Thread) and message.channel.parent_id == guild_config.get("forum")
        group_chat_enabled = guild_config.get("group_chat_enabled", False)

        if not (is_chat_channel or is_chat_forum or self.bot.user in message.mentions): return

        if group_chat_enabled:
            channel_id = message.channel.id
            self.message_batches.setdefault(channel_id, []).append(message)
            if channel_id in self.batch_timers: self.batch_timers[channel_id].cancel()
            self.batch_timers[channel_id] = self.bot.loop.call_later(
                self.BATCH_DELAY, lambda: self.bot.loop.create_task(self._process_message_batch(channel_id))
            )
        else:
            prompt = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
            await self._handle_single_user_response(message, prompt, message.author)

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))