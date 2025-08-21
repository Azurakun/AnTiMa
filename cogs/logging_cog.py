# cogs/logging_cog.py
import logging
from logging import Handler, LogRecord
import pymongo
from utils.db import logs_collection # Assuming logs_collection is defined in your db utility

class MongoHandler(Handler):
    """A logging handler that writes logs to a MongoDB collection."""

    def __init__(self, collection, level=logging.NOTSET):
        super().__init__(level)
        self.collection = collection

    def emit(self, record: LogRecord):
        """Emit a log record."""
        try:
            self.collection.insert_one({
                'timestamp': record.created,
                'level': record.levelname,
                'message': self.format(record),
                'module': record.module,
                'funcName': record.funcName,
                'lineNo': record.lineno
            })
        except Exception as e:
            print(f"Failed to log to MongoDB: {e}")

class LoggingCog(commands.Cog, name="Logging"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.setup_logging()

    def setup_logging(self):
        """Sets up the MongoDB logging handler."""
        # Get the root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)  # Set the minimum level of logs to capture

        # Add the MongoHandler
        mongo_handler = MongoHandler(collection=logs_collection)
        root_logger.addHandler(mongo_handler)

        logger.info("Logging to MongoDB has been configured.")

async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))