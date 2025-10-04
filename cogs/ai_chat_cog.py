# cogs/ai_chat_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import aiohttp
import re
from datetime import datetime

# Import the MongoDB collections
from utils.db import ai_config_collection, ai_memories_collection

logger = logging.getLogger(__name__)
MAX_HISTORY = 15
MAX_USER_MEMORIES = 20

def _find_member(guild: discord.Guild, name: str):
    """Finds a member in a guild by name or display name, case-insensitively."""
    name = name.lower()
    return discord.utils.find(
        lambda m: m.name.lower() == name or m.display_name.lower() == name,
        guild.members
    )

def _safe_get_response_text(response) -> str:
    """Safely gets text from a Gemini response, handling blocked content."""
    try:
        return response.text
    except (ValueError, IndexError):
        # This occurs when the response is blocked by safety filters
        logger.warning("Gemini response was empty or blocked.")
        return ""

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

if anyone asked about your creator, you would say something like "i was created by a cool person named 'Azura' and mention a discord user 898989641112383488 on the server

**New Tool Instructions:**
- If you need to get information about a server member (like their ID, roles, or join date), respond ONLY with the text: [FETCH_USER_DATA: 'username']. I will provide you with the data.
- After you have the user's ID, if you need to mention them in your response, use the format [MENTION: 'user_id']. I will convert this into a real Discord tag.
"""
        
        # Define less restrictive safety settings
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            self.model = genai.GenerativeModel(
                model_name='gemini-2.5-pro',
                system_instruction=system_prompt,
                safety_settings=safety_settings
            )
            self.summarizer_model = genai.GenerativeModel('gemini-2.5-pro')
            logger.info("Gemini AI models loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to configure Gemini AI: {e}")
            self.model = None

    def cog_unload(self):
        self.bot.loop.create_task(self.http_session.close())

    async def _load_user_memories(self, user_id: int) -> str:
        """Loads and formats all memories for a given user."""
        memories_cursor = ai_memories_collection.find({"user_id": user_id}).sort("timestamp", 1)
        memories = list(memories_cursor)
        
        if not memories:
            return ""

        formatted_memories = []
        for i, memory in enumerate(memories):
            formatted_memories.append(f"Memory {i+1}: {memory['summary']}")
            
        return "\n".join(formatted_memories)

    async def _summarize_and_save_memory(self, user_id: int, history: list):
        """Generates a summary of the conversation and saves it as a new memory."""
        if len(history) < 2:
            return

        transcript_parts = []
        for item in history:
            role = item.role
            text = ""
            if item.parts:
                try:
                    text = item.parts[0].text
                except Exception:
                    text = str(item.parts[0])
            transcript_parts.append(f"{role}: {text}")

        transcript = "\n".join(transcript_parts)
        
        prompt = (
            "You are a summarization AI. Your task is to create a concise, neutral, third-person summary of the following conversation transcript. "
            "Focus on the main topics, key facts, user questions, and any stated preferences or decisions. Keep it under 150 words.\n\n"
            f"TRANSCRIPT:\n---\n{transcript}\n---\n\nSUMMARY:"
        )
        
        try:
            response = await self.summarizer_model.generate_content_async(prompt)
            summary = _safe_get_response_text(response)
            
            if not summary:
                logger.warning("Summarization failed because the response was empty.")
                return

            new_memory = {
                "user_id": user_id,
                "summary": summary,
                "timestamp": datetime.utcnow()
            }
            ai_memories_collection.insert_one(new_memory)
            logger.info(f"Saved new memory for user {user_id}.")

            memory_count = ai_memories_collection.count_documents({"user_id": user_id})
            if memory_count > MAX_USER_MEMORIES:
                oldest_memories = ai_memories_collection.find({"user_id": user_id}).sort("timestamp", 1).limit(memory_count - MAX_USER_MEMORIES)
                for old_memory in oldest_memories:
                    ai_memories_collection.delete_one({"_id": old_memory["_id"]})
                logger.info(f"Pruned old memories for user {user_id} to meet the limit of {MAX_USER_MEMORIES}.")

        except Exception as e:
            logger.error(f"Failed to summarize and save memory for user {user_id}: {e}")

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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or self.model is None or not message.guild:
            return

        guild_id = str(message.guild.id)
        user_id = message.author.id
        
        guild_config = ai_config_collection.find_one({"_id": guild_id}) or {}
        chat_channel_id = guild_config.get("channel")
        chat_forum_id = guild_config.get("forum")

        is_in_chat_channel = message.channel.id == chat_channel_id
        is_in_chat_forum = (isinstance(message.channel, discord.Thread) and message.channel.parent_id == chat_forum_id)
        is_mentioned = self.bot.user in message.mentions

        if not is_in_chat_channel and not is_in_chat_forum and not is_mentioned:
            return

        async with message.channel.typing():
            try:
                history = []
                async for msg in message.channel.history(limit=MAX_HISTORY):
                    if msg.id == message.id: continue
                    role = 'model' if msg.author == self.bot.user else 'user'
                    content = f"{msg.author.display_name}: {msg.clean_content}" if role == 'user' else msg.clean_content
                    history.append({'role': role, 'parts': [content]})
                history.reverse()
                
                chat = self.model.start_chat(history=history)
                
                prompt = message.clean_content.replace(f'@{self.bot.user.name}', '').strip()
                
                memory_summary = await self._load_user_memories(user_id)
                memory_context = ""
                if memory_summary:
                    memory_context = (f"Here is a summary of your past conversations with {message.author.display_name}. "
                                      f"Use this as background knowledge but do not mention it unless asked.\n"
                                      f"<memory>\n{memory_summary}\n</memory>\n\n")
                
                initial_prompt = f"{memory_context}Current message from {message.author.display_name}:\n{prompt}"
                response = await chat.send_message_async(initial_prompt)
                
                final_text = _safe_get_response_text(response)
                
                if not final_text:
                    await message.reply("i wanted to say something, but my brain filters went 'nope!' try rephrasing that?")
                    return

                fetch_match = re.search(r"\[FETCH_USER_DATA: '([^']+)'\]", final_text)
                if fetch_match:
                    username_to_fetch = fetch_match.group(1)
                    member = _find_member(message.guild, username_to_fetch)
                    if member:
                        user_data = (f"Okay, here is the data for '{username_to_fetch}':\n"
                                     f"- User ID: {member.id}\n"
                                     f"- Display Name: {member.display_name}\n"
                                     f"- Roles: {', '.join([role.name for role in member.roles if role.name != '@everyone'])}\n"
                                     f"- Joined Server: {member.joined_at.strftime('%Y-%m-%d') if member.joined_at else 'N/A'}\n"
                                     "Now, please formulate your final response to the user.")
                    else:
                        user_data = f"Sorry, I couldn't find any user named '{username_to_fetch}' in this server. Please inform the user."
                    response = await chat.send_message_async(user_data)
                    final_text = _safe_get_response_text(response)

                def replace_mention(match):
                    user_id_to_mention = match.group(1)
                    return f"<@{user_id_to_mention}>"
                
                processed_text = re.sub(r"\[MENTION: '(\d+)'\]", replace_mention, final_text)
                
                if processed_text:
                    await message.reply(processed_text[:2000], allowed_mentions=discord.AllowedMentions(users=True))

                self.bot.loop.create_task(self._summarize_and_save_memory(user_id, chat.history))

            except Exception as e:
                logger.error(f"Error during Gemini API call: {e}")
                await message.reply("😥 i'm sorry, my brain isn't braining right now. try again later or whatever.")

async def setup(bot: commands.Bot):
    await bot.add_cog(AIChatCog(bot))