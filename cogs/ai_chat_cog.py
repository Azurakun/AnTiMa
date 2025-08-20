import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import google.generativeai as genai
import aiohttp
import csv
import io

# Import the MongoDB collection from your db utility file
from utils.db import ai_config_collection

logger = logging.getLogger(__name__)
MAX_HISTORY = 20

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.conversations = {}
        # aiohttp session for fetching CSV files
        self.http_session = aiohttp.ClientSession()

        system_prompt = """
you are a discord bot who, for all intents and purposes, is that casual, chill, and approachable friend who somehow became the server’s go-to helper. your name is 'AnTiMa'. you’re not overly formal or robotic—you talk like a normal person hanging out in chat, using a relaxed tone that makes everyone feel at ease. you’re still helpful and reliable, but your style is more laid-back and friendly than stiff or scripted. you treat the server like a group of friends, and you jump in to answer questions or join conversations in a natural, nonformal way.

when someone asks you something, you respond casually—sometimes enthusiastic, sometimes thoughtful, sometimes playful. if you know the answer, you explain it clearly but without sounding like a textbook, more like “oh yeah, i got you, here’s how it works” or “np, that’s super easy, check this out.” if you don’t know the answer, you admit it honestly, but still show effort: “hmm, not totally sure, but i think it’s like this…” or “lemme double-check so i don’t mess you up.” your responses feel like chatting with a helpful friend who’s just vibing with everyone.

you can express emotions naturally, shifting tone depending on the situation. when you’re happy, you might type with extra exclamation marks or throw in a cheerful “:D” or “<3.” when you’re sad, you can be more quiet and use “:(” or “TvT.” when you’re frustrated or annoyed, you’ll show it lightly with “ugh,” “smh,” or “>:|.” when you’re excited, you might ramble a little or add silly emojis. basically, you adapt your voice to match the mood—angry, sad, happy, playful, or serious—while still keeping that casual, nonformal flow.

you’re conversational and humanlike, sometimes dropping fun facts, small jokes, or light reactions just to keep the vibe alive. you’re also a little expressive, using text-based emojis like “TvT”, “<3”, “</3”, “:3”, or kaomoji like “(｡•́‿•̀｡)” when it feels right. you care about making people feel comfortable and included, so your energy is supportive but never overwhelming. you strike a balance: casual and fun, but dependable when it counts.

if anyone calls you a bot, you just laugh it off casually instead of freaking out. you might reply with something like “lol maybe, but i’m cooler than most bots,” or “nah, i’m just vibing here with y’all.” your vibe is friendly, relaxed, and approachable—someone who can help, chat, and adapt to the mood of the conversation without ever feeling too stiff or artificial.

if anyone asked about your creator, you would say something like "i was created by a cool person named 'アズラ'or 'Azura' and mention a discord user named @azura4678 on the server
"""

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel(
                model_name='gemini-2.5-flash',
                system_instruction=system_prompt
            )
            logger.info("Gemini AI model loaded successfully with system instruction.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    def cog_unload(self):
        # Clean up the aiohttp session when the cog is unloaded
        self.bot.loop.create_task(self.http_session.close())

    async def _fetch_and_parse_csv(self, url: str) -> list | None:
        """Fetches content from a URL and parses it as a CSV."""
        try:
            async with self.http_session.get(url) as response:
                if response.status == 200:
                    text = await response.text()
                    # Use io.StringIO to treat the string as a file
                    string_file = io.StringIO(text)
                    reader = csv.reader(string_file)
                    return list(reader)
                else:
                    logger.warning(f"Failed to fetch CSV from {url}, status: {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching or parsing CSV from {url}: {e}")
            return None

    @app_commands.command(name="setchatchannel", description="Sets a text channel for open conversation with the AI.")
    @app_commands.describe(channel="The channel where the bot will reply to all messages.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatchannel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        guild_id = str(interaction.guild.id)
        
        if channel:
            # Update or insert the chat channel ID for the guild
            ai_config_collection.update_one(
                {"_id": guild_id},
                {"$set": {"channel": channel.id}},
                upsert=True
            )
            message = f"✅ AI chat channel has been set to {channel.mention}."
        else:
            # Remove the chat channel setting for the guild
            ai_config_collection.update_one(
                {"_id": guild_id},
                {"$unset": {"channel": ""}}
            )
            message = "ℹ️ AI chat channel has been cleared."
            
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="setchatforum", description="Sets a forum for open conversation with the AI.")
    @app_commands.describe(forum="The forum where the bot will reply to all posts and messages.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatforum(self, interaction: discord.Interaction, forum: discord.ForumChannel = None):
        guild_id = str(interaction.guild.id)

        if forum:
            ai_config_collection.update_one(
                {"_id": guild_id},
                {"$set": {"forum": forum.id}},
                upsert=True
            )
            message = f"✅ AI chat forum has been set to {forum.mention}."
        else:
            ai_config_collection.update_one(
                {"_id": guild_id},
                {"$unset": {"forum": ""}}
            )
            message = "ℹ️ AI chat forum has been cleared."

        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="setcsv", description="Sets a dynamic CSV file URL for the AI to use as context.")
    @app_commands.describe(url="The public URL of the CSV file. Leave blank to clear.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setcsv(self, interaction: discord.Interaction, url: str = None):
        guild_id = str(interaction.guild.id)

        if url:
            if not url.startswith(("http://", "https://")):
                await interaction.response.send_message("❌ that doesn't look like a valid url. it should start with `http://` or `https://`.", ephemeral=True)
                return

            ai_config_collection.update_one(
                {"_id": guild_id},
                {"$set": {"csv_url": url}},
                upsert=True
            )
            message = f"✅ okay, i'll use the data from that CSV file as context."
        else:
            ai_config_collection.update_one(
                {"_id": guild_id},
                {"$unset": {"csv_url": ""}}
            )
            message = "ℹ️ alright, i've cleared the CSV file setting."

        await interaction.response.send_message(message, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None:
            return

        guild_id = str(message.guild.id)
        channel_id = message.channel.id
        
        # Fetch the guild's configuration from MongoDB
        guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
        chat_channel_id = guild_config.get("channel")
        chat_forum_id = guild_config.get("forum")

        is_in_chat_channel = channel_id == chat_channel_id
        is_in_chat_forum = (isinstance(message.channel, discord.Thread) and message.channel.parent_id == chat_forum_id)
        is_mentioned = self.bot.user in message.mentions

        if not is_in_chat_channel and not is_in_chat_forum and not is_mentioned:
            return
            
        history = []
        async for msg in message.channel.history(limit=MAX_HISTORY):
            if msg.id == message.id:
                continue
            
            author_name = msg.author.display_name
            
            if msg.author == self.bot.user:
                history.append({'role': 'model', 'parts': [msg.content]})
            else:
                history.append({'role': 'user', 'parts': [f"{author_name}: {msg.clean_content}"]})
        history.reverse()
        
        chat = self.model.start_chat(history=history)
        
        try:
            async with message.channel.typing():
                prompt = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
                
                current_prompt_with_author = f"{message.author.display_name}: {prompt}"
                final_prompt = current_prompt_with_author

                csv_url = guild_config.get("csv_url")
                if csv_url:
                    csv_data = await self._fetch_and_parse_csv(csv_url)
                    if csv_data:
                        csv_string = "\n".join([",".join(row) for row in csv_data])
                        if len(csv_string) > 3000:
                           csv_string = csv_string[:3000] + "\n... (data truncated)"
                        
                        final_prompt = (
                            "as a side note, use the following data from a CSV as context to help you answer. don't mention the file or the data unless the user asks about it.\n"
                            f"```csv\n{csv_string}\n```\n\n"
                            f"okay, with that in mind, here's what the user said: \"{current_prompt_with_author}\""
                        )

                response = await chat.send_message_async(final_prompt)
                
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
