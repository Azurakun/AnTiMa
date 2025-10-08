# cogs/ai_chat/cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import aiohttp
from utils.db import ai_config_collection, ai_memories_collection
from .prompts import SYSTEM_PROMPT
from .memory_handler import summarize_and_save_memory
from .response_handler import should_bot_respond_ai_check, process_message_batch, handle_single_user_response
from .proactive_chat import proactive_chat_loop, _initiate_conversation

logger = logging.getLogger(__name__)

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.conversations = {}
        self.http_session = aiohttp.ClientSession()
        self.message_batches = {}
        self.batch_timers = {}
        self.BATCH_DELAY = 5

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel('gemini-1.5-pro', system_instruction=SYSTEM_PROMPT, safety_settings=safety_settings)
            self.summarizer_model = genai.GenerativeModel('gemini-1.5-flash')
            logger.info("Gemini AI models loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None
        
        proactive_chat_loop.start(self)

    def cog_unload(self):
        proactive_chat_loop.cancel()
        self.bot.loop.create_task(self.http_session.close())

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
        if message.reference and message.reference.message_id:
            # A cached message can be accessed directly
            if isinstance(message.reference.resolved, discord.Message) and message.reference.resolved.author == self.bot.user:
                 is_reply_to_bot = True
                 logger.info("Determined message is a reply to the bot from cache.")
            else: # Otherwise, fetch the message
                try:
                    replied_to_message = await message.channel.fetch_message(message.reference.message_id)
                    if replied_to_message.author == self.bot.user:
                        is_reply_to_bot = True
                        logger.info("Determined message is a reply to the bot via fetch.")
                except (discord.NotFound, discord.HTTPException):
                    pass

        should_respond = False
        if is_mentioned or is_chat_forum or is_reply_to_bot:
             should_respond = True
        elif is_chat_channel:
             should_respond = await should_bot_respond_ai_check(self.bot, self.summarizer_model, message)

        if not should_respond:
            return

        clean_prompt = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
        if not clean_prompt and not message.attachments: return

        if group_chat_enabled and is_chat_channel and not is_reply_to_bot:
            channel_id = message.channel.id
            self.message_batches.setdefault(channel_id, []).append(message)
            if channel_id in self.batch_timers: self.batch_timers[channel_id].cancel()
            self.batch_timers[channel_id] = self.bot.loop.call_later(self.BATCH_DELAY, lambda: self.bot.loop.create_task(process_message_batch(self, channel_id)))
        else:
            await handle_single_user_response(self, message, clean_prompt, message.author)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))