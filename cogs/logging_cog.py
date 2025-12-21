# cogs/logging_cog.py
import discord
from discord.ext import commands
import logging
import datetime
import sys
import re
from utils.db import logs_collection

class MongoHandler(logging.Handler):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        # Pre-compile regex for performance
        self.ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def emit(self, record):
        try:
            timestamp = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
            msg = self.format(record)
            
            # Clean ANSI colors (keep logs clean in DB)
            clean_msg = self.ansi_escape.sub('', msg)

            log_entry = {
                "_id": f"{timestamp.strftime('%Y-%m-%d-%H')}-{timestamp.minute // 10}",
                "timestamp": timestamp,
                "level": record.levelname,
                "logger": record.name,
                "message": clean_msg,
                "guild_id": getattr(record, "guild_id", None),
                "user_id": getattr(record, "user_id", None)
            }
            
            # Upsert into 10-minute buckets
            logs_collection.update_one(
                {"_id": log_entry["_id"]},
                {"$push": {"logs": log_entry}, "$setOnInsert": {"created_at": timestamp}},
                upsert=True
            )
        except Exception:
            self.handleError(record)

class StreamToLogger(object):
    """
    Redirects writes to a logger instance AND the original stream.
    This prevents recursion by ensuring the logger used here doesn't write to this stream.
    """
    def __init__(self, logger, level, original_stream):
        self.logger = logger
        self.level = level
        self.original_stream = original_stream

    def write(self, buf):
        # 1. Write to the REAL terminal (so you can see it)
        try:
            self.original_stream.write(buf)
            self.original_stream.flush()
        except Exception: pass

        # 2. Log to Database (via Logger)
        # We strip whitespace to avoid logging empty newlines as separate entries
        for line in buf.rstrip().splitlines():
            if line.strip(): 
                self.logger.log(self.level, line.rstrip())

    def flush(self):
        self.original_stream.flush()

class LoggingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo_handler = MongoHandler(bot)
        
        # 1. Configure Root Logger (for general logging)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        # We add the mongo handler to root so normal logging.info() calls work
        root_logger.addHandler(self.mongo_handler)

        # 2. Setup stdout/stderr redirection
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        
        # Create dedicated loggers that DO NOT propagate to root
        # This is CRITICAL to prevent RecursionError (Logger -> stdout -> Logger...)
        
        self.log_stdout = logging.getLogger("STDOUT")
        self.log_stdout.addHandler(self.mongo_handler)
        self.log_stdout.propagate = False 

        self.log_stderr = logging.getLogger("STDERR")
        self.log_stderr.addHandler(self.mongo_handler)
        self.log_stderr.propagate = False

        # Redirect streams
        sys.stdout = StreamToLogger(self.log_stdout, logging.INFO, self.original_stdout)
        sys.stderr = StreamToLogger(self.log_stderr, logging.ERROR, self.original_stderr)
        
        print("✅ Logging System Online: Terminal output is being mirrored to Dashboard.")

    def cog_unload(self):
        # Restore original streams safely
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
        logging.getLogger().removeHandler(self.mongo_handler)

async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))