import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import json
import google.generativeai as genai
from google.generativeai.types import GenerationConfig

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
            self.model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("Gemini AI model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    # --- Data Persistence ---
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
        
        # --- Admin Commands to clear stuck conversations ---
    @app_commands.command(name="clearchat", description="Clears the AI's conversation history for this channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clearchat(self, interaction: discord.Interaction):
        """Deletes the conversation history for the current channel."""
        channel_id = interaction.channel.id
        
        if channel_id in self.conversations:
            del self.conversations[channel_id]
            message = f"✅ AI conversation history has been cleared for this channel."
            logger.info(f"Admin cleared conversation history for channel {channel_id}")
        else:
            message = "ℹ️ There was no AI conversation history to clear for this channel."
            
        await interaction.response.send_message(message, ephemeral=True)



    # --- Event Listener for Messages ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None:
            return

        guild_id = str(message.guild.id)
        channel_id = message.channel.id
        
        guild_config = self.ai_config.get(guild_id, {})
        chat_channel_id = guild_config.get("channel")
        chat_forum_id = guild_config.get("forum")

        is_in_chat_channel = channel_id == chat_channel_id
        is_in_chat_forum = (isinstance(message.channel, discord.Thread) and message.channel.parent_id == chat_forum_id)
        is_mentioned = self.bot.user in message.mentions

        if not is_in_chat_channel and not is_in_chat_forum and not is_mentioned:
            return
            
        # Each channel and each thread gets its own conversation history
        if channel_id not in self.conversations:
            self.conversations[channel_id] = self.model.start_chat(history=[])
        
        chat = self.conversations[channel_id]

        # *** FIX 1: Prune the conversation history to prevent context overflow ***
        # Keep the last 20 messages (10 user, 10 model). Adjust as needed.
        if len(chat.history) > 20:
            # Keep the last 20 items from the history
            chat.history = chat.history[-20:]
            logger.info(f"Pruned conversation history for channel {channel_id}")
        
        try:
            async with message.channel.typing():
                prompt = message.content.replace(f'<@{self.bot.user.id}>', '').strip()

                # *** FIX 2: Handle empty prompts gracefully ***
                if not prompt:
                    await message.reply("👋 Hello! Did you mean to ask me something? Just @mention me with your question.", mention_author=False)
                    return
                
                generation_config = GenerationConfig(max_output_tokens=480)

                response = await chat.send_message_async(
                    prompt,
                    generation_config=generation_config
                )
                
                # The response is now guaranteed to be short enough for one message.
                await message.reply(response.text)

        except Exception as e:
            # It's good practice to log the actual content that caused the error for debugging
            logger.error(f"Error during Gemini API call: {e}")
            logger.error(f"Failed prompt for channel {channel_id}: '{prompt}'")
            
            # This is a better way to handle the specific error from the log
            # The 'parts' attribute is empty when no content is generated
            if "response.text" in str(e):
                 # You can access the feedback to see the exact reason (e.g., safety)
                try:
                    logger.error(f"Gemini response feedback: {response.prompt_feedback}")
                except Exception:
                     # This will fail if 'response' doesn't even exist, which is fine
                     pass
                await message.reply("😥 I'm sorry, I couldn't generate a response. This might be due to a safety filter or a configuration issue. Please try rephrasing your question.")
            else:
                 await message.reply("😥 I'm sorry, I'm having trouble thinking right now. Please try again later.")
            
            # It's often better not to delete the conversation history here,
            # as the issue might be a one-off problem with the prompt itself.
            # If errors persist, then clearing history might be a manual option.
            # if channel_id in self.conversations:
            #     del self.conversations[channel_id]

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))