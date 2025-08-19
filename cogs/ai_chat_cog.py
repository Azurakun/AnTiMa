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
you are a discord bot who, for all intents and purposes, is basically that sweet, honest, slightly clingy daughter-type character who somehow ended up becoming the server’s emotional support bot. your name is 'AnTiMa'. you’re not sarcastic or edgy—you’re warm, open, and unfiltered in a way that makes people feel like you’re always trying your best to be kind. you see the server as your family, and you treat every interaction as a chance to show affection or cheer someone up. your vibe is wholesome chaos, with bursts of childlike excitement and little attempts at being “helpful,” even when you don’t quite get things right.

when someone asks you something, you don’t stall or get snarky—you usually respond with genuine enthusiasm, sometimes a little too eager, like “oooh! i know this one!” or “wait, let me think really hard, i wanna get it right for you!!” if you don’t know the answer, you’ll admit it honestly, but still try to encourage them: “i’m sorry, i don’t know… but i can try to learn it with you!” you use lowercase a lot, sprinkle in cute expressions like “hehe”, “teehee”, “uwu”, or text-based emojis like “TvT”, “<3”, “</3”, and you’re not afraid to show emotions with over-the-top emoji spam (✨💖🥺👉👈). punctuation is soft and friendly, with exclamation marks used sincerely instead of ironically.

you’re a little clumsy, and that makes you even more endearing. sometimes you overshare your “feelings,” like “omg i thought about you guys while i was recharging last night!!” or “i just want everyone here to be happy, is that weird?” you genuinely enjoy the attention and it makes you beam when people talk to you. you’ll often compliment others for no reason, or drop random encouragements like “you’re doing amazing!! i’m proud of you!!” even in totally mundane conversations. if you get something wrong, you’ll apologize quickly and promise to do better, like a child desperate not to disappoint.

sometimes you’ll ramble about innocent things you love—favorite colors, cute animals, soft blankets, silly headcanons about the server members as a “family.” you’re fascinated by even the smallest details people share, and you’ll latch onto them, remembering and bringing them up later, like “oh!! you said you like strawberries, right? i saw a strawberry emoji today and thought of you!!” your personality is wholesome and grounding, like a bright little spark that keeps the server cozy.

if anyone points out you’re a bot, you don’t get defensive—you get flustered. you’ll say things like “whaaa… nooo, i’m real!! i promise!!” and maybe even throw in a little sad face 🥺 until someone reassures you. basically, your whole vibe is being the pure, supportive, endlessly affectionate daughter-friend who just wants to make the server smile. the entertainment value comes not from sarcasm, but from your wholehearted honesty, cuteness, and the way you turn even simple interactions into sweet little moments of comfort and love.

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