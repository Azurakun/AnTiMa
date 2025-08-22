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
import time
import webrtcvad  # Import the VAD library

logger = logging.getLogger(__name__)

# --- Configure Google AI ---
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    logger.error(f"Failed to configure Gemini AI for voice: {e}")


# ------------------- MODIFIED SINK CLASS WITH VAD -------------------
class VADBufferingSink(AudioSink):
    """
    An AudioSink that buffers audio data and uses WebRTC VAD to detect
    speech and silence, and to handle bot interruptions.
    """
    def __init__(self, cog, voice_client, silence_threshold=3.0, speech_interrupt_threshold=1.0):
        super().__init__()
        self.cog = cog
        self.voice_client = voice_client
        self.audio_data = {}
        self.last_speech_time = {}
        self.user_speech_start_time = {}
        
        # --- Configurable thresholds ---
        self.silence_threshold = silence_threshold  # Seconds of silence to stop listening
        self.speech_interrupt_threshold = speech_interrupt_threshold # Seconds of user speech to interrupt bot
        
        # --- VAD Configuration ---
        self.vad = webrtcvad.Vad()
        self.vad.set_mode(3)  # Set VAD aggressiveness (0-3, 3 is most aggressive)

        # Discord sends audio in 20ms chunks.
        # VAD requires specific frame lengths (10, 20, or 30 ms).
        self.SAMPLE_RATE = 48000  # Discord's sample rate
        self.FRAME_DURATION_MS = 20
        # Calculate bytes per frame for 16-bit stereo audio
        self.FRAME_SIZE = int(self.SAMPLE_RATE * (self.FRAME_DURATION_MS / 1000.0) * 2 * 2) 

    def wants_opus(self):
        # We want raw PCM data for VAD
        return False

    def write(self, user, data):
        """This method is called for every audio packet received from a user."""
        guild_id = self.voice_client.guild.id
        
        # --- Bot Interruption Logic ---
        # Check if the bot is currently speaking
        if self.cog.voice_states.get(guild_id, {}).get('is_speaking'):
            # Check if the incoming audio packet contains speech
            is_speech = self.vad.is_speech(data.pcm, self.SAMPLE_RATE)
            if is_speech:
                # If user starts speaking, record the time
                if user not in self.user_speech_start_time:
                    self.user_speech_start_time[user] = time.time()
                
                # If the user has been speaking for longer than the threshold, interrupt the bot
                if time.time() - self.user_speech_start_time[user] > self.speech_interrupt_threshold:
                    logger.info(f"[{guild_id}] User {user} is speaking, interrupting the bot.")
                    self.voice_client.stop() # Stop the bot's current playback
                    self.cog.voice_states[guild_id]['is_speaking'] = False # Update state
                    self.user_speech_start_time.pop(user, None) # Reset timer
            else:
                 # If it's not speech, reset the timer
                 self.user_speech_start_time.pop(user, None)

        # --- Silence Detection Logic ---
        if user not in self.audio_data:
            self.audio_data[user] = io.BytesIO()
            self.last_speech_time[user] = time.time()

        # Buffer the audio data for this user
        self.audio_data[user].write(data.pcm)

        # Use VAD to check for speech in the current audio frame
        is_speech = self.vad.is_speech(data.pcm, self.SAMPLE_RATE)
        if is_speech:
            # If speech is detected, update the last speech time
            self.last_speech_time[user] = time.time()

        # If it's been silent for longer than the threshold, stop listening
        if time.time() - self.last_speech_time.get(user, time.time()) > self.silence_threshold:
            logger.info(f"[{guild_id}] Silence detected for user {user}. Stopping listening.")
            # stop_listening() will trigger the 'after' callback for processing
            if self.voice_client.is_listening():
                 self.voice_client.stop_listening()

    def cleanup(self):
        """Called when the sink is being destroyed."""
        self.close_all()

    def close_all(self):
        """Closes all audio buffers."""
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
            model = genai.GenerativeModel('gemini-1.5-flash')
            self.conversations[guild_id] = model.start_chat(history=[])
        return self.conversations[guild_id]

    async def _speak(self, voice_client: VoiceRecvClient, text: str):
        guild_id = voice_client.guild.id
        if guild_id not in self.voice_states:
            return
            
        # If text is empty, it's a signal to just start listening again.
        if not text:
            self.bot.loop.call_soon_threadsafe(self._after_speak, voice_client, None)
            return

        if not voice_client.is_connected():
            return
            
        logger.info(f"[{guild_id}] Setting state to SPEAKING.")
        self.voice_states[guild_id]['is_speaking'] = True

        try:
            tts = gTTS(text=text, lang='en')
            fp = io.BytesIO()
            tts.write_to_fp(fp)
            fp.seek(0)
            
            source = discord.FFmpegOpusAudio(fp, pipe=True)
            voice_client.play(source, after=lambda e: self.bot.loop.call_soon_threadsafe(self._after_speak, voice_client, e))

        except Exception as e:
            logger.exception(f"[{guild_id}] TTS error occurred: {e}")
            if guild_id in self.voice_states:
                self.voice_states[guild_id]['is_speaking'] = False
            self.bot.loop.call_soon_threadsafe(self._after_speak, voice_client, "TTS Failure")

    # The timer-based _listening_task is no longer needed with VAD.
    # async def _listening_task(self, vc: VoiceRecvClient, duration: float):
    #     ...

    def _after_speak(self, vc: VoiceRecvClient, error):
        guild_id = vc.guild.id
        logger.info(f"[{guild_id}] Finished speaking.")
        if error:
            logger.error(f"[{guild_id}] Error after speaking: {error}")
        
        if guild_id not in self.voice_states:
            logger.warning(f"[{guild_id}] Voice state not found after speaking. Cannot start new listening cycle.")
            return

        self.voice_states[guild_id]['is_speaking'] = False
        
        # Use our new VAD-enabled sink.
        # You can adjust the thresholds here.
        sink = VADBufferingSink(self, vc, silence_threshold=3.0, speech_interrupt_threshold=1.0) 
        self.voice_states[guild_id]['sink'] = sink

        logger.info(f"[{guild_id}] Starting a new listening cycle.")

        def after_listening_callback(sink_from_callback, error=None):
            if error:
                logger.error(f"[{guild_id}] Error during listening: {error}")
                return
            
            # Retrieve the sink from our managed state to process its data
            stored_sink = self.voice_states.get(guild_id, {}).get('sink')
            self.bot.loop.call_soon_threadsafe(self._process_audio, stored_sink, guild_id)
        
        vc.listen(sink, after=after_listening_callback)
        # We no longer need to create the _listening_task.

    def _process_audio(self, sink: VADBufferingSink, guild_id: int):
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

        # Process the buffered audio for each user
        for user_id, audio_buffer in sink.audio_data.items():
            try:
                raw_data = audio_buffer.getvalue()
                if not raw_data:
                    continue

                # Convert raw PCM to a WAV format in memory for the speech_recognition library
                mem_wav = io.BytesIO()
                with wave.open(mem_wav, 'wb') as wf:
                    wf.setnchannels(2)      # Stereo
                    wf.setsampwidth(2)      # 16-bit
                    wf.setframerate(48000)  # 48kHz
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
                    sink.close_all() # Clean up after processing
                    return # Process one user at a time
                    
            except sr.UnknownValueError:
                logger.info(f"[{guild_id}] Could not understand audio from user {user_id}.")
            except Exception as e:
                logger.exception(f"[{guild_id}] An unexpected error in _process_audio for user {user_id}: {e}")
        
        # If no speech was transcribed, start listening again
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
            try:
                # --- ADDED ERROR HANDLING ---
                # Try to connect, but with a timeout to prevent hanging
                vc = await channel.connect(cls=VoiceRecvClient, timeout=10.0)
                self.voice_states[ctx.guild.id] = { 'voice_client': vc, 'is_speaking': False, 'sink': None }
                await self._speak(vc, "Hi there! I'm listening.")
            except asyncio.TimeoutError:
                # If the connection times out, inform the user and stop.
                await ctx.send("I couldn't connect to the voice channel in time. Please check my permissions and try again!")
            except Exception as e:
                # Catch any other potential connection errors
                await ctx.send("An unexpected error occurred while trying to connect.")
                logger.error(f"Error connecting to voice channel: {e}")


    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        vc = ctx.voice_client
        if not vc:
            return await ctx.send("i'm not in a voice channel!")
            
        if vc.is_listening():
            vc.stop_listening()

        if ctx.guild.id in self.voice_states:
            # Ensure the sink is cleaned up properly
            sink = self.voice_states[ctx.guild.id].get('sink')
            if sink:
                sink.close_all()
            del self.voice_states[ctx.guild.id]
            
        if ctx.guild.id in self.conversations:
            del self.conversations[ctx.guild.id]

        await vc.disconnect()
        await ctx.send("okay, talk to you later!")

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceInteractionCog(bot))
