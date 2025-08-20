import discord
from discord import app_commands
from discord.ext import commands
import logging
import asyncio
import yt_dlp

# Suppress noisy youtube_dl/yt-dlp errors
yt_dlp.utils.bug_reports_message = lambda: ''

logger = logging.getLogger(__name__)

# --- FFMPEG options for streaming ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# --- YTDL options for extracting info ---
YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # bind to ipv4 to avoid issues
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

class YTDLSource(discord.PCMVolumeTransformer):
    """Represents a YTDL audio source, handling the data and FFmpeg stream."""
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url') # Link to the video page
        self.duration = data.get('duration_string')
        self.uploader = data.get('uploader')

class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # We store state per-guild to support multiple servers at once
        # State: {guild_id: {'queue': [song_data, ...], 'text_channel': discord.TextChannel}}
        self.guild_states = {}

    def get_guild_state(self, guild_id: int):
        """Gets or creates the state for a given guild."""
        return self.guild_states.setdefault(guild_id, {'queue': [], 'text_channel': None})

    async def play_next_song(self, guild_id: int):
        """The core loop that plays the next song in the queue."""
        state = self.get_guild_state(guild_id)
        queue = state['queue']
        text_channel = state['text_channel']
        guild = self.bot.get_guild(guild_id)
        
        if not queue:
            if text_channel:
                await text_channel.send("🎵 The queue is now empty.")
            return

        if not guild or not guild.voice_client or not guild.voice_client.is_connected():
            logger.warning(f"Bot is not connected to voice in guild {guild_id}, clearing queue.")
            self.guild_states.pop(guild_id, None)
            return

        # Get the next song's data and create the audio source just-in-time
        data = queue.pop(0)
        try:
            # Use the direct audio stream URL from the data
            source = YTDLSource(discord.FFmpegPCMAudio(data['url'], **FFMPEG_OPTIONS), data=data)
            guild.voice_client.play(source, after=lambda e: self.bot.loop.create_task(self.on_song_end(guild_id, e)))
            
            if text_channel:
                await text_channel.send(f'🎶 Now playing: **{source.title}**')
        except Exception as e:
            logger.error(f"Error playing next song in guild {guild_id}: {e}")
            if text_channel:
                await text_channel.send(f"😥 An error occurred while trying to play **{data.get('title', 'the next song')}**.")
            await self.play_next_song(guild_id) # Try to play the next one

    async def on_song_end(self, guild_id: int, error=None):
        """Callback for when a song finishes playing."""
        if error:
            logger.error(f"Player error in guild {guild_id}: {error}")
        await self.play_next_song(guild_id)

    @app_commands.command(name="join", description="Joins your current voice channel.")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            return await interaction.response.send_message("❌ You are not connected to a voice channel.", ephemeral=True)

        channel = interaction.user.voice.channel
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        
        await interaction.response.send_message(f"👋 Joined **{channel.name}**!")

    @app_commands.command(name="leave", description="Leaves the voice channel and clears the queue.")
    async def leave(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)

        self.guild_states.pop(interaction.guild.id, None)
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Left the voice channel.")

    @app_commands.command(name="play", description="Plays a song or adds it to the queue.")
    @app_commands.describe(query="A search term or URL for the song.")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        if not interaction.guild.voice_client:
            if interaction.user.voice:
                await interaction.user.voice.channel.connect()
            else:
                return await interaction.followup.send("❌ You need to be in a voice channel for me to play music.")

        state = self.get_guild_state(interaction.guild.id)
        state['text_channel'] = interaction.channel

        try:
            loop = self.bot.loop or asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            
            if 'entries' in data:
                data = data['entries'][0]

            state['queue'].append(data)
            
            if not interaction.guild.voice_client.is_playing():
                await interaction.followup.send(f"✅ Added **{data['title']}** to the queue. Starting playback!")
                await self.play_next_song(interaction.guild.id)
            else:
                await interaction.followup.send(f"✅ Added **{data['title']}** to the queue.")

        except Exception as e:
            logger.error(f"Error in play command: {e}")
            await interaction.followup.send("😥 Something went wrong. I couldn't find or play that song.")

    @app_commands.command(name="skip", description="Skips the current song.")
    async def skip(self, interaction: discord.Interaction):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
            await interaction.response.send_message("⏭️ Skipped!")
        else:
            await interaction.response.send_message("❌ I'm not playing anything right now.", ephemeral=True)

    @app_commands.command(name="stop", description="Stops the music and clears the queue.")
    async def stop(self, interaction: discord.Interaction):
        if interaction.guild.voice_client:
            state = self.get_guild_state(interaction.guild.id)
            state['queue'].clear()
            interaction.guild.voice_client.stop()
            await interaction.response.send_message("⏹️ Music stopped and queue cleared.")
        else:
            await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)

    @app_commands.command(name="queue", description="Shows the current song queue.")
    async def queue(self, interaction: discord.Interaction):
        state = self.get_guild_state(interaction.guild.id)
        queue = state['queue']
        
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing() and not queue:
            return await interaction.response.send_message("ℹ️ The queue is empty and nothing is playing.", ephemeral=True)
            
        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.purple())
        
        if vc.source:
            embed.add_field(name="Now Playing", value=f"**[{vc.source.title}]({vc.source.url})**", inline=False)
        
        if queue:
            queue_text = "\n".join([f"{i+1}. {song['title']}" for i, song in enumerate(queue[:10])])
            embed.add_field(name="Up Next", value=queue_text, inline=False)
        
        if len(queue) > 10:
            embed.set_footer(text=f"...and {len(queue) - 10} more.")
            
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))