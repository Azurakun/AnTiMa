# cogs/ai_chat/cog.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import os
import random
import asyncio
from datetime import datetime, timedelta, timezone
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import aiohttp
import collections
import functools
# Updated imports to ensure they match utils/db.py
from utils.db import ai_config_collection, ai_personal_memories_collection, server_lore_collection, rpg_sessions_collection, web_actions_collection
from utils.limiter import limiter
from .prompts import SYSTEM_PROMPT
from .response_handler import should_bot_respond_ai_check, process_message_batch, handle_single_user_response
from .proactive_chat import _initiate_conversation
from .personality_updater import personality_update_loop, update_guild_personality
from .server_context_learner import update_server_lore_summary
from .utils import perform_web_search, identify_visual_content

logger = logging.getLogger(__name__)

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
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
            self.model = genai.GenerativeModel('gemini-2.5-pro', system_instruction=SYSTEM_PROMPT, safety_settings=safety_settings, tools=[perform_web_search, identify_visual_content])
            self.summarizer_model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("Gemini AI models loaded.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None
        
        self.proactive_chat_loop.start()
        self.server_lore_update_loop.start()
        self.check_reload_requests.start()

    def cog_unload(self):
        self.proactive_chat_loop.cancel()
        self.server_lore_update_loop.cancel()
        self.check_reload_requests.cancel()
        self.bot.loop.create_task(self.http_session.close())

    async def run_db(self, func, *args, **kwargs):
        partial_func = functools.partial(func, *args, **kwargs)
        return await self.bot.loop.run_in_executor(None, partial_func)

    def _calculate_next_chat_time(self, frequency: str = "normal") -> datetime | None:
        if frequency == "disabled": return None
        now = datetime.now(timezone.utc)
        if frequency == "active": minutes = random.randint(30, 90)
        elif frequency == "quiet": minutes = random.randint(360, 720)
        elif frequency == "testing": minutes = random.randint(1, 2)
        else: minutes = random.randint(120, 300)
        return now + timedelta(minutes=minutes)

    @tasks.loop(seconds=3)
    async def check_reload_requests(self):
        """Watches for restart signals from the dashboard for instant apply."""
        try:
            req = await self.run_db(web_actions_collection.find_one_and_update, 
                {"type": "reload_chat", "status": "pending"},
                {"$set": {"status": "completed"}}
            )
            if req:
                logger.info("♻️ Reload signal received. Restarting Proactive Chat Loop...")
                self.proactive_chat_loop.restart()
        except Exception: pass

    @check_reload_requests.before_loop
    async def before_check_reload_requests(self):
        await self.bot.wait_until_ready()

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
        try:
            guild_configs = await self.run_db(lambda: list(ai_config_collection.find({"channel": {"$exists": True, "$ne": None}})))
            now = datetime.now(timezone.utc)
            for config in guild_configs:
                try:
                    guild_id = config["_id"]
                    
                    # 1. Immediate Disabled Check
                    if config.get("bot_disabled", False): continue
                    
                    # 2. Frequency Check
                    freq = config.get("chat_frequency", "normal")
                    if freq == "disabled": continue

                    # 3. Time Check
                    next_time = config.get("next_chat_time")
                    if next_time and next_time.tzinfo is None: next_time = next_time.replace(tzinfo=timezone.utc)
                    
                    if not next_time:
                        new_next_time = self._calculate_next_chat_time(freq)
                        if new_next_time:
                            await self.run_db(ai_config_collection.update_one, {"_id": guild_id}, {"$set": {"next_chat_time": new_next_time}})
                        continue

                    if now < next_time: continue 
                    
                    # 4. Logic Execution
                    guild = self.bot.get_guild(int(guild_id))
                    channel = self.bot.get_channel(int(config.get('channel')))
                    if not guild or not channel: continue

                    if channel.last_message_id:
                        try:
                            last_msg = await channel.fetch_message(channel.last_message_id)
                            if (now - last_msg.created_at) < timedelta(minutes=2):
                                retry_time = now + timedelta(minutes=15)
                                await self.run_db(ai_config_collection.update_one, {"_id": guild_id}, {"$set": {"next_chat_time": retry_time}})
                                continue
                        except: pass

                    recent_users = await self.run_db(ai_personal_memories_collection.distinct, "user_id", {"guild_id": int(guild_id)})
                    target_user = None
                    if recent_users:
                        for uid in recent_users:
                            mem = guild.get_member(uid)
                            if mem and not mem.bot:
                                target_user = mem
                                break
                    if not target_user:
                         online_members = [m for m in guild.members if not m.bot and m.status != discord.Status.offline]
                         if online_members: target_user = random.choice(online_members)

                    if target_user:
                        await _initiate_conversation(self, channel, target_user)

                    new_next_time = self._calculate_next_chat_time(freq)
                    if new_next_time:
                        await self.run_db(ai_config_collection.update_one, {"_id": guild_id}, {"$set": {"next_chat_time": new_next_time}})
                except: continue
        except: pass

    @proactive_chat_loop.before_loop
    async def before_proactive_chat_loop(self):
        await self.bot.wait_until_ready()

    # --- AI COMMAND GROUP ---
    ai_group = app_commands.Group(name="ai", description="🧠 AI Interaction Tools")

    @ai_group.command(name="forget", description="Clear AI memory.")
    @app_commands.choices(scope=[
        app_commands.Choice(name="My Memories", value="personal"),
        app_commands.Choice(name="Server Memories (Admin)", value="guild")
    ])
    async def ai_forget(self, interaction: discord.Interaction, scope: str):
        if scope == 'guild' and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        if scope == 'guild': await self.run_db(ai_personal_memories_collection.delete_many, {"guild_id": interaction.guild_id})
        else: await self.run_db(ai_personal_memories_collection.delete_many, {"user_id": interaction.user.id, "guild_id": interaction.guild_id})
        await interaction.followup.send(f"✅ **Memory Wiped:** {scope.capitalize()}")

    @ai_group.command(name="lore", description="View the AI's understanding of this server.")
    async def ai_lore(self, interaction: discord.Interaction):
        data = await self.run_db(server_lore_collection.find_one, {"_id": str(interaction.guild_id)})
        if not data: return await interaction.response.send_message("🧠 No lore data yet.", ephemeral=True)
        embed = discord.Embed(title=f"🧠 Context: {interaction.guild.name}", color=discord.Color.purple())
        embed.add_field(name="Manual", value=data.get("manual_description", "None"), inline=False)
        embed.add_field(name="Learned", value=data.get("learned_summary", "None"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ai_group.command(name="teach", description="[Admin] Manually explain the server context.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_teach(self, interaction: discord.Interaction, description: str):
        await interaction.response.defer(ephemeral=True)
        await update_server_lore_summary(self.summarizer_model, interaction.guild, manual_description=description)
        await interaction.followup.send(f"✅ **Lore Updated:** \"{description}\"")

    @ai_group.command(name="refresh", description="[Admin] Force AI to re-read recent chats.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await update_guild_personality(self.summarizer_model, interaction.guild)
        await interaction.followup.send("✅ Personality Refreshed.")

    @ai_group.command(name="chat", description="[Admin] Trigger a proactive message to a user.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ai_chat(self, interaction: discord.Interaction, user: discord.Member):
        if user.bot: return await interaction.response.send_message("❌ Bots only talk to humans.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await _initiate_conversation(self, interaction.channel, user)
        await interaction.followup.send(f"✅ Triggered chat with {user.mention}.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild: return
        
        if isinstance(message.channel, discord.Thread):
            try:
                if rpg_sessions_collection.find_one({"thread_id": message.channel.id}): return 
            except: pass

        is_targeted = self.bot.user in message.mentions or (message.reference and message.reference.resolved and message.reference.resolved.author == self.bot.user)
        
        # --- LIMITER CHECK ---
        if is_targeted and not limiter.check_available(message.author.id, message.guild.id, "antima_gen"):
            embed = discord.Embed(
                title="⏳ Energy Depleted",
                description="You've used up your free AI interactions for now!\nTo support AnTiMa's development and server costs, please consider donating:",
                color=discord.Color.red()
            )
            embed.add_field(name="☕ International", value="[Support on Ko-fi](https://ko-fi.com/shirozura)", inline=True)
            embed.add_field(name="🍱 Indonesia", value="[Support on Trakteer](https://trakteer.id/Azuranyan)", inline=True)
            embed.set_footer(text="Your support helps keep the AI alive!")
            await message.reply(embed=embed)
            return

        guild_id = str(message.guild.id)
        guild_config = await self.run_db(ai_config_collection.find_one, {"_id": guild_id}) or {}

        if guild_config.get("bot_disabled", False):
            if self.bot.user in message.mentions: await message.reply("💤 Disabled.")
            return

        if not await should_bot_respond_ai_check(self, self.bot, self.summarizer_model, message):
            self.ignored_messages.append(message.id)
            return

        clean = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
        if not clean and not message.attachments: return

        if guild_config.get("group_chat_enabled", False) and message.channel.id == guild_config.get("channel") and not is_targeted:
            self.message_batches.setdefault(message.channel.id, []).append(message)
            if message.channel.id in self.batch_timers: self.batch_timers[message.channel.id].cancel()
            self.batch_timers[message.channel.id] = self.bot.loop.call_later(self.BATCH_DELAY, lambda: self.bot.loop.create_task(process_message_batch(self, message.channel.id)))
        else:
            await handle_single_user_response(self, message, clean, message.author)
            if is_targeted:
                limiter.consume(message.author.id, message.guild.id, "antima_gen")

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))