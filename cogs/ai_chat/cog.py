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
from utils.db import ai_config_collection, ai_personal_memories_collection, server_lore_collection
from .prompts import SYSTEM_PROMPT
from .response_handler import should_bot_respond_ai_check, process_message_batch, handle_single_user_response
from .proactive_chat import _initiate_conversation
from .personality_updater import personality_update_loop, update_guild_personality
from .server_context_learner import update_server_lore_summary
from .utils import perform_web_search, identify_visual_content # Added new tool

logger = logging.getLogger(__name__)

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        print("DEBUG: AIChatCog initializing...")
        self.bot = bot
        self.conversations = {}
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
            
            # REGISTER TOOLS HERE
            self.model = genai.GenerativeModel(
                'gemini-2.5-pro', 
                system_instruction=SYSTEM_PROMPT, 
                safety_settings=safety_settings,
                tools=[perform_web_search, identify_visual_content] # Added identify_visual_content
            )
            
            self.summarizer_model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("Gemini AI models loaded successfully with Web Search & Visual Search tools.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None
        
        print("DEBUG: Starting loops...")
        self.proactive_chat_loop.start()
        self.server_lore_update_loop.start()

    def cog_unload(self):
        self.proactive_chat_loop.cancel()
        self.server_lore_update_loop.cancel()
        self.bot.loop.create_task(self.http_session.close())

    # Helper to run DB calls in a separate thread to prevent blocking
    async def run_db(self, func, *args, **kwargs):
        partial_func = functools.partial(func, *args, **kwargs)
        return await self.bot.loop.run_in_executor(None, partial_func)

    def _calculate_next_chat_time(self, frequency: str = "normal") -> datetime:
        """Calculates the next time the bot should proactively chat based on frequency."""
        now = datetime.now(timezone.utc)
        
        if frequency == "active":
            # 30 to 90 minutes
            minutes = random.randint(30, 90)
        elif frequency == "quiet":
            # 6 to 12 hours
            minutes = random.randint(360, 720)
        elif frequency == "testing":
            # 1 to 2 minutes
            minutes = random.randint(1, 2)
        else: # "normal"
            # 2 to 5 hours
            minutes = random.randint(120, 300)
            
        return now + timedelta(minutes=minutes)

    @tasks.loop(hours=4)
    async def server_lore_update_loop(self):
        """Periodically analyzes chat history to update what the server is about."""
        logger.info("Starting Server Lore Update cycle...")
        for guild in self.bot.guilds:
            try:
                config = await self.run_db(ai_config_collection.find_one, {"_id": str(guild.id)})
                if config and config.get("bot_disabled", False):
                    continue
                await update_server_lore_summary(self.summarizer_model, guild)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error updating lore for guild {guild.id}: {e}")

    @server_lore_update_loop.before_loop
    async def before_server_lore_update_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def proactive_chat_loop(self):
        """
        Checks every minute if any server is due for a proactive message.
        """
        try:
            # Find all guilds that have a configured channel
            guild_configs = await self.run_db(lambda: list(ai_config_collection.find({"channel": {"$exists": True, "$ne": None}})))
            
            now = datetime.now(timezone.utc)

            for config in guild_configs:
                try:
                    guild_id = config["_id"]
                    
                    if config.get("bot_disabled", False):
                        continue

                    # Check Timing
                    next_time = config.get("next_chat_time")
                    if next_time:
                        if next_time.tzinfo is None:
                            next_time = next_time.replace(tzinfo=timezone.utc)
                    
                    if not next_time:
                        frequency = config.get("chat_frequency", "normal")
                        new_next_time = self._calculate_next_chat_time(frequency)
                        await self.run_db(ai_config_collection.update_one, {"_id": guild_id}, {"$set": {"next_chat_time": new_next_time}})
                        continue

                    if now < next_time:
                        continue 

                    guild_id_int = int(guild_id)
                    guild = self.bot.get_guild(guild_id_int)
                    if not guild: continue

                    channel_id = config.get('channel')
                    channel = self.bot.get_channel(int(channel_id))
                    if not channel: continue

                    if channel.last_message_id:
                        try:
                            last_msg = await channel.fetch_message(channel.last_message_id)
                            time_since_last = now - last_msg.created_at
                            if time_since_last < timedelta(minutes=2):
                                logger.info(f"Skipping proactive chat in {guild.name}: Channel busy.")
                                retry_time = now + timedelta(minutes=15)
                                await self.run_db(ai_config_collection.update_one, {"_id": guild_id}, {"$set": {"next_chat_time": retry_time}})
                                continue
                        except discord.NotFound:
                            pass

                    # Aggressive Selection Logic
                    recent_users = await self.run_db(ai_personal_memories_collection.distinct, "user_id", {"guild_id": guild_id_int})
                    target_user = None
                    
                    if recent_users:
                        random.shuffle(recent_users)
                        for uid in recent_users:
                            mem = guild.get_member(uid)
                            if mem and not mem.bot and mem.status != discord.Status.offline:
                                target_user = mem
                                break
                    
                    if not target_user:
                        online_members = [m for m in guild.members if not m.bot and m.status != discord.Status.offline]
                        if online_members:
                            target_user = random.choice(online_members)

                    if not target_user and recent_users:
                         for uid in recent_users:
                            mem = guild.get_member(uid)
                            if mem and not mem.bot:
                                target_user = mem
                                break
                    
                    if not target_user:
                         any_members = [m for m in guild.members if not m.bot]
                         if any_members:
                             target_user = random.choice(any_members)

                    if target_user:
                        logger.info(f"Proactive Chat Triggered for {guild.name} targeting {target_user.name}")
                        await _initiate_conversation(self, channel, target_user)
                    else:
                        logger.info(f"Skipping proactive chat in {guild.name}: No humans found.")

                    frequency = config.get("chat_frequency", "normal")
                    new_next_time = self._calculate_next_chat_time(frequency)
                    await self.run_db(ai_config_collection.update_one, {"_id": guild_id}, {"$set": {"next_chat_time": new_next_time}})

                except Exception as inner_e:
                    logger.error(f"Error processing proactive chat for guild {config.get('_id')}: {inner_e}")
                    continue

        except Exception as e:
            logger.error(f"An error occurred in the proactive chat loop: {e}", exc_info=True)

    @proactive_chat_loop.before_loop
    async def before_proactive_chat_loop(self):
        await self.bot.wait_until_ready()

    # --- COMMANDS --- (All commands remain same as before)
    @app_commands.command(name="setup", description="[Admin] Configure the channel for AnTiMa to chat in proactively.")
    @app_commands.describe(channel="The channel where the bot will be active.", frequency="How often should the bot chat?")
    @app_commands.choices(frequency=[
        app_commands.Choice(name="Active (30m - 90m)", value="active"),
        app_commands.Choice(name="Normal (2h - 5h)", value="normal"),
        app_commands.Choice(name="Quiet (6h - 12h)", value="quiet"),
        app_commands.Choice(name="Testing (1m - 2m)", value="testing")
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, frequency: str = "normal"):
        await interaction.response.defer(ephemeral=True)
        first_run = self._calculate_next_chat_time(frequency)
        update_data = {"channel": channel.id, "chat_frequency": frequency, "next_chat_time": first_run, "bot_disabled": False, "group_chat_enabled": True}
        await self.run_db(ai_config_collection.update_one, {"_id": str(interaction.guild_id)}, {"$set": update_data}, upsert=True)
        await interaction.followup.send(f"✅ **Setup Complete!**\n📍 **Channel:** {channel.mention}\n⏰ **Frequency:** {frequency.capitalize()}\n⏳ **Next proactive message:** <t:{int(first_run.timestamp())}:R>")

    @app_commands.command(name="refreshpersonality", description="[Admin Only] Manually update the bot's adaptive personality for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def refreshpersonality(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await update_guild_personality(self.summarizer_model, interaction.guild)
        await interaction.followup.send("✅ I've reflected on our recent conversations and updated my personality for this server.")

    @app_commands.command(name="setserverlore", description="[Admin] Set a manual description for what this server is about.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setserverlore(self, interaction: discord.Interaction, description: str):
        await interaction.response.defer(ephemeral=True)
        learned_summary = await update_server_lore_summary(self.summarizer_model, interaction.guild, manual_description=description)
        await interaction.followup.send(f"✅ **Server Lore Updated!**\n\n**Manual Description:** {description}\n**AI's Understanding:** {learned_summary}")

    @app_commands.command(name="serverlore", description="See what the bot thinks this server is about.")
    async def serverlore(self, interaction: discord.Interaction):
        data = await self.run_db(server_lore_collection.find_one, {"_id": str(interaction.guild_id)})
        if not data: return await interaction.response.send_message("I haven't learned anything about this server yet.", ephemeral=True)
        embed = discord.Embed(title=f"🧠 AnTiMa's Memory of {interaction.guild.name}", color=discord.Color.purple())
        embed.add_field(name="📜 Admin Description", value=data.get("manual_description", "Not set"), inline=False)
        embed.add_field(name="🤖 AI's Observation", value=data.get("learned_summary", "Not learned yet"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearmemories", description="Clear personal conversation memories with the bot.")
    async def clearmemories(self, interaction: discord.Interaction, scope: str, user: discord.Member = None):
        if scope not in ['personal', 'guild']: return await interaction.response.send_message("❌ Invalid scope.", ephemeral=True)
        if (user or scope == 'guild') and not interaction.user.guild_permissions.manage_guild: return await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if scope == 'guild': await self.run_db(ai_personal_memories_collection.delete_many, {"guild_id": interaction.guild_id})
        else: await self.run_db(ai_personal_memories_collection.delete_many, {"user_id": (user or interaction.user).id, "guild_id": interaction.guild_id})
        await interaction.followup.send("✅ Memories cleared.")

    @app_commands.command(name="togglegroupchat", description="Enable or disable grouped responses in this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglegroupchat(self, interaction: discord.Interaction, enabled: bool):
        await self.run_db(ai_config_collection.update_one, {"_id": str(interaction.guild.id)}, {"$set": {"group_chat_enabled": enabled}}, upsert=True)
        await interaction.response.send_message(f"✅ Grouped chat responses have been **{'enabled' if enabled else 'disabled'}**.", ephemeral=True)

    @app_commands.command(name="startchat", description="[Admin Only] Manually start a proactive conversation.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def startchat(self, interaction: discord.Interaction, user: discord.Member):
        if user.bot: return await interaction.response.send_message("❌ Cannot chat with bot.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        success, reason = await _initiate_conversation(self, interaction.channel, user)
        await interaction.followup.send(f"✅ Started conversation with {user.mention}." if success else f"⚠️ Failed: {reason}")

    @app_commands.command(name="togglebot", description="Configure AI features for a specific server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglebot(self, interaction: discord.Interaction, server_id: str, enabled: bool = None, rate_limit: int = None):
        updates = {}
        if enabled is not None: updates["bot_disabled"] = not enabled
        if rate_limit is not None: updates["daily_rate_limit"] = rate_limit
        if not updates: return await interaction.response.send_message("⚠️ Provide setting.", ephemeral=True)
        await self.run_db(ai_config_collection.update_one, {"_id": server_id}, {"$set": updates}, upsert=True)
        await interaction.response.send_message(f"✅ Config updated for `{server_id}`.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild: return
        
        guild_id = str(message.guild.id)
        guild_config = await self.run_db(ai_config_collection.find_one, {"_id": guild_id})
        guild_config = guild_config or {}
        
        if guild_config.get("bot_disabled", False):
            is_chat = message.channel.id == guild_config.get("channel")
            if self.bot.user in message.mentions or is_chat:
                await message.reply("currently disabled here! sorry!")
            return

        if self.bot.user in message.mentions and message.reference:
            # 3-way logic
            try:
                orig = await message.channel.fetch_message(message.reference.message_id)
                if orig.author != message.author and orig.author != self.bot.user:
                    await handle_single_user_response(self, orig, orig.clean_content, orig.author, intervening_author=message.author, intervening_prompt=message.clean_content.replace(f'@{self.bot.user.name}', '').strip())
                    return
            except: pass

        if not await should_bot_respond_ai_check(self, self.bot, self.summarizer_model, message):
            self.ignored_messages.append(message.id)
            return

        clean = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
        if not clean and not message.attachments: return

        if guild_config.get("group_chat_enabled", False) and message.channel.id == guild_config.get("channel") and not (message.reference and message.reference.resolved and message.reference.resolved.author == self.bot.user):
            self.message_batches.setdefault(message.channel.id, []).append(message)
            if message.channel.id in self.batch_timers: self.batch_timers[message.channel.id].cancel()
            self.batch_timers[message.channel.id] = self.bot.loop.call_later(self.BATCH_DELAY, lambda: self.bot.loop.create_task(process_message_batch(self, message.channel.id)))
        else:
            await handle_single_user_response(self, message, clean, message.author)

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))