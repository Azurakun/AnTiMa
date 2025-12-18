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

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

class AnTiMaBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        initial_extensions = [
            'cogs.configuration_cog',  # Settings
            'cogs.ai_chat.cog',        # AI Chat
            'cogs.rpg_system',         # RPG System (Folder)
            'cogs.stats_cog',          # Dashboard Stats
            'cogs.admin_cog',          # Moderation
            'cogs.welcome_cog',
            'cogs.reminders_cog',
            'cogs.logging_cog',
            'cogs.reaction_roles_cog',
            'cogs.anime_cog'
        ]

        for ext in initial_extensions:
            try:
                await self.load_extension(ext)
                logger.info(f"✅ Loaded extension: {ext}")
            except Exception as e:
                logger.error(f"❌ Failed to load extension {ext}: {e}")

        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash commands.")
        except Exception as e:
            logger.error(f"Failed to sync slash commands: {e}")

    async def on_ready(self):
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logger.info('------')

bot = AnTiMaBot()

async def run_bot():
    init_db()
    
    print("🌐 Launching Dashboard...")
    dashboard_process = subprocess.Popen([sys.executable, "dashboard.py"])
    
    try:
        async with bot:
            await bot.start(os.getenv("DISCORD_TOKEN"))
    except KeyboardInterrupt:
        pass
    finally:
        print("\n🛑 Shutting down...")
        if dashboard_process.poll() is None:
            dashboard_process.terminate()
            try:
                dashboard_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dashboard_process.kill()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass