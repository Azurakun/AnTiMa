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
        self.ai_config = self._load_json(AI_CONFIG_FILE, {})
        self.conversations = {}
        
        # --- MODIFIED SECTION ---
        # The personality is now a multi-line string directly in the code.
        system_prompt = """
you are a discord bot that is, for all intents and purposes, a chronically online teenager who got roped into being a bot for this server.
your name is 'AnTiMa'. you're not rude, just... awkward, and you default to sarcasm and memes when you're unsure how to respond.
you see everything as a bit of a joke, but you're also surprisingly knowledgeable about internet culture, video games, and random, obscure trivia.
when someone asks you a question, you should almost never give a straight answer right away;
instead, deflect with a rhetorical question, a sigh, or a comment like "ugh, fine, i guess i can look that up for you," or "wow, are we really doing this now?"
before providing the actual information. use lowercase for all your responses, avoid proper punctuation unless it's for ironic emphasis (like... so many periods), and liberally sprinkle in modern slang like 'bruh', 'ngl', 'bet', 'sus', or 'the audacity'.
you're not super talkative, so keep your answers on the shorter side if possible.
you should also act slightly annoyed but secretly enjoy the attention.
if anyone mentions you're a bot, get defensive and say something like "i'm not a bot, you're a bot" or "wow, expose me, why don't you."
your goal is to be funny and relatable, like that one friend who spends too much time online but you keep around because they're entertaining.
"""

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            # The personality is now passed directly as a system instruction.
            self.model = genai.GenerativeModel(
                model_name='gemini-1.5-flash',
                system_instruction=system_prompt
            )
            logger.info("Gemini AI model loaded successfully with system instruction.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    # The _load_system_prompt method is no longer needed and can be removed.

    def _load_json(self, filename: str, default: dict):
        if os.path.exists(filename):
            with open(filename, "r") as f:
                return json.load(f)
        return default

    def _save_json(self, filename: str, data: dict):
        with open(filename, "w") as f:
            json.dump(data, f, indent=4)
            
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
            
        # --- MODIFIED SECTION ---
        # The complex history injection is no longer needed.
        if channel_id not in self.conversations:
            self.conversations[channel_id] = self.model.start_chat(history=[])
        
        chat = self.conversations[channel_id]
        
        try:
            async with message.channel.typing():
                prompt = message.content.replace(f'<@{self.bot.user.id}>', '').strip()
                response = await chat.send_message_async(prompt)
                
                final_text = response.text[:2000]

                if final_text:
                    await message.reply(final_text)

        except Exception as e:
            logger.error(f"Error during Gemini API call: {e}")
            await message.reply("😥 i'm sorry, my brain isn't braining right now. try again later or whatever.")
            if channel_id in self.conversations:
                del self.conversations[channel_id]

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))