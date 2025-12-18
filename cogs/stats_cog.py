# cogs/stats_cog.py
import discord
from discord.ext import commands, tasks
from datetime import datetime
import functools
import asyncio
from utils.db import db, stats_collection, live_activity_collection # Fixed imports

class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.update_stats_loop.start()

    def cog_unload(self):
        self.update_stats_loop.cancel()

    async def run_db(self, func, *args, **kwargs):
        """Helper to run synchronous DB calls in a separate thread to avoid blocking."""
        partial_func = functools.partial(func, *args, **kwargs)
        return await self.bot.loop.run_in_executor(None, partial_func)

    @tasks.loop(seconds=60)
    async def update_stats_loop(self):
        try:
            total_guilds = len(self.bot.guilds)
            total_users = sum(g.member_count for g in self.bot.guilds)
            
            await self.run_db(stats_collection.update_one, {"_id": "global"}, {"$set": {"total_guilds": total_guilds, "total_users": total_users}}, upsert=True)
            
            count = await self.run_db(live_activity_collection.count_documents, {})
            if count > 50:
                oldest = await self.run_db(lambda: list(live_activity_collection.find().sort("timestamp", 1).limit(count - 50)))
                if oldest:
                    ids = [x["_id"] for x in oldest]
                    await self.run_db(live_activity_collection.delete_many, {"_id": {"$in": ids}})
        except Exception as e:
            print(f"Stats Loop Error: {e}")

    @update_stats_loop.before_loop
    async def before_update_stats_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        timestamp = datetime.utcnow()

        await self.run_db(stats_collection.update_one, {"_id": "global"}, {"$inc": {"total_messages": 1}}, upsert=True)

        if message.guild:
            await self.run_db(stats_collection.update_one, {"_id": f"guild_{message.guild.id}"}, {"$inc": {"messages": 1}, "$set": {"name": message.guild.name, "last_active": timestamp}}, upsert=True)

        await self.run_db(stats_collection.update_one, {"_id": f"user_{message.author.id}"}, {"$inc": {"messages": 1}, "$set": {"name": message.author.name, "display_name": message.author.display_name, "last_active": timestamp}}, upsert=True)

        await self.run_db(live_activity_collection.insert_one, {"user": message.author.name, "guild": message.guild.name if message.guild else "DM", "action": "Sent a message", "timestamp": timestamp})

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction, command):
        timestamp = datetime.utcnow()
        await self.run_db(stats_collection.update_one, {"_id": f"cmd_{command.name}"}, {"$inc": {"usage_count": 1}, "$set": {"name": command.name, "last_used": timestamp}}, upsert=True)
        await self.run_db(stats_collection.update_one, {"_id": "global"}, {"$inc": {"total_commands": 1}}, upsert=True)
        await self.run_db(live_activity_collection.insert_one, {"user": interaction.user.name, "guild": interaction.guild.name if interaction.guild else "DM", "action": f"Used /{command.name}", "timestamp": timestamp})

async def setup(bot):
    await bot.add_cog(StatsCog(bot))