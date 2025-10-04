# cogs/voice_interaction_cog.py
import discord # Make sure discord is imported
from discord.ext import commands
import discord.ext.voice_recv
from discord.ext.voice_recv import VoiceRecvClient
from discord.ext.voice_recv.sinks import AudioSink
import logging
import asyncio
import speech_recognition as sr
import google.generativeai as genai
import os
import io
import wave
import webrtcvad
import elevenlabs
from elevenlabs.client import ElevenLabs

logger = logging.getLogger(__name__)

# --- Configure APIs ---
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    logger.error(f"Failed to configure APIs for voice: {e}")

# --- VAD CONSTANTS ---
SAMPLE_RATE = 48000
FRAME_DURATION_MS = 30
BYTES_PER_SAMPLE = 2
CHANNELS = 2
SAMPLES_PER_FRAME = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)
BYTES_PER_FRAME_MONO = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE
BYTES_PER_FRAME_STEREO = BYTES_PER_FRAME_MONO * CHANNELS
SILENCE_THRESHOLD = 15

# --- VADBufferingSink Class (Unchanged) ---
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

        while len(user_state['pcm_buffer']) >= BYTES_PER_FRAME_STEREO:
            pcm_chunk = user_state['pcm_buffer'][:BYTES_PER_FRAME_STEREO]
            del user_state['pcm_buffer'][:BYTES_PER_FRAME_STEREO]
            
            mono_chunk = bytearray()
            for i in range(0, len(pcm_chunk), 4):
                mono_chunk.extend(pcm_chunk[i:i+2])

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

# --- Main Cog ---
class VoiceInteractionCog(commands.Cog, name="VoiceInteraction"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recognizer = sr.Recognizer()
        self.conversations = {}
        self.voice_states = {}
        
        self.elevenlabs_client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
        self.elevenlabs_voice_id = "21m00Tcm4TlvDq8ikWAM" # Example: "Rachel"
        self.elevenlabs_model_id = "eleven_turbo_v2"

        if not discord.opus.is_loaded():
            try:
                discord.opus.load_opus('opus')
                logger.info("Opus library loaded successfully.")
            except OSError:
                logger.error("Could not load the Opus library.")

    def _get_or_create_conversation(self, guild_id):
        if guild_id not in self.conversations:
            model = genai.GenerativeModel('gemini-1.5-flash')
            self.conversations[guild_id] = model.start_chat(history=[])
        return self.conversations[guild_id]

    async def _speak_streamed(self, voice_client: VoiceRecvClient, text_to_speak: str):
        guild_id = voice_client.guild.id
        
        logger.info(f"[{guild_id}] Setting state to SPEAKING for streamed audio.")
        self.voice_states[guild_id]['is_speaking'] = True

        try:
            audio_stream = self.elevenlabs_client.text_to_speech.stream(
                text=text_to_speak,
                voice_id=self.elevenlabs_voice_id,
                model_id=self.elevenlabs_model_id
            )

            if voice_client.is_connected():
                full_audio = b"".join(audio_stream)
                if full_audio and voice_client.is_connected():
                    # <-- FIX: Use FFmpegPCMAudio to decode the MP3 stream
                    source = discord.FFmpegPCMAudio(io.BytesIO(full_audio))
                    finished = asyncio.Event()
                    voice_client.play(source, after=lambda e: self.bot.loop.call_soon_threadsafe(finished.set))
                    await finished.wait()

        except Exception as e:
            logger.exception(f"[{guild_id}] Error during streamed TTS playback: {e}")
        finally:
            if guild_id in self.voice_states:
                logger.info(f"[{guild_id}] Finished speaking streamed audio.")
                self.voice_states[guild_id]['is_speaking'] = False

    async def _process_audio(self, raw_data, user, guild_id: int):
        state = self.voice_states.get(guild_id)
        if not state or state.get('is_speaking'): return

        vc = state.get('voice_client')
        if not vc: return

        try:
            if not raw_data: return
            
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
                response = await chat.send_message_async(text)
                await self._speak_streamed(vc, response.text)
                
        except sr.UnknownValueError:
            logger.info(f"[{guild_id}] Could not understand audio from user {user.id}.")
            audio_generator = self.elevenlabs_client.text_to_speech.stream(
                text="I'm sorry, I didn't catch that.", 
                voice_id=self.elevenlabs_voice_id,
                model_id=self.elevenlabs_model_id
            )
            full_audio = b"".join(audio_generator)
            # <-- FIX: Use FFmpegPCMAudio here too
            source = discord.FFmpegPCMAudio(io.BytesIO(full_audio))
            if vc.is_connected(): vc.play(source)

        except Exception as e:
            logger.exception(f"[{guild_id}] An unexpected error in _process_audio for user {user.id}: {e}")

    @commands.command(name="joinchat")
    async def joinchat(self, ctx: commands.Context):
        if not ctx.author.voice:
            return await ctx.send("you're not in a voice channel, silly!")

        channel = ctx.author.voice.channel
        
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            sink = VADBufferingSink(self)
            try:
                vc = await channel.connect(cls=VoiceRecvClient)
            except discord.ClientException:
                await ctx.voice_client.move_to(channel)
                vc = ctx.voice_client

            self.voice_states[ctx.guild.id] = { 
                'voice_client': vc, 
                'is_speaking': False, 
                'sink': sink 
            }
            vc.listen(sink)
            
            audio_generator = self.elevenlabs_client.text_to_speech.stream(
                text="Hi there! I'm listening.", 
                voice_id=self.elevenlabs_voice_id,
                model_id=self.elevenlabs_model_id
            )
            full_audio = b"".join(audio_generator)
            # <-- FIX: And use FFmpegPCMAudio here as well
            source = discord.FFmpegPCMAudio(io.BytesIO(full_audio))
            vc.play(source)

    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        vc = ctx.voice_client
        guild_id = ctx.guild.id
        if not vc:
            return await ctx.send("i'm not in a voice channel!")
            
        if vc.is_listening():
            vc.stop_listening()
        
        if guild_id in self.voice_states:
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