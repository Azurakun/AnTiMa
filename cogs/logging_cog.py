# cogs/logging_cog.py
import logging
from logging import Handler, LogRecord
from datetime import datetime
from discord.ext import commands
from utils.db import logs_collection # This now points to the correct DB connection

logger = logging.getLogger(__name__)

class MongoHandler(Handler):
    """A logging handler that appends logs to a new document in MongoDB every 10 minutes."""

    def __init__(self, collection, level=logging.NOTSET):
        super().__init__(level)
        self.collection = collection

    def emit(self, record: LogRecord):
        """Appends a log record to the document for the current 10-minute interval."""
        try:
            # Use the current date and 10-minute interval as the document ID
            timestamp = datetime.utcfromtimestamp(record.created)
            log_id = f"{timestamp.strftime('%Y-%m-%d-%H')}-{timestamp.minute // 10}"
            
            log_entry = {
                'timestamp': record.created,
                'level': record.levelname,
                'message': self.format(record),
                'module': record.module,
                'funcName': record.funcName,
                'lineNo': record.lineno
            }

            # Find the document for today and push the new log into its 'logs' array.
            # If the document doesn't exist, upsert=True will create it.
            self.collection.update_one(
                {'_id': log_id},
                {'$push': {'logs': log_entry}},
                upsert=True
            )
        except Exception as e:
            # Fallback to console if DB logging fails
            print(f"Failed to log to MongoDB: {e}")
            print(f"Log Record: {self.format(record)}")

class LoggingCog(commands.Cog, name="Logging"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.setup_logging()

    def setup_logging(self):
        """Sets up the MongoDB logging handler."""
        if logs_collection is None:
            logger.error("Logs collection is not available. Cannot set up MongoDB logging.")
            return

        root_logger = logging.getLogger()
        
        # Avoid adding handlers multiple times on reload
        if any(isinstance(h, MongoHandler) for h in root_logger.handlers):
            logger.info("MongoHandler already configured.")
            return

        root_logger.setLevel(logging.INFO)
        mongo_handler = MongoHandler(collection=logs_collection)
        root_logger.addHandler(mongo_handler)

        logger.info("Logging to MongoDB has been configured.")

async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))