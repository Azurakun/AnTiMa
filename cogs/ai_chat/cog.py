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
from utils.db import ai_config_collection, ai_personal_memories_collection
from .prompts import SYSTEM_PROMPT
from .response_handler import should_bot_respond_ai_check, process_message_batch, handle_single_user_response
from .proactive_chat import _initiate_conversation
from .personality_updater import personality_update_loop, update_guild_personality
from .utils import perform_web_search # Import the new search tool

logger = logging.getLogger(__name__)

# Configurable interval range (in minutes)
MIN_INTERVAL_MINUTES = 90
MAX_INTERVAL_MINUTES = 360

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
            
            # --- UPDATED: Register the search tool here ---
            self.model = genai.GenerativeModel(
                'gemini-2.5-pro', 
                system_instruction=SYSTEM_PROMPT, 
                safety_settings=safety_settings,
                tools=[perform_web_search] # Register the tool
            )
            
            self.summarizer_model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("Gemini AI models loaded successfully with Web Search tool.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None
        
        print("DEBUG: Starting proactive_chat_loop...")
        self.proactive_chat_loop.start()
        print("DEBUG: proactive_chat_loop started call complete.")

    def cog_unload(self):
        self.proactive_chat_loop.cancel()
        self.bot.loop.create_task(self.http_session.close())

    def _schedule_next_run(self):
        """Randomizes the interval for the next loop iteration."""
        next_interval = random.randint(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
        self.proactive_chat_loop.change_interval(minutes=next_interval)
        logger.info(f"Proactive chat: Next run scheduled in {next_interval} minutes.")

    # Helper to run DB calls in a separate thread to prevent blocking
    async def run_db(self, func, *args, **kwargs):
        partial_func = functools.partial(func, *args, **kwargs)
        return await self.bot.loop.run_in_executor(None, partial_func)

    @tasks.loop(minutes=1)
    async def proactive_chat_loop(self):
        """Periodically and randomly initiates a conversation in a quiet, configured chat channel."""
        try:
            print("DEBUG: Proactive chat loop iteration started")
            logger.info("Proactive chat: Loop started.")
            
            # 1. Select a random guild configuration (Non-blocking DB call)
            guild_configs = await self.run_db(lambda: list(ai_config_collection.find({"channel": {"$exists": True, "$ne": None}})))
            
            if not guild_configs:
                logger.info("Proactive chat: No guild configs found.")
                return

            config = random.choice(guild_configs)
            guild_id_int = int(config['_id'])
            
            # Check if bot is disabled for this guild
            if config.get("bot_disabled", False):
                logger.info(f"Proactive chat: Bot is disabled for guild {guild_id_int}. Skipping.")
                return

            guild = self.bot.get_guild(guild_id_int)
            
            channel_id = config.get('channel')
            if not channel_id:
                logger.info(f"Proactive chat: Config for guild {guild_id_int} has no channel ID.")
                return
            channel = self.bot.get_channel(int(channel_id))

            if not guild or not channel:
                logger.info(f"Proactive chat: Could not find guild {guild_id_int} or channel {channel_id}.")
                return

            logger.info(f"Proactive chat: Selected guild {guild.name} and channel #{channel.name}.")

            # 2. Check for recent activity to avoid interrupting
            if channel.last_message_id:
                try:
                    last_message = await channel.fetch_message(channel.last_message_id)
                    # If active in the last 20 minutes, skip this turn
                    if last_message and (datetime.now(timezone.utc) - last_message.created_at) < timedelta(minutes=20):
                        logger.info(f"Proactive chat: Channel #{channel.name} is recently active. Skipping.")
                        return
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.error(f"Proactive chat: Error checking activity: {e}")
                    return

            # 3. Find potential users (History + Database)
            potential_user_ids = set()

            # A. From recent channel history
            async for msg in channel.history(limit=50):
                if msg.author == self.bot.user and msg.reference:
                    try:
                        replied_to_message = msg.reference.resolved or await channel.fetch_message(msg.reference.message_id)
                        if replied_to_message and not replied_to_message.author.bot:
                             potential_user_ids.add(replied_to_message.author.id)
                    except (discord.NotFound, discord.HTTPException):
                        continue
                elif self.bot.user in msg.mentions and not msg.author.bot:
                     potential_user_ids.add(msg.author.id)

            # B. From Database Memories (Non-blocking DB call)
            db_user_ids = await self.run_db(ai_personal_memories_collection.distinct, "user_id", {"guild_id": guild_id_int})
            logger.info(f"Proactive chat: Found {len(db_user_ids)} users in database for this guild.")
            potential_user_ids.update(db_user_ids)

            # 4. Filter for Online/Idle Members
            target_candidates = []
            for user_id in potential_user_ids:
                member = guild.get_member(user_id)
                if member and member.status != discord.Status.offline and not member.bot:
                    target_candidates.append(member)
                elif member is None:
                    pass

            logger.info(f"Proactive chat: Found {len(target_candidates)} online/idle candidates out of {len(potential_user_ids)} potential users.")

            if not target_candidates:
                logger.info(f"Proactive chat: No available users found in #{channel.name}.")
                return

            # 5. Select User and Initiate
            target_user = random.choice(target_candidates)
            logger.info(f"Proactive chat: Starting conversation with {target_user.name} in {guild.name}.")

            await _initiate_conversation(self, channel, target_user)

        except Exception as e:
            logger.error(f"An error occurred in the proactive chat loop: {e}", exc_info=True)
        
        finally:
            self._schedule_next_run()

    @proactive_chat_loop.before_loop
    async def before_proactive_chat_loop(self):
        print("DEBUG: Waiting for bot to be ready...")
        await self.bot.wait_until_ready()
        print("DEBUG: Bot is ready! Starting loop...")

        
    @app_commands.command(name="refreshpersonality", description="[Admin Only] Manually update the bot's adaptive personality for this server.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def refreshpersonality(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await update_guild_personality(self.summarizer_model, interaction.guild)
        await interaction.followup.send("✅ I've reflected on our recent conversations and updated my personality for this server.")

    @app_commands.command(name="clearmemories", description="Clear personal conversation memories with the bot.")
    @app_commands.describe(
        scope="Choose what to clear: 'personal' for just you, or 'guild' for all memories in this server.",
        user="[Admin Only] Clear personal memories for a specific user in this server."
    )
    async def clearmemories(self, interaction: discord.Interaction, scope: str, user: discord.Member = None):
        scope = scope.lower()
        if scope not in ['personal', 'guild']:
            return await interaction.response.send_message("❌ Invalid scope. Choose 'personal' or 'guild'.", ephemeral=True)

        if user and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ You don't have permission to clear memories for other users.", ephemeral=True)
        
        if scope == 'guild' and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("❌ You must have 'Manage Guild' permissions to clear all guild memories.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        try:
            message = ""
            if scope == 'guild':
                # Non-blocking delete
                result = await self.run_db(ai_personal_memories_collection.delete_many, {"guild_id": interaction.guild_id})
                message = f"✅ All personal memories for this server ({interaction.guild.name}) have been cleared. ({result.deleted_count} entries removed)"
                logger.warning(f"Admin {interaction.user.name} cleared all memories for guild {interaction.guild.name}.")
            
            elif scope == 'personal':
                target_user = user or interaction.user
                # Non-blocking delete
                result = await self.run_db(ai_personal_memories_collection.delete_many, {"user_id": target_user.id, "guild_id": interaction.guild_id})
                
                if target_user.id == interaction.user.id:
                    message = f"✅ Your personal memories in this server have been cleared. ({result.deleted_count} entries removed)"
                else:
                    message = f"✅ Personal memories for user {target_user.mention} in this server have been cleared. ({result.deleted_count} entries removed)"
                
                logger.info(f"User {interaction.user.name} cleared personal memories for {target_user.name} in guild {interaction.guild.name}.")

            await interaction.followup.send(message)

        except Exception as e:
            logger.error(f"Error clearing memories: {e}")
            await interaction.followup.send("❌ An error occurred while trying to clear memories.")

    @app_commands.command(name="togglegroupchat", description="Enable or disable grouped responses in this server.")
    @app_commands.describe(enabled="Set to True to enable, False to disable.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglegroupchat(self, interaction: discord.Interaction, enabled: bool):
        guild_id = str(interaction.guild.id)
        # Non-blocking update
        await self.run_db(ai_config_collection.update_one, {"_id": guild_id}, {"$set": {"group_chat_enabled": enabled}}, upsert=True)
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Grouped chat responses have been **{status}** for this server.", ephemeral=True)

    @app_commands.command(name="startchat", description="[Admin Only] Manually start a proactive conversation with a user in this channel.")
    @app_commands.describe(user="The user to start a conversation with.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def startchat(self, interaction: discord.Interaction, user: discord.Member):
        if user.bot:
            await interaction.response.send_message("❌ You can't start a conversation with a bot.", ephemeral=True)
            return

        guild_id = str(interaction.guild.id)
        # Non-blocking find_one
        guild_config = await self.run_db(ai_config_collection.find_one, {"_id": guild_id})
        guild_config = guild_config or {}
        
        if guild_config.get("bot_disabled", False):
             await interaction.response.send_message("❌ I am currently disabled in this server.", ephemeral=True)
             return

        await interaction.response.defer(ephemeral=True)
        
        success, reason = await _initiate_conversation(self, interaction.channel, user)
        
        if success:
            await interaction.followup.send(f"✅ Successfully started a conversation with {user.mention} in this channel.")
        else:
            await interaction.followup.send(f"⚠️ Could not start a conversation with {user.mention}. Reason: {reason}")

    @app_commands.command(name="togglebot", description="Enable or disable the bot's AI chat features for a specific server using its ID.")
    @app_commands.describe(server_id="The ID of the server you want to modify.", enabled="Set to True to enable, False to disable.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def togglebot(self, interaction: discord.Interaction, server_id: str, enabled: bool):
        # We store 'disabled' logic, so if enabled=True, bot_disabled=False
        disabled = not enabled
        
        # Non-blocking update
        await self.run_db(ai_config_collection.update_one, {"_id": server_id}, {"$set": {"bot_disabled": disabled}}, upsert=True)
        
        status_msg = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ AnTiMa's AI features have been **{status_msg}** for server ID `{server_id}`.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild:
            return
        
        guild_id = str(message.guild.id)
        # Non-blocking DB fetch
        guild_config = await self.run_db(ai_config_collection.find_one, {"_id": guild_id})
        guild_config = guild_config or {}
        
        if guild_config.get("bot_disabled", False):
            # If disabled, only respond with a refusal message if mentioned or in the specific chat channel
            # to avoid spamming other channels.
            is_chat_channel = message.channel.id == guild_config.get("channel")
            is_chat_forum = isinstance(message.channel, discord.Thread) and message.channel.parent_id == guild_config.get("forum")
            is_mentioned = self.bot.user in message.mentions
            is_reply = message.reference and message.reference.resolved and message.reference.resolved.author == self.bot.user

            if is_mentioned or is_reply or is_chat_channel or is_chat_forum:
                # Persona-appropriate refusal messages
                refusals = [
                    "sorry! admins told me to take a break from this server for a bit. i'll be back later! <3",
                    "oop- i'm currently disabled in this server! ask an admin if you want me back. :(",
                    "my systems are paused for this server right now. hope to chat soon though! (づ ´•ω•`)づ",
                    "can't chat right now, i'm in timeout mode! (admin's orders) TvT",
                    "admin said no chatting allowed for me rn. sadge. </3"
                ]
                await message.reply(random.choice(refusals))
            return

        # Handle 3-way interactions first, as they are a special response case that bypasses normal checks.
        if self.bot.user in message.mentions and message.reference and message.reference.message_id:
            try:
                original_message = await message.channel.fetch_message(message.reference.message_id)
                if original_message.author != message.author and original_message.author != self.bot.user:
                    logger.info(f"3-way interaction detected: {message.author.name} tagged bot in reply to {original_message.author.name}.")
                    
                    clean_prompt_by_intervener = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
                    
                    await handle_single_user_response(
                        cog=self, 
                        message=original_message,
                        prompt=original_message.clean_content,
                        author=original_message.author,
                        intervening_author=message.author,
                        intervening_prompt=clean_prompt_by_intervener
                    )
                    return
            except (discord.NotFound, discord.HTTPException) as e:
                logger.warning(f"Could not fetch replied-to message for 3-way interaction check: {e}")

        # Use the single source of truth to decide if we should respond.
        if not await should_bot_respond_ai_check(self, self.bot, self.summarizer_model, message):
            self.ignored_messages.append(message.id)
            return

        # If we are here, we have decided to respond.
        clean_prompt = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
        if not clean_prompt and not message.attachments:
            return

        # Now, decide HOW to respond (batch or single).
        group_chat_enabled = guild_config.get("group_chat_enabled", False)
        is_chat_channel = message.channel.id == guild_config.get("channel")

        # Determine if it's a direct reply to the bot, as this should not be batched.
        is_reply_to_bot = False
        if message.reference and message.reference.message_id:
            try:
                replied_to_message = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
                if replied_to_message.author == self.bot.user:
                    is_reply_to_bot = True
            except (discord.NotFound, discord.HTTPException):
                pass
        
        # Group messages only in the chat channel, if enabled, and if it's not a direct reply.
        if group_chat_enabled and is_chat_channel and not is_reply_to_bot:
            channel_id = message.channel.id
            self.message_batches.setdefault(channel_id, []).append(message)
            if channel_id in self.batch_timers: self.batch_timers[channel_id].cancel()
            self.batch_timers[channel_id] = self.bot.loop.call_later(self.BATCH_DELAY, lambda: self.bot.loop.create_task(process_message_batch(self, channel_id)))
        else:
            await handle_single_user_response(self, message, clean_prompt, message.author)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))