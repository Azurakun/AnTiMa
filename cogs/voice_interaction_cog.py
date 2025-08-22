# cogs/voice_interaction_cog.py
import discord
from discord.ext import commands
import discord.ext.voice_recv
from discord.ext.voice_recv import VoiceRecvClient
from discord.ext.voice_recv.sinks import AudioSink
import logging
import asyncio
import speech_recognition as sr
from gtts import gTTS
import google.generativeai as genai
import os
import io
import wave
import audioop # Required for volume analysis
import time    # Required for the silence timer

logger = logging.getLogger(__name__)

VOICE_SYSTEM_PROMPT = """
Act as a friendly and helpful voice assistant. Keep your responses concise and clear. Your name is AnTiMa. When asked about your creator, say you were created by Azura.
"""

# --- Configure Google AI ---
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    logger.error(f"Failed to configure Gemini AI for voice: {e}")


# ------------------- NEW InterruptSink CLASS -------------------
# This sink's only job is to detect the start of speech to interrupt the bot.
class InterruptSink(AudioSink):
    def __init__(self, interrupt_callback, volume_threshold=400):
        super().__init__()
        self.interrupt_callback = interrupt_callback
        self.volume_threshold = volume_threshold

    def wants_opus(self):
        return False

    def write(self, user, data):
        # Check the volume of the incoming audio
        volume = audioop.rms(data.pcm, 2)
        if volume > self.volume_threshold:
            # If volume is high enough, trigger the interrupt and stop listening
            if self.interrupt_callback:
                logger.info(f"Interruption detected from user {user}!")
                self.interrupt_callback()
                self.interrupt_callback = None # Fire only once

# ------------------- BufferingSink for VAD -------------------
class BufferingSink(AudioSink):
    def __init__(self, stop_callback, silence_duration=0.5, volume_threshold=400):
        super().__init__()
        self.stop_callback = stop_callback
        self.silence_duration = silence_duration
        self.volume_threshold = volume_threshold
        self.audio_data = {}
        self.last_speech_time = None
        self.has_spoken = False 

    def wants_opus(self):
        return False

    def write(self, user, data):
        volume = audioop.rms(data.pcm, 2)
        is_speaking = volume > self.volume_threshold

        if is_speaking:
            self.last_speech_time = time.time()
            if not self.has_spoken:
                logger.info("Speech detected, VAD timer armed.")
                self.has_spoken = True

        if user not in self.audio_data:
            self.audio_data[user] = io.BytesIO()
        self.audio_data[user].write(data.pcm)

        if self.has_spoken and not is_speaking and self.stop_callback:
            time_since_last_speech = time.time() - self.last_speech_time
            if time_since_last_speech > self.silence_duration:
                logger.info(f"Silence detected for {self.silence_duration}s, stopping listener.")
                self.stop_callback()
                self.stop_callback = None

    def cleanup(self):
        pass

    def close_all(self):
        for buffer in self.audio_data.values():
            buffer.close()
        self.audio_data.clear()


class VoiceInteractionCog(commands.Cog, name="VoiceInteraction"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recognizer = sr.Recognizer()
        self.conversations = {}
        self.voice_states = {}

        if not discord.opus.is_loaded():
            try:
                discord.opus.load_opus('opus')
                logger.info("Opus library loaded successfully.")
            except OSError:
                logger.error("Could not load the Opus library. Voice functionality may not work.")

    def _get_or_create_conversation(self, guild_id):
        if guild_id not in self.conversations:
            model = genai.GenerativeModel(
                'gemini-1.5-flash',
                system_instruction=VOICE_SYSTEM_PROMPT
            )
            self.conversations[guild_id] = model.start_chat(history=[])
        return self.conversations[guild_id]

    # This is the new, primary speaking method with interruption logic
    async def _speak(self, voice_client: VoiceRecvClient, text: str):
        guild_id = voice_client.guild.id
        if guild_id not in self.voice_states:
            return

        if not text:
            # If there's nothing to say, go straight to listening for user input
            self.bot.loop.call_soon_threadsafe(self._start_listening_cycle, voice_client, None)
            return

        if not voice_client.is_connected():
            return

        logger.info(f"[{guild_id}] Setting state to SPEAKING.")
        self.voice_states[guild_id]['is_speaking'] = True

        try:
            # Generate TTS audio
            tts = gTTS(text=text, lang='en')
            fp = io.BytesIO()
            tts.write_to_fp(fp)
            fp.seek(0)
            
            source = discord.FFmpegOpusAudio(fp, pipe=True)

            # --- Interruption Logic ---
            # Define a callback to stop the bot's playback
            interrupt_callback = lambda: self.bot.loop.call_soon_threadsafe(voice_client.stop)
            
            # Start listening for interruptions while we are speaking
            interrupt_sink = InterruptSink(interrupt_callback)
            voice_client.listen(interrupt_sink)
            
            # Play the audio. The 'after' callback will clean up the interrupt listener
            voice_client.play(source, after=lambda e: self.bot.loop.call_soon_threadsafe(self._after_speak_cleanup, voice_client, e))

        except Exception as e:
            logger.exception(f"[{guild_id}] TTS error occurred: {e}")
            if guild_id in self.voice_states:
                self.voice_states[guild_id]['is_speaking'] = False
            self.bot.loop.call_soon_threadsafe(self._start_listening_cycle, voice_client, "TTS Failure")
    
    # This function is called after speaking (or being interrupted).
    # It cleans up the interrupt listener and starts the main VAD listener.
    def _after_speak_cleanup(self, vc: VoiceRecvClient, error):
        if vc.is_listening():
            vc.stop_listening() # Stop the interrupt listener
        self._start_listening_cycle(vc, error)

    # This function is now dedicated to starting the main listening cycle for user input.
    def _start_listening_cycle(self, vc: VoiceRecvClient, error):
        guild_id = vc.guild.id
        logger.info(f"[{guild_id}] Finished speaking/interrupted.")
        if error:
            logger.error(f"[{guild_id}] Error after speaking: {error}")
        
        if guild_id not in self.voice_states:
            return

        self.voice_states[guild_id]['is_speaking'] = False
        
        stop_listening_callback = lambda: self.bot.loop.call_soon_threadsafe(vc.stop_listening)
        sink = BufferingSink(stop_listening_callback, silence_duration=0.5)
        self.voice_states[guild_id]['sink'] = sink

        logger.info(f"[{guild_id}] Starting a new listening cycle with VAD.")

        def after_listening_callback(sink_from_callback, error=None):
            if error:
                logger.error(f"[{guild_id}] Error during listening: {error}")
                return
            
            stored_sink = self.voice_states.get(guild_id, {}).get('sink')
            self.bot.loop.call_soon_threadsafe(self._process_audio, stored_sink, guild_id)
        
        vc.listen(sink, after=after_listening_callback)

    def _process_audio(self, sink: BufferingSink, guild_id: int):
        if not sink:
            logger.warning(f"[{guild_id}] _process_audio was called with a None sink. Restarting loop.")
            vc = self.voice_states.get(guild_id, {}).get('voice_client')
            if vc:
                 asyncio.run_coroutine_threadsafe(self._speak(vc, ""), self.bot.loop)
            return

        logger.info(f"[{guild_id}] _process_audio callback has been triggered.")
        
        state = self.voice_states.get(guild_id)
        if not state or state.get('is_speaking'):
            sink.close_all()
            return

        vc = state.get('voice_client')
        if not vc:
            sink.close_all()
            return

        for user_id, audio_buffer in sink.audio_data.items():
            try:
                raw_data = audio_buffer.getvalue()
                if not raw_data:
                    continue

                mem_wav = io.BytesIO()
                with wave.open(mem_wav, 'wb') as wf:
                    wf.setnchannels(2)
                    wf.setsampwidth(2)
                    wf.setframerate(48000)
                    wf.writeframes(raw_data)
                mem_wav.seek(0)

                with sr.AudioFile(mem_wav) as source:
                    audio_data = self.recognizer.record(source)
                
                logger.info(f"[{guild_id}] Transcribing audio for user {user_id}...")
                text = self.recognizer.recognize_google(audio_data)
                logger.info(f"[{guild_id}] User {user_id} said: '{text}'")

                if text and vc.is_connected():
                    chat = self._get_or_create_conversation(guild_id)
                    response = chat.send_message(text)
                    
                    logger.info(f"[{guild_id}] AI Response: '{response.text}'")
                    asyncio.run_coroutine_threadsafe(self._speak(vc, response.text), self.bot.loop)
                    sink.close_all()
                    return
                    
            except sr.UnknownValueError:
                logger.info(f"[{guild_id}] Could not understand audio from user {user_id}.")
            except Exception as e:
                logger.exception(f"[{guild_id}] An unexpected error in _process_audio for user {user_id}: {e}")
        
        asyncio.run_coroutine_threadsafe(self._speak(vc, ""), self.bot.loop)
        sink.close_all()


    @commands.command(name="joinchat")
    async def joinchat(self, ctx: commands.Context):
        if not ctx.author.voice:
            return await ctx.send("you're not in a voice channel, silly!")

        channel = ctx.author.voice.channel
        
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            vc = await channel.connect(cls=VoiceRecvClient)
            self.voice_states[ctx.guild.id] = { 'voice_client': vc, 'is_speaking': False, 'sink': None }
            await self._speak(vc, "Hi there! I'm listening.")

    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        vc = ctx.voice_client
        if not vc:
            return await ctx.send("i'm not in a voice channel!")
            
        if vc.is_listening():
            vc.stop_listening()

        if ctx.guild.id in self.voice_states:
            del self.voice_states[ctx.guild.id]
            
        if ctx.guild.id in self.conversations:
            del self.conversations[ctx.guild.id]

        await vc.disconnect()
        await ctx.send("okay, talk to you later!")

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceInteractionCog(bot))