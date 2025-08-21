# cogs/music_cog.py
import discord
from discord.ext import commands
from discord import app_commands
import logging
import asyncio
import yt_dlp
import functools
import traceback

logger = logging.getLogger(__name__)

# --- yt-dlp Configuration ---
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
    'source_address': '0.0.0.0'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        try:
            # Use functools.partial to run the synchronous ytdl code in an executor
            partial_extract = functools.partial(ytdl.extract_info, url, download=not stream)
            data = await loop.run_in_executor(None, partial_extract)

            if 'entries' in data:
                # take first item from a playlist
                data = data['entries'][0]

            filename = data['url'] if stream else ytdl.prepare_filename(data)
            return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)
        except Exception:
            logger.error(f"Error in YTDLSource.from_url for url: {url}")
            logger.error(traceback.format_exc())
            raise


class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues = {}  # {guild_id: [song_info, ...]}
        self.current_song = {} # {guild_id: song_info}

    def _get_queue(self, guild_id: int):
        """Gets the queue for a guild, creating one if it doesn't exist."""
        if guild_id not in self.queues:
            self.queues[guild_id] = []
        return self.queues[guild_id]

    async def _play_next(self, interaction: discord.Interaction):
        """Plays the next song in the queue."""
        guild_id = interaction.guild.id
        queue = self._get_queue(guild_id)

        if not queue:
            self.current_song.pop(guild_id, None)
            await interaction.channel.send("the queue is empty now! i'll just chill in vc until you add more songs <3")
            return

        song_url = queue.pop(0)
        
        try:
            player = await YTDLSource.from_url(song_url, loop=self.bot.loop, stream=True)
            self.current_song[guild_id] = player
            
            interaction.guild.voice_client.play(
                player, 
                after=lambda e: self.bot.loop.create_task(self._song_finished(interaction))
            )

            embed = discord.Embed(
                title="Now Playing",
                description=f"**[{player.title}]({player.url})**",
                color=discord.Color.green()
            )
            await interaction.channel.send(embed=embed)

        except Exception:
            logger.error("Error playing song with yt-dlp:")
            logger.error(traceback.format_exc())
            await interaction.channel.send("i had a little trouble playing that song, sorry! TvT")
            await self._song_finished(interaction)

    async def _song_finished(self, interaction: discord.Interaction):
        """Callback for when a song finishes playing."""
        guild_id = interaction.guild.id
        if guild_id in self.queues:
            await self._play_next(interaction)

    @app_commands.command(name="play", description="Play a song from YouTube.")
    @app_commands.describe(query="The name of the song or a YouTube URL.")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("you need to be in a voice channel for me to play music!", ephemeral=True)
            return

        await interaction.response.defer()

        voice_client = interaction.guild.voice_client
        if not voice_client:
            voice_client = await interaction.user.voice.channel.connect()

        guild_id = interaction.guild.id
        queue = self._get_queue(guild_id)
        
        try:
            # We add the query directly, YTDLSource will handle the search
            queue.append(query)
            
            embed = discord.Embed(
                title="Added to Queue",
                description=f"**{query}**",
                color=discord.Color.purple()
            )
            await interaction.followup.send(embed=embed)

            if not voice_client.is_playing():
                await self._play_next(interaction)

        except Exception as e:
            logger.error(f"Error adding song to queue: {e}")
            await interaction.followup.send("i couldn't find that song, sorry! try another one?")

    @app_commands.command(name="skip", description="Skips the current song.")
    async def skip(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()
            await interaction.response.send_message("okay, skipping to the next song!")
        else:
            await interaction.response.send_message("i'm not playing anything right now!", ephemeral=True)

    @app_commands.command(name="stop", description="Stops the music and clears the queue.")
    async def stop(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        voice_client = interaction.guild.voice_client
        if voice_client:
            self._get_queue(guild_id).clear()
            if voice_client.is_playing():
                voice_client.stop()
            await voice_client.disconnect()
            await interaction.response.send_message("music stopped and i've left the vc! let me know if you need anything else :D")
        else:
            await interaction.response.send_message("i'm not in a voice channel!", ephemeral=True)

    @app_commands.command(name="queue", description="Shows the current song queue.")
    async def queue(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        queue = self._get_queue(guild_id)
        
        if not self.current_song.get(guild_id) and not queue:
            await interaction.response.send_message("the queue is empty!", ephemeral=True)
            return
            
        embed = discord.Embed(title="Music Queue", color=discord.Color.blue())
        
        if self.current_song.get(guild_id):
            embed.add_field(name="Now Playing", value=f"[{self.current_song[guild_id].title}]({self.current_song[guild_id].url})", inline=False)
        
        if queue:
            queue_text = ""
            for i, song in enumerate(queue):
                queue_text += f"{i+1}. {song}\n"
            embed.add_field(name="Up Next", value=queue_text, inline=False)
            
        await interaction.response.send_message(embed=embed)
        
    @app_commands.command(name="volume", description="Changes the player's volume.")
    @app_commands.describe(value="The volume to set (0-100).")
    async def volume(self, interaction: discord.Interaction, value: int):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.source:
            voice_client.source.volume = value / 100
            await interaction.response.send_message(f"volume set to {value}%")
        else:
            await interaction.response.send_message("i'm not playing anything right now!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))