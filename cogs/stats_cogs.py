# cogs/stats_cog.py
import discord
from discord.ext import commands, tasks
from datetime import datetime
from utils.db import ai_config_collection # Assuming you have a stats collection, if not, we create one below
import pymongo

# We need a new collection for stats specifically
from utils.db import db # Access the raw database object to create a new collection
stats_collection = db["bot_stats"]
live_activity_collection = db["live_activity"] # For the "Live Feed"

class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.update_stats_loop.start()

    @tasks.loop(seconds=30)
    async def update_stats_loop(self):
        """Periodically syncs aggregate stats (like total guilds)"""
        await stats_collection.update_one(
            {"_id": "global"},
            {"$set": {"total_guilds": len(self.bot.guilds), "total_users": sum(g.member_count for g in self.bot.guilds)}},
            upsert=True
        )

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return

        # 1. Update Global & Per-Guild Message Counts
        timestamp = datetime.utcnow()
        
        # Increment Global Counter
        await stats_collection.update_one(
            {"_id": "global"},
            {"$inc": {"total_messages": 1}},
            upsert=True
        )

        # Increment Guild Counter
        if message.guild:
            await stats_collection.update_one(
                {"_id": f"guild_{message.guild.id}"},
                {
                    "$inc": {"messages": 1},
                    "$set": {"name": message.guild.name, "last_active": timestamp}
                },
                upsert=True
            )

        # Increment User Counter
        await stats_collection.update_one(
            {"_id": f"user_{message.author.id}"},
            {
                "$inc": {"messages": 1},
                "$set": {
                    "name": message.author.name, 
                    "avatar": str(message.author.avatar.url) if message.author.avatar else None,
                    "last_active": timestamp
                }
            },
            upsert=True
        )

        # 2. Push to Live Activity Feed (Capped at last 20 entries)
        # This is what makes the dashboard look "Live"
        activity_entry = {
            "user": message.author.name,
            "guild": message.guild.name if message.guild else "DM",
            "action": "Sent a message",
            "timestamp": timestamp
        }
        
        await live_activity_collection.insert_one(activity_entry)
        
        # Cleanup old logs (Keep only last 50 for performance)
        # In production, you might want to archive these instead of deleting
        count = await live_activity_collection.count_documents({})
        if count > 50:
            oldest = await live_activity_collection.find().sort("timestamp", 1).limit(count - 50).to_list(None)
            if oldest:
                await live_activity_collection.delete_many({"_id": {"$in": [x["_id"] for x in oldest]}})

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction, command):
        """Track command usage"""
        await stats_collection.update_one(
            {"_id": "global"},
            {"$inc": {"total_commands": 1}},
            upsert=True
        )
        
        await live_activity_collection.insert_one({
            "user": interaction.user.name,
            "guild": interaction.guild.name if interaction.guild else "DM",
            "action": f"Used command /{command.name}",
            "timestamp": datetime.utcnow()
        })

async def setup(bot):
    await bot.add_cog(StatsCog(bot))