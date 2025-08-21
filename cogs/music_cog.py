# cogs/music_cog.py
import discord
from discord.ext import commands
from discord import app_commands
import logging
import os
import asyncio
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from pytube import Search

logger = logging.getLogger(__name__)

# --- FFmpeg Configuration ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queues = {}  # {guild_id: [song_info, ...]}

        # --- Spotify API Setup ---
        try:
            self.spotify = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=os.environ["SPOTIPY_CLIENT_ID"],
                    client_secret=os.environ["SPOTIPY_CLIENT_SECRET"]
                )
            )
        except Exception as e:
            logger.error(f"Failed to initialize Spotify client: {e}")
            self.spotify = None

    def _get_spotify_info(self, query: str):
        """Searches Spotify for a song and returns its title and artist."""
        if not self.spotify:
            return None

        try:
            if "spotify.com/track/" in query:
                result = self.spotify.track(query)
            else:
                results = self.spotify.search(q=query, type='track', limit=1)
                if not results['tracks']['items']:
                    return None
                result = results['tracks']['items'][0]

            title = result['name']
            artist = result['artists'][0]['name']
            return {"title": title, "artist": artist}

        except Exception as e:
            logger.error(f"Error searching on Spotify: {e}")
            return None

    async def _play_next(self, interaction: discord.Interaction):
        """Plays the next song in the queue."""
        guild_id = interaction.guild.id
        if guild_id in self.queues and self.queues[guild_id]:
            song_info = self.queues[guild_id][0]

            try:
                # Search on YouTube using Pytube
                search_query = f"{song_info['title']} {song_info['artist']} lyrics"
                s = Search(search_query)
                yt_result = s.results[0] # Get the first result
                audio_stream = yt_result.streams.get_audio_only()
                
                if not audio_stream:
                    await interaction.channel.send("i couldn't find a streamable audio for that song, sorry! skipping...")
                    self.bot.loop.create_task(self._song_finished(interaction))
                    return

                stream_url = audio_stream.url

                voice_client = interaction.guild.voice_client
                if voice_client and voice_client.is_connected():
                    voice_client.play(
                        discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS),
                        after=lambda e: self.bot.loop.create_task(self._song_finished(interaction))
                    )
                    embed = discord.Embed(
                        title="Now Playing",
                        description=f"**{song_info['title']}** by **{song_info['artist']}**",
                        color=discord.Color.green()
                    )
                    await interaction.channel.send(embed=embed)

            except Exception as e:
                logger.error(f"Error playing song with pytube: {e}")
                await interaction.channel.send("i had a little trouble playing that song, sorry! TvT")
                self.bot.loop.create_task(self._song_finished(interaction)) # Move to next song
        else:
            await interaction.channel.send("the queue is empty now! i'll just chill in vc until you add more songs <3")


    async def _song_finished(self, interaction: discord.Interaction):
        """Callback for when a song finishes playing."""
        guild_id = interaction.guild.id
        if guild_id in self.queues and self.queues[guild_id]:
            self.queues[guild_id].pop(0)
            if self.queues[guild_id]:
                await self._play_next(interaction)

    @app_commands.command(name="play", description="Play a song from Spotify.")
    @app_commands.describe(query="The name of the song or a Spotify URL.")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("you need to be in a voice channel for me to play music!", ephemeral=True)
            return

        await interaction.response.defer()

        song_info = self._get_spotify_info(query)
        if not song_info:
            await interaction.followup.send("i couldn't find that song on spotify, sorry! try another one?")
            return

        voice_client = interaction.guild.voice_client
        if not voice_client:
            voice_client = await interaction.user.voice.channel.connect()

        guild_id = interaction.guild.id
        if guild_id not in self.queues:
            self.queues[guild_id] = []

        self.queues[guild_id].append(song_info)
        embed = discord.Embed(
            title="Added to Queue",
            description=f"**{song_info['title']}** by **{song_info['artist']}**",
            color=discord.Color.purple()
        )
        await interaction.followup.send(embed=embed)

        if not voice_client.is_playing():
            await self._play_next(interaction)

    @app_commands.command(name="skip", description="Skips the current song.")
    async def skip(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()
            await interaction.response.send_message("okay, skipping to the next song!")
            # The `after` callback in play will handle the next song
        else:
            await interaction.response.send_message("i'm not playing anything right now!", ephemeral=True)

    @app_commands.command(name="stop", description="Stops the music and clears the queue.")
    async def stop(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        voice_client = interaction.guild.voice_client
        if voice_client:
            if guild_id in self.queues:
                self.queues[guild_id].clear()
            if voice_client.is_playing():
                voice_client.stop()
            await voice_client.disconnect()
            await interaction.response.send_message("music stopped and i've left the vc! let me know if you need anything else :D")
        else:
            await interaction.response.send_message("i'm not in a voice channel!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))