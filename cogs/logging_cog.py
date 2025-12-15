# cogs/logging_cog.py
import discord
from discord.ext import commands
import logging
import datetime
from utils.db import logs_collection

class MongoHandler(logging.Handler):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    def emit(self, record):
        try:
            # FIX: Use timezone-aware datetime instead of utcfromtimestamp
            timestamp = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
            
            log_entry = {
                # Format ID as YYYY-MM-DD-HH-M (10-minute buckets)
                "_id": f"{timestamp.strftime('%Y-%m-%d-%H')}-{timestamp.minute // 10}",
                "timestamp": timestamp,
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
                "guild_id": getattr(record, "guild_id", None),
                "user_id": getattr(record, "user_id", None)
            }
            
            # Use update_one with upsert to create or append to the bucket
            logs_collection.update_one(
                {"_id": log_entry["_id"]},
                {
                    "$push": {"logs": log_entry},
                    "$setOnInsert": {"created_at": timestamp}
                },
                upsert=True
            )
        except Exception:
            self.handleError(record)

class LoggingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo_handler = MongoHandler(bot)
        
        # Add the custom handler to the root logger
        logging.getLogger().addHandler(self.mongo_handler)
        
        # Set logging level (INFO captures most things, DEBUG is for verbose output)
        logging.getLogger().setLevel(logging.INFO)

    def cog_unload(self):
        # Remove the handler when the cog is unloaded to prevent duplicates
        logging.getLogger().removeHandler(self.mongo_handler)

async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))