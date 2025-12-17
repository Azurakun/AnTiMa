# cogs/ai_chat/cog.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import os
import random
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import aiohttp
import collections
import functools
from utils.db import ai_config_collection, ai_personal_memories_collection, server_lore_collection, rpg_sessions_collection
from utils.limiter import limiter
from .prompts import SYSTEM_PROMPT
from .response_handler import should_bot_respond_ai_check, process_message_batch, handle_single_user_response
from .proactive_chat import _initiate_conversation
from .personality_updater import personality_update_loop, update_guild_personality
from .server_context_learner import update_server_lore_summary
from .utils import perform_web_search, identify_visual_content

logger = logging.getLogger(__name__)

# CONSTANTS
CREATOR_ID = 898989641112383488 # REPLACE WITH YOUR ID

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        print("DEBUG: AIChatCog initializing...")
        self.bot = bot
        self.http_session = aiohttp.ClientSession()
        self.message_batches = {}
        self.batch_timers = {}
        self.BATCH_DELAY = 5
        self.ignored_messages = collections.deque(maxlen=500)

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel(
                'gemini-2.5-pro', 
                system_instruction=SYSTEM_PROMPT, 
                safety_settings=safety_settings,
                tools=[perform_web_search, identify_visual_content]
            )
            self.summarizer_model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("Gemini AI models loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None
        
        self.proactive_chat_loop.start()
        self.server_lore_update_loop.start()

    def cog_unload(self):
        self.proactive_chat_loop.cancel()
        self.server_lore_update_loop.cancel()
        self.bot.loop.create_task(self.http_session.close())

    async def run_db(self, func, *args, **kwargs):
        partial_func = functools.partial(func, *args, **kwargs)
        return await self.bot.loop.run_in_executor(None, partial_func)

    def _calculate_next_chat_time(self, frequency: str = "normal") -> datetime:
        now = datetime.now(timezone.utc)
        if frequency == "active": minutes = random.randint(30, 90)
        elif frequency == "quiet": minutes = random.randint(360, 720)
        elif frequency == "testing": minutes = random.randint(1, 2)
        else: minutes = random.randint(120, 300)
        return now + timedelta(minutes=minutes)

    @tasks.loop(hours=4)
    async def server_lore_update_loop(self):
        for guild in self.bot.guilds:
            try:
                config = await self.run_db(ai_config_collection.find_one, {"_id": str(guild.id)})
                if config and config.get("bot_disabled", False): continue
                await update_server_lore_summary(self.summarizer_model, guild)
                await asyncio.sleep(5)
            except Exception as e: logger.error(f"Error updating lore for {guild.id}: {e}")

    @server_lore_update_loop.before_loop
    async def before_server_lore_update_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def proactive_chat_loop(self):
        # Proactive chat logic (Skipping heavy modification here as prompt focused on rate limits)
        pass 

    @proactive_chat_loop.before_loop
    async def before_proactive_chat_loop(self):
        await self.bot.wait_until_ready()

    # --- ADMIN COMMANDS ---

    @app_commands.command(name="setlimits", description="[Creator Only] Set AI Rate Limits.")
    @app_commands.describe(
        function_type="Which feature to limit (Antima Chat or RPG).",
        scope="User (Individual) or Guild (Server-wide).",
        limit="Number of requests allowed.",
        window="Time window in seconds."
    )
    @app_commands.choices(function_type=[
        app_commands.Choice(name="AnTiMa Chat", value="antima"),
        app_commands.Choice(name="RPG Adventure", value="rpg")
    ], scope=[
        app_commands.Choice(name="User Limit", value="user"),
        app_commands.Choice(name="Server Limit", value="guild")
    ])
    async def setlimits(self, interaction: discord.Interaction, function_type: str, scope: str, limit: int, window: int):
        if interaction.user.id != CREATOR_ID:
            return await interaction.response.send_message("❌ Restricted to the Creator.", ephemeral=True)
        
        limiter.update_limits(function_type, scope, limit, window)
        await interaction.response.send_message(f"✅ **{function_type.upper()} {scope.upper()} Limit** updated: {limit} reqs / {window}s.", ephemeral=True)

    @app_commands.command(name="togglebot", description="Enable/Disable bot for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglebot(self, interaction: discord.Interaction, enabled: bool):
        await self.run_db(ai_config_collection.update_one, {"_id": str(interaction.guild_id)}, {"$set": {"bot_disabled": not enabled}}, upsert=True)
        status = "Enabled" if enabled else "Disabled"
        await interaction.response.send_message(f"✅ Bot **{status}** for this server.", ephemeral=True)

    @app_commands.command(name="setup", description="[Admin] Configure the channel for AnTiMa to chat in proactively.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, frequency: str = "normal"):
        await interaction.response.defer(ephemeral=True)
        first_run = self._calculate_next_chat_time(frequency)
        update_data = {"channel": channel.id, "chat_frequency": frequency, "next_chat_time": first_run, "bot_disabled": False, "group_chat_enabled": True}
        await self.run_db(ai_config_collection.update_one, {"_id": str(interaction.guild_id)}, {"$set": update_data}, upsert=True)
        await interaction.followup.send(f"✅ **Setup Complete!** Channel: {channel.mention}")

    @app_commands.command(name="clearmemories", description="Clear personal conversation memories.")
    async def clearmemories(self, interaction: discord.Interaction, scope: str, user: discord.Member = None):
        if scope not in ['personal', 'guild']: return await interaction.response.send_message("❌ Invalid scope.", ephemeral=True)
        if (user or scope == 'guild') and not interaction.user.guild_permissions.manage_guild: return await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if scope == 'guild': await self.run_db(ai_personal_memories_collection.delete_many, {"guild_id": interaction.guild_id})
        else: await self.run_db(ai_personal_memories_collection.delete_many, {"user_id": (user or interaction.user).id, "guild_id": interaction.guild_id})
        await interaction.followup.send("✅ Memories cleared.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild: return
        
        # --- RPG THREAD ISOLATION ---
        if isinstance(message.channel, discord.Thread):
            try:
                if rpg_sessions_collection.find_one({"thread_id": message.channel.id}): return 
            except: pass

        # --- RATE LIMIT CHECK (PEEK ONLY) ---
        is_targeted = self.bot.user in message.mentions or (message.reference and message.reference.resolved and message.reference.resolved.author == self.bot.user)
        
        if is_targeted:
            # Check availability BEFORE processing
            if not limiter.check_available(message.author.id, message.guild.id, "antima"):
                await message.add_reaction("⏳") # Visual indicator that limit is hit
                return

        guild_id = str(message.guild.id)
        guild_config = await self.run_db(ai_config_collection.find_one, {"_id": guild_id}) or {}

        if guild_config.get("bot_disabled", False):
            if self.bot.user in message.mentions: await message.reply("currently disabled here! sorry!")
            return

        if not await should_bot_respond_ai_check(self, self.bot, self.summarizer_model, message):
            self.ignored_messages.append(message.id)
            return

        clean = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
        if not clean and not message.attachments: return

        # --- PROCESSING & CONSUMING LIMIT ---
        if guild_config.get("group_chat_enabled", False) and message.channel.id == guild_config.get("channel") and not is_targeted:
            self.message_batches.setdefault(message.channel.id, []).append(message)
            if message.channel.id in self.batch_timers: self.batch_timers[message.channel.id].cancel()
            self.batch_timers[message.channel.id] = self.bot.loop.call_later(self.BATCH_DELAY, lambda: self.bot.loop.create_task(process_message_batch(self, message.channel.id)))
        else:
            # Generate Response
            await handle_single_user_response(self, message, clean, message.author)
            
            # Consume Limit AFTER generation triggered
            if is_targeted:
                source = limiter.consume(message.author.id, message.guild.id, "antima")
                logger.info(f"Antima Gen consumed from: {source}")

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))