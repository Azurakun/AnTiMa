# cogs/ai_chat/cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import aiohttp
import collections # <-- ADDED
from utils.db import ai_config_collection, ai_personal_memories_collection
from .prompts import SYSTEM_PROMPT
from .response_handler import should_bot_respond_ai_check, process_message_batch, handle_single_user_response
from .proactive_chat import proactive_chat_loop, _initiate_conversation
from .personality_updater import personality_update_loop, update_guild_personality

logger = logging.getLogger(__name__)

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.conversations = {}
        self.http_session = aiohttp.ClientSession()
        self.message_batches = {}
        self.batch_timers = {}
        self.BATCH_DELAY = 5
        self.ignored_messages = collections.deque(maxlen=500) # <-- ADDED: To track ignored conversations

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel('gemini-2.5-pro', system_instruction=SYSTEM_PROMPT, safety_settings=safety_settings)
            self.summarizer_model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("Gemini AI models loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None
        
        proactive_chat_loop.start(self)

    def cog_unload(self):
        proactive_chat_loop.cancel()
        self.bot.loop.create_task(self.http_session.close())
        
        
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
                result = ai_personal_memories_collection.delete_many({"guild_id": interaction.guild_id})
                message = f"✅ All personal memories for this server ({interaction.guild.name}) have been cleared. ({result.deleted_count} entries removed)"
                logger.warning(f"Admin {interaction.user.name} cleared all memories for guild {interaction.guild.name}.")
            
            elif scope == 'personal':
                target_user = user or interaction.user
                result = ai_personal_memories_collection.delete_many({"user_id": target_user.id, "guild_id": interaction.guild_id})
                
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
        ai_config_collection.update_one({"_id": guild_id}, {"$set": {"group_chat_enabled": enabled}}, upsert=True)
        status = "enabled" if enabled else "disabled"
        await interaction.response.send_message(f"✅ Grouped chat responses have been **{status}** for this server.", ephemeral=True)

    @app_commands.command(name="startchat", description="[Admin Only] Manually start a proactive conversation with a user in this channel.")
    @app_commands.describe(user="The user to start a conversation with.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def startchat(self, interaction: discord.Interaction, user: discord.Member):
        if user.bot:
            await interaction.response.send_message("❌ You can't start a conversation with a bot.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        
        success, reason = await _initiate_conversation(self, interaction.channel, user)
        
        if success:
            await interaction.followup.send(f"✅ Successfully started a conversation with {user.mention} in this channel.")
        else:
            await interaction.followup.send(f"⚠️ Could not start a conversation with {user.mention}. Reason: {reason}")

    # vvvvvv REWRITTEN vvvvvv
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild:
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
        guild_id = str(message.guild.id)
        guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
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
    # ^^^^^^ REWRITTEN ^^^^^^


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))