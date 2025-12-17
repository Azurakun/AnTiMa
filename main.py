# main.py
import discord
from discord.ext import commands
import os
import asyncio
import logging
from dotenv import load_dotenv
from utils.db import init_db
import subprocess
import sys

# Load .env
load_dotenv()

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

class AnTiMaBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        extensions = [
            'cogs.ai_chat.cog',
            'cogs.rpg_adventure_cog',
            'cogs.stats_cog',          # Ensure this matches file name
            'cogs.admin_cog',
            'cogs.welcome_cog',
            'cogs.reminders_cog',
            'cogs.logging_cog',
            'cogs.reaction_roles_cog',
            'cogs.anime_cog'
        ]
        for ext in extensions:
            try:
                await self.load_extension(ext)
                logger.info(f"✅ Loaded: {ext}")
            except Exception as e:
                logger.error(f"❌ Failed to load {ext}: {e}")

        await self.tree.sync()

    async def on_ready(self):
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')

bot = AnTiMaBot()

async def run_bot():
    init_db()
    
    # Dashboard Launcher
    print("🌐 Launching Dashboard...")
    dashboard_process = subprocess.Popen([sys.executable, "dashboard.py"])
    
    try:
        async with bot:
            await bot.start(os.getenv("DISCORD_TOKEN"))
    finally:
        print("🛑 Stopping Dashboard...")
        dashboard_process.kill()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass