import discord
from discord import app_commands
from discord.ext import commands
import logging
from utils.danbooru_api import get_random_danbooru_image, danbooru_tag_autocomplete

logger = logging.getLogger(__name__)

# Define the autocomplete function for the command
async def anime_tag_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    data = await danbooru_tag_autocomplete(current)
    return [
        app_commands.Choice(name=tag["name"], value=tag["name"])
        for tag in data
        if not tag["name"].startswith("rating:")
    ]

# Define a Discord UI view with button
class AnotherOneButton(discord.ui.View):
    def __init__(self, tags: str, nsfw: bool = False, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.tags = tags
        self.nsfw = nsfw

    @discord.ui.button(label="🔁 Another One!", style=discord.ButtonStyle.primary)
    async def another_one(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            result = await get_random_danbooru_image(self.tags, nsfw=self.nsfw)
            if not result:
                await interaction.followup.send(f"No more results found for `{self.tags}`.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Here's your `{self.tags or 'Random'}` image!",
                description=f"**Character**: {result['character']}\n**Artist**: {result['artist']}",
                color=discord.Color.purple()
            )
            embed.set_image(url=result['image_url'])

            if result['source']:
                embed.add_field(name="Source", value=result['source'], inline=False)

            await interaction.followup.send(embed=embed, view=AnotherOneButton(self.tags, self.nsfw))
        except Exception as e:
            logger.error(f"Button error: {e}")
            await interaction.followup.send("Error fetching new image.", ephemeral=True)

class AnimeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="animeimage", description="Fetch a random anime image with artist and character info")
    @app_commands.autocomplete(tags=anime_tag_autocomplete)
    @app_commands.describe(tags="Character or tag to search for (autocomplete enabled)")
    async def animeimage(self, interaction: discord.Interaction, tags: str = None):
        try:
            await interaction.response.defer()

            result = await get_random_danbooru_image(tags)
            if not result:
                await interaction.followup.send(f"No results found for `{tags}`.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Here's your `{tags or 'Random'}` image!",
                description=f"**Character**: {result['character']}\n**Artist**: {result['artist']}\n**Tag used**: `{result['actual_tag']}`",
                color=discord.Color.purple()
            )
            embed.set_image(url=result['image_url'])

            if result['source']:
                embed.add_field(name="Source", value=result['source'], inline=False)

            view = AnotherOneButton(tags=result['actual_tag'])
            await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            logger.error(f"Command error: {e}")
            await interaction.followup.send("Oops! Something unexpected went wrong.", ephemeral=True)

# This setup function is required for the cog to be loaded
async def setup(bot: commands.Bot):
    await bot.add_cog(AnimeCog(bot))