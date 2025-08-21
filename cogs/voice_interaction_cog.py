# cogs/voice_interaction_cog.py
import discord
from discord.ext import commands, tasks
import logging
import asyncio
import speech_recognition as sr
from gtts import gTTS
import google.generativeai as genai
import os
import io
import wave
import audioop

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
        if error:
            logger.error(f"Error after speaking: {error}")
        
        state = self.voice_states.get(guild_id)
        if state:
            state['is_speaking'] = False

    @tasks.loop(seconds=1.0)
    async def listen_and_process_task(self, guild_id):
        state = self.voice_states.get(guild_id)
        if not state or state['is_speaking'] or not state['voice_client'].is_connected():
            return

        vc = state['voice_client']
        if not hasattr(vc, 'ws') or not vc.ws:
            return
            
        audio_buffer = state.get('audio_buffer', io.BytesIO())
        
        while len(vc.ws.recv_buffer) > 0:
            packet = vc.ws.recv_buffer.popleft()
            if packet:
                # Decode from opus and write raw PCM data to buffer
                decoded_packet = vc.decoder.decode(packet)
                audio_buffer.write(decoded_packet)

        state['audio_buffer'] = audio_buffer

    @commands.command(name="joinchat")
    async def joinchat(self, ctx: commands.Context):
        if not ctx.author.voice:
            await ctx.send("you're not in a voice channel, silly!")
            return

        channel = ctx.author.voice.channel
        
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()

        self.voice_states[ctx.guild.id] = {
            'is_speaking': False,
            'voice_client': ctx.voice_client,
            'audio_buffer': io.BytesIO()
        }
        
        await self._speak(ctx.voice_client, "Hi there! I'm listening.")
        self.listen_and_process_task.start(ctx.guild.id)

    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        if not ctx.voice_client:
            await ctx.send("i'm not in a voice channel!")
            return

        self.listen_and_process_task.cancel()
        
        await ctx.voice_client.disconnect()
        
        if ctx.guild.id in self.conversations:
            del self.conversations[ctx.guild.id]
        if ctx.guild.id in self.voice_states:
            del self.voice_states[ctx.guild.id]
            
        await ctx.send("okay, talk to you later!")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # A simple way to trigger speech processing when a user stops talking
        # This is not perfect, but works without complex voice activity detection
        if not member.bot and before.speaking and not after.speaking:
            state = self.voice_states.get(member.guild.id)
            if state and not state['is_speaking'] and state['voice_client'].channel == before.channel:
                buffer = state.get('audio_buffer')
                if buffer and buffer.tell() > 20000: # Process if there's a decent amount of audio
                    self.process_audio_buffer(member.guild.id, buffer)
                    state['audio_buffer'] = io.BytesIO() # Reset buffer

    def process_audio_buffer(self, guild_id, buffer):
        state = self.voice_states.get(guild_id)
        if not state: return

        buffer.seek(0)
        
        # Convert raw PCM to a WAV file in memory for speech_recognition
        with io.BytesIO() as wav_buffer:
            with wave.open(wav_buffer, 'wb') as wf:
                wf.setnchannels(discord.opus.Decoder.CHANNELS)
                wf.setsampwidth(discord.opus.Decoder.SAMPLE_SIZE // discord.opus.Decoder.CHANNELS)
                wf.setframerate(discord.opus.Decoder.SAMPLING_RATE)
                wf.writeframes(buffer.read())
            
            wav_buffer.seek(0)
            with sr.AudioFile(wav_buffer) as source:
                audio_data = self.recognizer.record(source)

        try:
            text = self.recognizer.recognize_google(audio_data)
            logger.info(f"User said: {text}")

            if text:
                chat = self._get_or_create_conversation(guild_id)
                response = chat.send_message(text)
                asyncio.run_coroutine_threadsafe(
                    self._speak(state['voice_client'], response.text),
                    self.bot.loop
                )
        except sr.UnknownValueError:
            pass # Ignore if speech is not understood
        except Exception as e:
            logger.error(f"Error in STT processing: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceInteractionCog(bot))