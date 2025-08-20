import discord
from discord import app_commands
from discord.ext import commands
import logging
import asyncio
import yt_dlp

# Suppress noisy youtube_dl errors
yt_dlp.utils.bug_reports_message = lambda: ''

logger = logging.getLogger(__name__)

# --- FFMPEG options ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# --- YTDL options ---
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
    'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
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
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)


class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues = {} # {guild_id: [song_queue]}

    def get_queue(self, guild_id: int):
        return self.queues.setdefault(guild_id, [])

    async def play_next(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)
        
        if not queue:
            # If the queue is empty, we can schedule the bot to leave after some inactivity
            # For now, we'll just stop.
            return

        # Pop the next song
        source = queue.pop(0)
        
        if interaction.guild.voice_client and interaction.guild.voice_client.is_connected():
            interaction.guild.voice_client.play(
                source, after=lambda e: self.bot.loop.create_task(self.play_next(interaction))
            )
            await interaction.channel.send(f'🎶 Now playing: **{source.title}**')
        else:
            logger.warning(f"Voice client disconnected unexpectedly in guild {guild_id}")


    @app_commands.command(name="join", description="Joins your current voice channel.")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("❌ You are not connected to a voice channel.", ephemeral=True)
            return

        channel = interaction.user.voice.channel
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        
        await interaction.response.send_message(f"👋 Joined **{channel.name}**!")

    @app_commands.command(name="leave", description="Leaves the current voice channel.")
    async def leave(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
            return

        self.queues.pop(interaction.guild.id, None) # Clear queue on leave
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Left the voice channel.")

    @app_commands.command(name="play", description="Plays a song from YouTube or adds it to the queue.")
    @app_commands.describe(query="The URL or search query for the song you want to play.")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        # Join the user's channel if not already in one
        if not interaction.guild.voice_client:
            if interaction.user.voice:
                await interaction.user.voice.channel.connect()
            else:
                await interaction.followup.send("❌ You need to be in a voice channel for me to play music.")
                return

        try:
            player = await YTDLSource.from_url(query, loop=self.bot.loop, stream=True)
            queue = self.get_queue(interaction.guild.id)
            queue.append(player)
            
            if not interaction.guild.voice_client.is_playing():
                await interaction.followup.send(f"✅ Added **{player.title}** to the queue. Starting playback!")
                await self.play_next(interaction)
            else:
                await interaction.followup.send(f"✅ Added **{player.title}** to the queue.")

        except Exception as e:
            logger.error(f"Error in play command: {e}")
            await interaction.followup.send("😥 Something went wrong. I couldn't play that song.")
            
    @app_commands.command(name="skip", description="Skips the current song.")
    async def skip(self, interaction: discord.Interaction):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            interaction.guild.voice_client.stop()
            await interaction.response.send_message("⏭️ Skipped!")
            # The `after` callback in play_next will handle playing the next song
        else:
            await interaction.response.send_message("❌ I'm not playing anything right now.", ephemeral=True)
            
    @app_commands.command(name="stop", description="Stops the music and clears the queue.")
    async def stop(self, interaction: discord.Interaction):
        if interaction.guild.voice_client:
            self.queues.pop(interaction.guild.id, None) # Clear the queue
            interaction.guild.voice_client.stop()
            await interaction.response.send_message("⏹️ Music stopped and queue cleared.")
        else:
            await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
            
    @app_commands.command(name="queue", description="Shows the current song queue.")
    async def queue(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild.id)
        
        if not queue:
            await interaction.response.send_message("ℹ️ The queue is currently empty.", ephemeral=True)
            return
            
        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.purple())
        
        # Add currently playing song if any
        current_song = interaction.guild.voice_client.source if interaction.guild.voice_client and interaction.guild.voice_client.is_playing() else None
        if current_song:
             embed.add_field(name="Now Playing", value=f"**{current_song.title}**", inline=False)
        
        if queue:
            queue_text = "\n".join([f"{i+1}. {song.title}" for i, song in enumerate(queue[:10])])
            embed.add_field(name="Up Next", value=queue_text, inline=False)
        
        if len(queue) > 10:
            embed.set_footer(text=f"...and {len(queue) - 10} more.")
            
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))