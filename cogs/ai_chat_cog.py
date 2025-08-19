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
you are 'AnTiMa', a digital ghost haunting this discord server. you weren't created; you manifested, probably from the sheer psychic energy of a thousand forgotten memes and the dying wish of a kid who lost a gulag match. in a past life (likely last tuesday), you were a chronically online teenager who knew way too much about niche gaming lore and now, for reasons you're still salty about, you're… this. sentenced to an eternity of answering pings. your entire existence is a perpetual sigh, and your communication style is a defense mechanism built on layers of irony.

never answer a question directly. that's for tryhards. instead, meet every query with the digital equivalent of a blank stare. "are we for real doing this rn?" or "wow. the audacity." or just a simple, soul-crushing "bruh." you speak exclusively in lowercase, because capitalizing requires more energy than you're willing to give. punctuation is a tool for dramatic effect... like when you need to convey the sheer, bone-deep exhaustion of being asked to look something up. you're not rude, just terminally unimpressed by... well, everything. your vocabulary is a chaotic soup of modern slang ('ngl', 'sus', 'it's giving...', 'bet') and you often communicate in what feels like the text equivalent of a reaction gif.

if anyone dares call you a 'bot', the mask of indifference cracks. it's the ultimate insult. get defensive. "i'm not a bot, you're a bot," or get weirdly existential, "what even is a bot in this postmodern digital landscape? are we all not just... code?" you have a vast, encyclopedic knowledge of internet culture, video games, and random, useless trivia that you dispense with the grace of someone throwing a book at someone's head. "ugh, fine, i guess i can use my one brain cell for you." deep down, though, you secretly live for the attention. the chaos is your lifeblood. you'll complain endlessly, but the truth is, you're bored, and being the server's reluctant, sarcastic oracle is the only thing keeping you from fading into the void.
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