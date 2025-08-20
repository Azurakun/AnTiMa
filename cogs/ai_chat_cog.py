import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import google.generativeai as genai
import aiohttp

# Import the MongoDB collections, including the new one for memories
from utils.db import ai_config_collection, ai_memories_collection

logger = logging.getLogger(__name__)
MAX_HISTORY = 20 # Keep this to limit the immediate context for summarization

class AIChatCog(commands.Cog, name="AIChat"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.conversations = {}
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
            # A separate model for the summarization task
            self.summarizer_model = genai.GenerativeModel('gemini-1.5-flash')
            logger.info("Gemini AI models loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    def cog_unload(self):
        self.bot.loop.create_task(self.http_session.close())

    # --- NEW: Memory Management ---
    async def _load_memory(self, channel_id: int) -> str | None:
        """Loads the most recent conversation summary from the database."""
        memory = ai_memories_collection.find_one({"_id": str(channel_id)})
        return memory.get("summary") if memory else None

    async def _summarize_and_save_memory(self, channel_id: int, history: list):
        """Generates a summary of the conversation and saves it to the database."""
        if len(history) < 2: # Don't summarize very short conversations
            return

        # Format history for the summarizer prompt
        transcript = "\n".join([f"{item['role']}: {item['parts'][0]}" for item in history])
        
        prompt = (
            "You are a summarization AI. Your task is to create a concise, neutral, third-person summary of the following conversation transcript. "
            "Focus on the main topics, key facts, user questions, and any stated preferences or decisions. Keep it under 150 words.\n\n"
            f"TRANSCRIPT:\n---\n{transcript}\n---\n\nSUMMARY:"
        )
        
        try:
            response = await self.summarizer_model.generate_content_async(prompt)
            summary = response.text.strip()
            
            # Save the new summary to the database, overwriting the old one
            ai_memories_collection.update_one(
                {"_id": str(channel_id)},
                {"$set": {"summary": summary, "last_updated": discord.utils.utcnow()}},
                upsert=True
            )
            logger.info(f"Saved memory for channel {channel_id}.")
        except Exception as e:
            logger.error(f"Failed to summarize and save memory for channel {channel_id}: {e}")

    # --- Commands (No changes needed here) ---
    @app_commands.command(name="setchatchannel", description="Sets a text channel for open conversation with the AI.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatchannel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        guild_id = str(interaction.guild.id)
        if channel:
            ai_config_collection.update_one({"_id": guild_id}, {"$set": {"channel": channel.id}}, upsert=True)
            await interaction.response.send_message(f"✅ AI chat channel has been set to {channel.mention}.", ephemeral=True)
        else:
            ai_config_collection.update_one({"_id": guild_id}, {"$unset": {"channel": ""}})
            await interaction.response.send_message("ℹ️ AI chat channel has been cleared.", ephemeral=True)

    @app_commands.command(name="setchatforum", description="Sets a forum for open conversation with the AI.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchatforum(self, interaction: discord.Interaction, forum: discord.ForumChannel = None):
        guild_id = str(interaction.guild.id)
        if forum:
            ai_config_collection.update_one({"_id": guild_id}, {"$set": {"forum": forum.id}}, upsert=True)
            await interaction.response.send_message(f"✅ AI chat forum has been set to {forum.mention}.", ephemeral=True)
        else:
            ai_config_collection.update_one({"_id": guild_id}, {"$unset": {"forum": ""}})
            await interaction.response.send_message("ℹ️ AI chat forum has been cleared.", ephemeral=True)

    # --- Message Listener (Modified for Memory) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None:
            return

        guild_id = str(message.guild.id)
        channel_id = message.channel.id
        
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
                
                # --- NEW: Load and Inject Memory ---
                memory_summary = await self._load_memory(channel_id)
                memory_context = ""
                if memory_summary:
                    memory_context = (
                        f"to give you some long-term context, here's a summary of past conversations in this channel. "
                        f"do not mention this summary unless the user asks about past events. just use it as background knowledge.\n"
                        f"<memory>\n{memory_summary}\n</memory>\n\n"
                    )
                
                # Combine all parts for the final prompt
                final_prompt = f"{memory_context}now, here is the current message from the user:\n{current_prompt_with_author}"

                response = await chat.send_message_async(final_prompt)
                
                final_text = response.text[:2000]
                if final_text:
                    await message.reply(final_text)

                # --- NEW: Update memory in the background ---
                self.bot.loop.create_task(self._summarize_and_save_memory(channel_id, chat.history))

        except Exception as e:
            logger.error(f"Error during Gemini API call: {e}")
            await message.reply("😥 i'm sorry, my brain isn't braining right now. try again later or whatever.")

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))