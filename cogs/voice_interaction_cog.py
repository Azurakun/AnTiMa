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
        self.conversations = {}  # {guild_id: GenerativeModel.start_chat()}
        self.voice_states = {}   # {guild_id: {'is_speaking': False, 'is_listening': False, 'voice_client': vc}}

    def _get_or_create_conversation(self, guild_id):
        if guild_id not in self.conversations:
            model = genai.GenerativeModel('gemini-1.5-flash')
            self.conversations[guild_id] = model.start_chat(history=[])
        return self.conversations[guild_id]

    async def _speak(self, voice_client: discord.VoiceClient, text: str):
        """Converts text to speech and plays it in the voice channel."""
        if not text:
            return

        guild_id = voice_client.guild.id
        self.voice_states[guild_id]['is_speaking'] = True

        try:
            # Generate TTS audio in memory
            tts = gTTS(text=text, lang='en')
            fp = io.BytesIO()
            tts.write_to_fp(fp)
            fp.seek(0)
            
            # Play the audio
            voice_client.play(discord.FFmpegPCMAudio(fp, pipe=True), after=lambda e: self._after_speak(guild_id, e))
        except Exception as e:
            logger.error(f"TTS error: {e}")
            self.voice_states[guild_id]['is_speaking'] = False

    def _after_speak(self, guild_id, error):
        """Callback for after the bot finishes speaking."""
        if error:
            logger.error(f"Error after speaking: {error}")
        
        # Give a small buffer before listening again
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.5), self.bot.loop)
        self.voice_states[guild_id]['is_speaking'] = False

    def _process_audio(self, sink: discord.sinks.WaveSink, guild_id: int):
        """Processes the recorded audio, transcribes it, and gets an AI response."""
        voice_state = self.voice_states.get(guild_id)
        if not voice_state or voice_state['is_speaking']:
            return

        for user_id, audio in sink.audio_data.items():
            try:
                audio_data = sr.AudioData(audio.file.read(), sink.encoding.sample_rate, 2)
                text = self.recognizer.recognize_google(audio_data)
                logger.info(f"User {user_id} said: {text}")

                if text:
                    # Send text to Gemini and get response
                    chat = self._get_or_create_conversation(guild_id)
                    response = chat.send_message(text)
                    
                    # Speak the response
                    asyncio.run_coroutine_threadsafe(
                        self._speak(voice_state['voice_client'], response.text),
                        self.bot.loop
                    )

            except sr.UnknownValueError:
                logger.info("Google Speech Recognition could not understand audio")
            except sr.RequestError as e:
                logger.error(f"Could not request results from Google Speech Recognition service; {e}")
            except Exception as e:
                logger.error(f"Error processing audio for user {user_id}: {e}")
    
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
            'is_speaking': False,
            'is_listening': True,
            'voice_client': ctx.voice_client
        }
        
        await self._speak(ctx.voice_client, "Hi there! I'm listening.")
        
        # Start the listening loop
        while self.voice_states.get(ctx.guild.id, {}).get('is_listening'):
            # Small sleep to prevent a tight loop
            await asyncio.sleep(0.1)
            
            vc = self.voice_states.get(ctx.guild.id, {}).get('voice_client')
            if not vc or not vc.is_connected() or self.voice_states[ctx.guild.id]['is_speaking']:
                continue

            sink = discord.sinks.WaveSink()
            try:
                # Listen for up to 5 seconds of audio
                vc.listen(sink, after=lambda sink, guild_id=ctx.guild.id: self._process_audio(sink, guild_id), timeout=5.0)
            except Exception as e:
                logger.error(f"Listening failed: {e}")
            
            # Wait for the listen timeout to complete before looping again
            await asyncio.sleep(5.0)

    @commands.command(name="leavechat")
    async def leavechat(self, ctx: commands.Context):
        """Leaves the voice channel and ends the conversation."""
        if not ctx.voice_client:
            await ctx.send("i'm not in a voice channel!")
            return

        if ctx.guild.id in self.voice_states:
            self.voice_states[ctx.guild.id]['is_listening'] = False
            
        await ctx.voice_client.disconnect()
        
        if ctx.guild.id in self.conversations:
            del self.conversations[ctx.guild.id]
        if ctx.guild.id in self.voice_states:
            del self.voice_states[ctx.guild.id]
            
        await ctx.send("okay, talk to you later!")

    # Cog unload cleanup
    def cog_unload(self):
        for state in self.voice_states.values():
            if state['voice_client']:
                self.bot.loop.create_task(state['voice_client'].disconnect())

async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceInteractionCog(bot))