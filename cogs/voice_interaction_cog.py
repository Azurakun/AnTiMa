# cogs/voice_interaction_cog.py
import discord 
from discord.ext import commands
import discord.ext.voice_recv
from discord.ext.voice_recv import WaveSink
from discord.voice_client import VoiceClient

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

        # --- NEW: Load the Opus library ---
        if not discord.opus.is_loaded():
            try:
                # Let discord.py find the Opus library automatically
                discord.opus.load_opus('opus')
                logger.info("Opus library loaded successfully.")
            except OSError:
                logger.error(
                    "Could not load the Opus library. "
                    "Make sure it's installed and accessible in your system's PATH. "
                    "Voice functionality will not work."
                )

    def _get_or_create_conversation(self, guild_id):
        if guild_id not in self.conversations:
            model = genai.GenerativeModel('gemini-1.5-flash')
            self.conversations[guild_id] = model.start_chat(history=[])
        return self.conversations[guild_id]

    async def _speak(self, voice_client: VoiceClient, text: str):
        """Converts text to speech and plays it in the voice channel."""
        if not text or not voice_client.is_connected():
            return

        guild_id = voice_client.guild.id
        logger.info(f"[{guild_id}] Setting state to SPEAKING.")
        self.voice_states[guild_id]['is_speaking'] = True

        try:
            tts = gTTS(text=text, lang='en')
            fp = io.BytesIO()
            tts.write_to_fp(fp)
            fp.seek(0)
            
            logger.info(f"[{guild_id}] Playing TTS audio.")
            source = discord.FFmpegOpusAudio(fp, pipe=True)
            voice_client.play(source, after=lambda e: self.bot.loop.call_soon_threadsafe(self._after_speak, guild_id, e))

        except Exception as e:
            logger.exception(f"[{guild_id}] TTS error occurred.")
            self.voice_states[guild_id]['is_speaking'] = False

    def _after_speak(self, guild_id, error):
        """Callback for after the bot finishes speaking."""
        logger.info(f"[{guild_id}] Finished speaking.")
        if error:
            logger.error(f"[{guild_id}] Error after speaking: {error}")
        
        state = self.voice_states.get(guild_id)
        if state and state.get('voice_client'):
             logger.info(f"[{guild_id}] Scheduling listening task.")
             asyncio.run_coroutine_threadsafe(self._start_listening(state['voice_client']), self.bot.loop)
        else:
            logger.warning(f"[{guild_id}] No voice state found after speaking, cannot start listening.")


    def _process_audio(self, sink: WaveSink, guild_id: int):
        """Processes the recorded audio, transcribes it, and gets an AI response."""
        logger.info(f"[{guild_id}] _process_audio callback has been triggered.")
        state = self.voice_states.get(guild_id)

        if not state:
            logger.warning(f"[{guild_id}] No state found in _process_audio, returning.")
            return
            
        if state.get('is_speaking', False):
            logger.info(f"[{guild_id}] Bot is speaking, ignoring incoming audio for now.")
            return

        if not sink.audio_data:
            logger.info(f"[{guild_id}] Sink has no audio data. Restarting listening.")
            asyncio.run_coroutine_threadsafe(self._start_listening(state['voice_client']), self.bot.loop)
            return

        logger.info(f"[{guild_id}] Processing audio for {len(sink.audio_data)} user(s).")
        for user_id, audio in sink.audio_data.items():
            try:
                audio_data = sr.AudioData(audio.file.read(), sink.encoding.sample_rate, 2)
                logger.info(f"[{guild_id}] Transcribing audio for user {user_id}...")
                text = self.recognizer.recognize_google(audio_data)
                logger.info(f"[{guild_id}] User {user_id} said: '{text}'")

                if text:
                    chat = self._get_or_create_conversation(guild_id)
                    response = chat.send_message(text)
                    
                    logger.info(f"[{guild_id}] AI Response: '{response.text}'")
                    asyncio.run_coroutine_threadsafe(
                        self._speak(state['voice_client'], response.text),
                        self.bot.loop
                    )
            except sr.UnknownValueError:
                logger.info(f"[{guild_id}] Could not understand audio from user {user_id}, restarting listening.")
                asyncio.run_coroutine_threadsafe(self._start_listening(state['voice_client']), self.bot.loop)
            except Exception:
                logger.exception(f"[{guild_id}] An unexpected error occurred in _process_audio for user {user_id}.")
                asyncio.run_coroutine_threadsafe(self._start_listening(state['voice_client']), self.bot.loop)

    async def _start_listening(self, vc: VoiceClient):
        """Starts a new listening cycle."""
        if not vc or not vc.is_connected():
            logger.warning(f"[{vc.guild.id if vc else 'N/A'}] Voice client not available or connected, cannot start listening.")
            return
        
        guild_id = vc.guild.id
        logger.info(f"[{guild_id}] Starting a new listening cycle.")
        
        # This state should now be set before we start recording
        self.voice_states[guild_id]['is_speaking'] = False
        
        vc.start_recording(
            WaveSink(), 
            self._process_audio, # Pass the callback function directly
            guild_id # Pass the guild_id as an argument to the callback
        )
        logger.info(f"[{guild_id}] Now recording...")

    @commands.command(name="joinchat")
    async def joinchat(self, ctx: commands.Context):
        """Joins your voice channel and starts a voice conversation."""
        if not ctx.author.voice:
            await ctx.send("you're not in a voice channel, silly!")
            return

        channel = ctx.author.voice.channel
        
        if ctx.voice_client:
            voice_client = await ctx.voice_client.move_to(channel)
        else:
            voice_client = await channel.connect(cls=discord.ext.voice_recv.VoiceRecvClient)

        self.voice_states[ctx.guild.id] = {
            'is_speaking': True, # Start as true because we are about to speak
            'voice_client': voice_client
        }
        
        await self._speak(voice_client, "Hi there! What's up. I'm listening.")

    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        """Leaves the voice channel and ends the conversation."""
        if not ctx.voice_client:
            await ctx.send("i'm not in a voice channel!")
            return
            
        if ctx.guild.id in self.conversations:
            del self.conversations[ctx.guild.id]
        if ctx.guild.id in self.voice_states:
            del self.voice_states[ctx.guild.id]
            
        if ctx.voice_client.is_recording():
            ctx.voice_client.stop_recording()

        await ctx.send("okay, talk to you later!")
        await ctx.voice_client.disconnect()

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceInteractionCog(bot))