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
import webrtcvad  # <-- NEW IMPORT

logger = logging.getLogger(__name__)

# --- Configure Google AI ---
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    logger.error(f"Failed to configure Gemini AI for voice: {e}")

# ------------------- VAD CONSTANTS (CORRECTED) -------------------
SAMPLE_RATE = 48000
FRAME_DURATION_MS = 30  # VAD supports 10, 20, or 30 ms frames
BYTES_PER_SAMPLE = 2    # 16-bit PCM = 2 bytes
CHANNELS = 2            # Discord sends stereo

# Correctly calculate the number of bytes per frame
SAMPLES_PER_FRAME = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000) # 48000 * 0.030 = 1440 samples
BYTES_PER_FRAME_MONO = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE      # 1440 * 2 = 2880 bytes
BYTES_PER_FRAME_STEREO = BYTES_PER_FRAME_MONO * CHANNELS         # 2880 * 2 = 5760 bytes

SILENCE_THRESHOLD = 15 # How many consecutive non-speech frames to wait before stopping


# ------------------- MODIFIED SINK WITH VAD & INTERRUPTION -------------------
class VADBufferingSink(AudioSink):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.vad = webrtcvad.Vad(3)
        self.audio_buffers = {}
        self.user_states = {}

    def wants_opus(self):
        return False

    def write(self, user, data):
        guild_id = user.guild.id
        state = self.cog.voice_states.get(guild_id)

        if state and state.get('is_speaking'):
            logger.info(f"[{guild_id}] User {user} spoke, interrupting bot.")
            state['voice_client'].stop()
            self.user_states.pop(user.id, None)
            self.audio_buffers.pop(user.id, None)
            return

        if user.id not in self.user_states:
            self.user_states[user.id] = {
                'speaking': False,
                'silence_frames': 0,
                'pcm_buffer': bytearray()
            }
        
        user_state = self.user_states[user.id]
        user_state['pcm_buffer'].extend(data.pcm)

        # Process audio in chunks of the correct stereo frame size
        while len(user_state['pcm_buffer']) >= BYTES_PER_FRAME_STEREO:
            # Slice a perfect 30ms stereo chunk from our buffer
            pcm_chunk = user_state['pcm_buffer'][:BYTES_PER_FRAME_STEREO]
            del user_state['pcm_buffer'][:BYTES_PER_FRAME_STEREO]
            
            # Convert the 5760-byte stereo chunk to a 2880-byte mono chunk for the VAD
            mono_chunk = bytearray()
            for i in range(0, len(pcm_chunk), 4):
                mono_chunk.extend(pcm_chunk[i:i+2])

            # This call will now receive a chunk of the correct size (2880 bytes)
            is_speech = self.vad.is_speech(mono_chunk, SAMPLE_RATE)

            if is_speech:
                if not user_state['speaking']:
                    logger.info(f"[{guild_id}] Speech started for user {user.id}")
                    user_state['speaking'] = True
                    self.audio_buffers[user.id] = bytearray()
                
                user_state['silence_frames'] = 0
                self.audio_buffers[user.id].extend(pcm_chunk)

            elif user_state['speaking']:
                user_state['silence_frames'] += 1
                self.audio_buffers[user.id].extend(pcm_chunk)

                if user_state['silence_frames'] > SILENCE_THRESHOLD:
                    logger.info(f"[{guild_id}] Speech ended for user {user.id}. Processing...")
                    audio_to_process = self.audio_buffers.pop(user.id)
                    
                    self.cog.bot.loop.create_task(
                        self.cog._process_audio(audio_to_process, user, guild_id)
                    )
                    
                    user_state['speaking'] = False
                    user_state['silence_frames'] = 0

    def cleanup(self):
        self.close_all()

    def close_all(self):
        self.audio_buffers.clear()
        self.user_states.clear()    


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
            
        if not text or not voice_client.is_connected():
            self.bot.loop.call_soon_threadsafe(self._after_speak, voice_client, None)
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

    def _after_speak(self, vc: VoiceRecvClient, error):
        guild_id = vc.guild.id
        if error:
            logger.error(f"[{guild_id}] Error during/after speaking: {error}")
        
        if guild_id in self.voice_states:
            logger.info(f"[{guild_id}] Finished speaking, setting state to LISTENING.")
            self.voice_states[guild_id]['is_speaking'] = False
        else:
            logger.warning(f"[{guild_id}] Voice state not found after speaking.")
    
    # --- MODIFIED AUDIO PROCESSING FUNCTION ---
    async def _process_audio(self, raw_data, user, guild_id: int):
        state = self.voice_states.get(guild_id)
        if not state or state.get('is_speaking'):
            return # Don't process if we're already speaking or disconnected

        vc = state.get('voice_client')
        if not vc:
            return

        try:
            if not raw_data:
                return

            mem_wav = io.BytesIO()
            with wave.open(mem_wav, 'wb') as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(BYTES_PER_SAMPLE)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(raw_data)
            mem_wav.seek(0)

            with sr.AudioFile(mem_wav) as source:
                audio_data = self.recognizer.record(source)
            
            logger.info(f"[{guild_id}] Transcribing audio for user {user.id}...")
            text = self.recognizer.recognize_google(audio_data)
            logger.info(f"[{guild_id}] User {user.id} said: '{text}'")

            if text and vc.is_connected():
                chat = self._get_or_create_conversation(guild_id)
                response = await chat.send_message_async(text) # Use async for non-blocking call
                
                logger.info(f"[{guild_id}] AI Response: '{response.text}'")
                await self._speak(vc, response.text)
                
        except sr.UnknownValueError:
            logger.info(f"[{guild_id}] Could not understand audio from user {user.id}.")
            await self._speak(vc, "I'm sorry, I didn't catch that. Could you say it again?")
        except Exception as e:
            logger.exception(f"[{guild_id}] An unexpected error in _process_audio for user {user.id}: {e}")

    # --- MODIFIED COMMANDS ---
    @commands.command(name="joinchat")
    async def joinchat(self, ctx: commands.Context):
        if not ctx.author.voice:
            return await ctx.send("you're not in a voice channel, silly!")

        channel = ctx.author.voice.channel
        
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            sink = VADBufferingSink(self)
            vc = await channel.connect(cls=VoiceRecvClient)
            self.voice_states[ctx.guild.id] = { 
                'voice_client': vc, 
                'is_speaking': False, 
                'sink': sink 
            }
            vc.listen(sink) # Start listening continuously
            await self._speak(vc, "Hi there! I'm listening.")

    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        vc = ctx.voice_client
        guild_id = ctx.guild.id
        if not vc:
            return await ctx.send("i'm not in a voice channel!")
            
        if vc.is_listening():
            vc.stop_listening()
        
        if guild_id in self.voice_states:
            # Clean up the sink properly
            sink = self.voice_states[guild_id].get('sink')
            if sink:
                sink.cleanup()
            del self.voice_states[guild_id]
            
        if guild_id in self.conversations:
            del self.conversations[guild_id]

        await vc.disconnect()
        await ctx.send("okay, talk to you later!")

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceInteractionCog(bot))