# cogs/voice_interaction_cog.py
import discord
from discord.ext import commands
import logging
import asyncio
import speech_recognition as sr
from gtts import gTTS
import google.generativeai as genai
import os
import io

logger = logging.getLogger(__name__)

# --- Configure Google AI ---
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    logger.error(f"Failed to configure Gemini AI for voice: {e}")

class VoiceInteractionCog(commands.Cog, name="VoiceInteraction"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recognizer = sr.Recognizer()
        self.conversations = {}
        self.voice_states = {}

    def _get_or_create_conversation(self, guild_id):
        if guild_id not in self.conversations:
            model = genai.GenerativeModel('gemini-1.5-flash')
            self.conversations[guild_id] = model.start_chat(history=[])
        return self.conversations[guild_id]

    async def _speak(self, voice_client: discord.VoiceClient, text: str):
        """Converts text to speech and plays it in the voice channel."""
        if not text or not voice_client.is_connected():
            return

        guild_id = voice_client.guild.id
        self.voice_states[guild_id]['is_speaking'] = True

        try:
            tts = gTTS(text=text, lang='en')
            fp = io.BytesIO()
            tts.write_to_fp(fp)
            fp.seek(0)
            
            voice_client.play(discord.FFmpegPCMAudio(fp, pipe=True), after=lambda e: self._after_speak(guild_id, e))
        except Exception as e:
            logger.error(f"TTS error: {e}")
            self.voice_states[guild_id]['is_speaking'] = False

    def _after_speak(self, guild_id, error):
        """Callback for after the bot finishes speaking."""
        if error:
            logger.error(f"Error after speaking: {error}")
        
        state = self.voice_states.get(guild_id)
        if state:
            asyncio.run_coroutine_threadsafe(self._start_listening(state['voice_client']), self.bot.loop)

    def _process_audio(self, sink: discord.sinks.WaveSink, guild_id: int):
        """Processes the recorded audio, transcribes it, and gets an AI response."""
        state = self.voice_states.get(guild_id)
        if not state or state['is_speaking']:
            return

        for user_id, audio in sink.audio_data.items():
            try:
                audio_data = sr.AudioData(audio.file.read(), sink.encoding.sample_rate, 2)
                text = self.recognizer.recognize_google(audio_data)
                logger.info(f"User {user_id} said: {text}")

                if text:
                    chat = self._get_or_create_conversation(guild_id)
                    response = chat.send_message(text)
                    
                    asyncio.run_coroutine_threadsafe(
                        self._speak(state['voice_client'], response.text),
                        self.bot.loop
                    )
            except sr.UnknownValueError:
                logger.info("Could not understand audio, restarting listening.")
                asyncio.run_coroutine_threadsafe(self._start_listening(state['voice_client']), self.bot.loop)
            except Exception as e:
                logger.error(f"Error processing audio for user {user_id}: {e}")
                asyncio.run_coroutine_threadsafe(self._start_listening(state['voice_client']), self.bot.loop)

    async def _start_listening(self, vc: discord.VoiceClient):
        """Starts a new listening cycle."""
        if not vc or not vc.is_connected() or self.voice_states[vc.guild.id]['is_speaking']:
            return
        
        # Give a small buffer
        await asyncio.sleep(0.5)

        self.voice_states[vc.guild.id]['is_speaking'] = False
        
        vc.listen(
            discord.sinks.WaveSink(), 
            after=lambda sink, gid=vc.guild.id: self._process_audio(sink, gid)
        )

    @commands.command(name="joinchat")
    async def joinchat(self, ctx: commands.Context):
        """Joins your voice channel and starts a voice conversation."""
        if not ctx.author.voice:
            await ctx.send("you're not in a voice channel, silly!")
            return

        channel = ctx.author.voice.channel
        
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()

        self.voice_states[ctx.guild.id] = {
            'is_speaking': True, # Start as true until after greeting
            'voice_client': ctx.voice_client
        }
        
        await self._speak(ctx.voice_client, "Hi there! I'm listening.")
        # Listening will be started automatically by the _after_speak callback

    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        """Leaves the voice channel and ends the conversation."""
        if not ctx.voice_client:
            await ctx.send("i'm not in a voice channel!")
            return

        if ctx.voice_client.is_listening():
            ctx.voice_client.stop_listening()
            
        await ctx.voice_client.disconnect()
        
        if ctx.guild.id in self.conversations:
            del self.conversations[ctx.guild.id]
        if ctx.guild.id in self.voice_states:
            del self.voice_states[ctx.guild.id]
            
        await ctx.send("okay, talk to you later!")

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceInteractionCog(bot))