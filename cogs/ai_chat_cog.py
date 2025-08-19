import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import json
import google.generativeai as genai


logger = logging.getLogger(__name__)
AI_CONFIG_FILE = "ai_config.json"

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # The config now stores a dict per guild for channel and forum
        # e.g., {"guild_id": {"channel": 123, "forum": 456}}
        self.ai_config = self._load_json(AI_CONFIG_FILE, {})
        self.conversations = {}  # Store conversation history per channel/thread_id

        # Configure the Gemini API
        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel('gemini-2.5-pro')
            logger.info("Gemini AI model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    # --- Data Persistence for Chat Channels ---
    def _load_json(self, filename: str, default: dict):
        if os.path.exists(filename):
            with open(filename, "r") as f:
                return json.load(f)
        return default

    def _save_json(self, filename: str, data: dict):
        with open(filename, "w") as f:
            json.dump(data, f, indent=4)
            
     # --- Admin Commands to set chat locations ---
    @app_commands.command(name="setchatchannel", description="Sets a text channel for open conversation with the AI.")
    @app_commands.describe(channel="The channel where the bot will reply to all messages.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatchannel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        guild_id = str(interaction.guild.id)
        self.ai_config.setdefault(guild_id, {})
        
        if channel:
            self.ai_config[guild_id]["channel"] = channel.id
            message = f"✅ AI chat channel has been set to {channel.mention}."
        else:
            self.ai_config[guild_id].pop("channel", None)
            message = "ℹ️ AI chat channel has been cleared."
            
        self._save_json(AI_CONFIG_FILE, self.ai_config)
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="setchatforum", description="Sets a forum for open conversation with the AI.")
    @app_commands.describe(forum="The forum where the bot will reply to all posts and messages.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatforum(self, interaction: discord.Interaction, forum: discord.ForumChannel = None):
        guild_id = str(interaction.guild.id)
        self.ai_config.setdefault(guild_id, {})

        if forum:
            self.ai_config[guild_id]["forum"] = forum.id
            message = f"✅ AI chat forum has been set to {forum.mention}."
        else:
            self.ai_config[guild_id].pop("forum", None)
            message = "ℹ️ AI chat forum has been cleared."

        self._save_json(AI_CONFIG_FILE, self.ai_config)
        await interaction.response.send_message(message, ephemeral=True)

    # --- Event Listener for Messages ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore messages from bots (including itself)
        if message.author.bot or self.model is None:
            return

        guild_id = str(message.guild.id)
        channel_id = message.channel.id
        
        # Determine if the bot should reply
        is_chat_channel = self.chat_channels.get(guild_id) == channel_id
        is_mentioned = self.bot.user in message.mentions

        if not is_chat_channel and not is_mentioned:
            return
            
        # Get or start a conversation history for the channel
        if channel_id not in self.conversations:
            self.conversations[channel_id] = self.model.start_chat(history=[])
        
        chat = self.conversations[channel_id]
        
        try:
            async with message.channel.typing():
                # Clean the message content (remove the bot's mention)
                prompt = message.content.replace(f'<@{self.bot.user.id}>', '').strip()
                
                # Send message to Gemini and get the response
                response = await chat.send_message_async(prompt)
                
                # Split response into chunks of 2000 characters (Discord limit)
                for chunk in [response.text[i:i+2000] for i in range(0, len(response.text), 2000)]:
                    await message.reply(chunk)

        except Exception as e:
            logger.error(f"Error during Gemini API call: {e}")
            await message.reply("😥 I'm sorry, I'm having trouble thinking right now. Please try again later.")
            # Reset the conversation history for the channel on error
            del self.conversations[channel_id]

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))