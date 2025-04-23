
import discord
from discord import app_commands
import requests
import os
from flask import Flask
from threading import Thread
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Your bot token here
TOKEN = os.environ["TOKEN"]

# Define the bot client
class AnimeImageBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        try:
            await self.tree.sync()
            logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
            logger.info("------")
        except Exception as e:
            logger.error(f"Error during bot startup: {e}")

client = AnimeImageBot()

# Define slash command
@client.tree.command(name="animeimage", description="Fetch a random anime image")
@app_commands.describe(
    category="Choose a category like 'sfw' or 'nsfw'",
    type="Type of image, e.g., waifu, neko, shinobu, bully, etc."
)
async def animeimage(interaction: discord.Interaction, category: str = "sfw", type: str = "waifu"):
    try:
        url = f"https://api.waifu.pics/{category}/{type}"
        response = requests.get(url)
        response.raise_for_status()  # Raise exception for bad status codes
        data = response.json()
        image_url = data["url"]

        embed = discord.Embed(title=f"Here's your random {type}!", color=discord.Color.purple())
        embed.set_image(url=image_url)

        await interaction.response.send_message(embed=embed)
    except requests.RequestException as e:
        logger.error(f"API request error: {e}")
        error_message = f"Error: Failed to fetch image from the API. Status code: {e.response.status_code if hasattr(e, 'response') else 'N/A'}"
        await interaction.response.send_message(error_message)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await interaction.response.send_message("Oops! Something unexpected went wrong.")

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    try:
        app.run(host='0.0.0.0', port=5000)
    except Exception as e:
        logger.error(f"Flask server error: {e}")

def main():
    try:
        Thread(target=run).start()
        client.run(TOKEN)
    except Exception as e:
        logger.error(f"Main execution error: {e}")

if __name__ == "__main__":
    main()
