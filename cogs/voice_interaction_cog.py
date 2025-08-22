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

logger = logging.getLogger(__name__)

# --- Configure Google AI ---
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    logger.error(f"Failed to configure Gemini AI for voice: {e}")


# ------------------- NEW CLASS DEFINITION START -------------------
class BufferingSink(AudioSink):
    def __init__(self):
        super().__init__()
        self.audio_data = {}

    def wants_opus(self):
        return False

    def write(self, user, data):
        if user not in self.audio_data:
            self.audio_data[user] = io.BytesIO()
        self.audio_data[user].write(data.pcm)

    def cleanup(self):
        pass

    def close_all(self):
        for buffer in self.audio_data.values():
            buffer.close()
        self.audio_data.clear()
# -------------------- NEW CLASS DEFINITION END --------------------


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

    async def _listening_task(self, vc: VoiceRecvClient, duration: float):
        await asyncio.sleep(duration)
        if vc.is_connected() and vc.is_listening():
            logger.info(f"[{vc.guild.id}] Listening timeout reached, stopping listener.")
            vc.stop_listening()

    def _after_speak(self, vc: VoiceRecvClient, error):
        guild_id = vc.guild.id
        logger.info(f"[{guild_id}] Finished speaking.")
        if error:
            logger.error(f"[{guild_id}] Error after speaking: {error}")
        
        if guild_id not in self.voice_states:
            logger.warning(f"[{guild_id}] Voice state not found after speaking. Cannot start new listening cycle.")
            return

        self.voice_states[guild_id]['is_speaking'] = False
        
        sink = BufferingSink()
        self.voice_states[guild_id]['sink'] = sink

        logger.info(f"[{guild_id}] Starting a new listening cycle.")

        def after_listening_callback(sink_from_callback, error=None):
            if error:
                logger.error(f"[{guild_id}] Error during listening: {error}")
                return
            
            stored_sink = self.voice_states.get(guild_id, {}).get('sink')
            self.bot.loop.call_soon_threadsafe(self._process_audio, stored_sink, guild_id)
        
        vc.listen(sink, after=after_listening_callback)
        self.bot.loop.create_task(self._listening_task(vc, 15.0))

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
                    # Use hardcoded standard Discord audio properties
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