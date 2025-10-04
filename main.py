import discord
from discord.ext import commands
import os
from flask import Flask
from threading import Thread
import logging
import asyncio
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TOKEN"]

# We use commands.Bot instead of discord.Client to use the cogs extension system
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.reactions = True
        intents.voice_states = True # Required for voice
        # The command_prefix is required but won't be used for slash commands
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("------")

    async def setup_hook(self):
        # This is the best place to load cogs and sync commands
        cogs_folder = "cogs"
        for filename in os.listdir(cogs_folder):
            if filename.endswith(".py"):
                await self.load_extension(f"{cogs_folder}.{filename[:-3]}")
        
        # Sync the commands to Discord
        await self.tree.sync()

client = MyBot()

# --- Flask Web Server for Uptime ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    try:
        app.run(host='0.0.0.0', port=5000)
    except Exception as e:
        logger.error(f"Flask server error: {e}")

# --- Main Execution ---
if __name__ == "__main__":
    # Start Flask in a separate thread
    Thread(target=run_flask).start()
    
    # Run the bot
    try:
        client.run(TOKEN)
    except Exception as e:
        logger.error(f"Main execution error: {e}")