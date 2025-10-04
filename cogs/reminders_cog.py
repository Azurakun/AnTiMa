# cogs/reminders_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
import asyncio
import datetime
import uuid
import re
from zoneinfo import ZoneInfo, available_timezones
from utils.db import reminders_collection, user_timezones_collection # Import the collections

logger = logging.getLogger(__name__)

class RemindersCog(commands.Cog, name="Reminders"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self.initialize_reminders())

    async def initialize_reminders(self):
        """On bot startup, load and schedule all pending reminders from the database."""
        await self.bot.wait_until_ready()
        logger.info("Initializing pending reminders from MongoDB...")
        pending_reminders = list(reminders_collection.find())
        for reminder in pending_reminders:
            await self._schedule_reminder(reminder)
        logger.info(f"Scheduled {len(pending_reminders)} reminders.")

    # --- Core Reminder Logic ---
    async def _schedule_reminder(self, reminder: dict):
        """Creates a background task to wait for and send a reminder."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        remind_time = datetime.datetime.fromisoformat(reminder["remind_time_iso"])
        delay = (remind_time - now_utc).total_seconds()

        if delay > 0:
            self.bot.loop.create_task(self._send_reminder_task(delay, reminder))
        else:
            logger.warning(f"Reminder {reminder['_id']} was in the past. Sending now.")
            self.bot.loop.create_task(self._send_reminder_task(0, reminder))

    async def _send_reminder_task(self, delay: float, reminder: dict):
        """The actual background task that sleeps and then sends the reminder."""
        if delay > 0:
            await asyncio.sleep(delay)

        user = self.bot.get_user(reminder["user_id"])
        if not user:
            logger.error(f"Could not find user {reminder['user_id']}.")
            reminders_collection.delete_one({"_id": reminder["_id"]})
            return
        
        for i in range(reminder["repeat"]):
            try:
                embed = discord.Embed(
                    title="⏰ Reminder!",
                    description=f"**Event:** {reminder['message']}",
                    color=discord.Color.gold()
                )
                embed.set_footer(text=f"This is call {i + 1} of {reminder['repeat']}.")
                await user.send(f"Hey {user.mention}, you asked me to remind you!", embed=embed)
                if i < reminder["repeat"] - 1:
                    await asyncio.sleep(30)
            except discord.Forbidden:
                logger.error(f"Cannot send DM to user {user.name}.")
                break

        reminders_collection.delete_one({"_id": reminder["_id"]})

    # --- Time Parsing Helper (no changes needed) ---
    def _parse_time(self, time_str: str, user_tz: ZoneInfo) -> datetime.datetime | None:
        """Parses a flexible time string into a timezone-aware datetime object."""
        now = datetime.datetime.now(user_tz)
        if re.match(r"^\d+", time_str):
            delta = datetime.timedelta()
            parts = re.findall(r"(\d+)([dhms])", time_str.lower())
            if not parts: return None
            for value, unit in parts:
                if unit == 'd': delta += datetime.timedelta(days=int(value))
                elif unit == 'h': delta += datetime.timedelta(hours=int(value))
                elif unit == 'm': delta += datetime.timedelta(minutes=int(value))
                elif unit == 's': delta += datetime.timedelta(seconds=int(value))
            return now + delta
        try:
            hour, minute = map(int, time_str.split(':'))
            remind_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if remind_time < now:
                remind_time += datetime.timedelta(days=1)
            return remind_time
        except ValueError:
            return None

    # --- Autocomplete (no changes needed) ---
    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        all_timezones = available_timezones()
        if not current:
            # Show some common examples first
            suggestions = ["Asia/Jakarta", "Europe/London", "America/New_York", "America/Los_Angeles", "UTC"]
        else:
            suggestions = [tz for tz in all_timezones if current.lower() in tz.lower()]
        
        return [
            app_commands.Choice(name=tz, value=tz)
            for tz in suggestions[:25]
        ]

    # --- Discord Commands ---
    @app_commands.command(name="settimezone", description="Set your local timezone for reminders.")
    @app_commands.autocomplete(timezone=timezone_autocomplete)
    async def settimezone(self, interaction: discord.Interaction, timezone: str):
        if timezone not in available_timezones():
            await interaction.response.send_message("❌ **Invalid timezone!** Please select one from the list.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        # Update or insert the user's timezone in the database
        user_timezones_collection.update_one(
            {"_id": user_id},
            {"$set": {"timezone": timezone}},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Your timezone has been set to **{timezone}**.", ephemeral=True)

    @app_commands.command(name="remindme", description="Sets a personal reminder in your local time.")
    @app_commands.describe(when="When to be reminded (e.g., '10m', '2h30m', or '16:30').", message="What to be reminded about.", repeat="How many times to notify you. Default is 1.")
    async def remindme(self, interaction: discord.Interaction, when: str, message: str, repeat: int = 1):
        user_id = str(interaction.user.id)
        
        # Get the user's timezone from the database
        user_timezone_data = user_timezones_collection.find_one({"_id": user_id})
        if not user_timezone_data:
            await interaction.response.send_message(
                "️️️⚠️ **Please set your timezone first!** Use the `/settimezone` command before setting a reminder.",
                ephemeral=True
            )
            return

        user_tz_str = user_timezone_data["timezone"]
        user_tz = ZoneInfo(user_tz_str)
        remind_time = self._parse_time(when, user_tz)

        if not remind_time:
            await interaction.response.send_message("❌ **Invalid time format!** Please use a format like `10m`, `2h30m`, or `16:30`.", ephemeral=True)
            return

        reminder = {
            "user_id": interaction.user.id,
            "message": message,
            "remind_time_iso": remind_time.isoformat(),
            "repeat": max(1, min(5, repeat)) # Clamped between 1 and 5
        }

        # Insert the new reminder into the database
        result = reminders_collection.insert_one(reminder)
        # Get the reminder back with its new ID for scheduling
        new_reminder = reminders_collection.find_one({"_id": result.inserted_id})
        await self._schedule_reminder(new_reminder)

        await interaction.response.send_message(
            f"✅ **Reminder set!** I will remind you about `{message}` at {discord.utils.format_dt(remind_time, style='T')} your time.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(RemindersCog(bot))