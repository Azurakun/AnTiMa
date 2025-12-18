# cogs/configuration_cog.py
import discord
from discord import app_commands
from discord.ext import commands
from utils.db import ai_config_collection
from utils.limiter import limiter
from datetime import datetime, timedelta, timezone
import random

CREATOR_ID = 123456789012345678 # REPLACE WITH YOUR ID

class ConfigurationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Renamed to avoid conflicts
    configuration_group = app_commands.Group(name="config", description="⚙️ Master settings for AnTiMa.")

    @configuration_group.command(name="chat", description="Set the channel and frequency for AI proactive chatting.")
    @app_commands.describe(
        channel="Channel for AI messages.",
        frequency="How often the AI speaks."
    )
    @app_commands.choices(frequency=[
        app_commands.Choice(name="Active (30m - 90m)", value="active"),
        app_commands.Choice(name="Normal (2h - 5h)", value="normal"),
        app_commands.Choice(name="Quiet (6h - 12h)", value="quiet"),
        app_commands.Choice(name="Testing (1m - 2m)", value="testing")
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_chat(self, interaction: discord.Interaction, channel: discord.TextChannel, frequency: str = "normal"):
        await interaction.response.defer(ephemeral=True)
        
        now = datetime.now(timezone.utc)
        if frequency == "active": minutes = random.randint(30, 90)
        elif frequency == "quiet": minutes = random.randint(360, 720)
        elif frequency == "testing": minutes = random.randint(1, 2)
        else: minutes = random.randint(120, 300)
        
        update_data = {
            "channel": channel.id,
            "chat_frequency": frequency,
            "next_chat_time": now + timedelta(minutes=minutes),
            "bot_disabled": False,
            "group_chat_enabled": True
        }
        
        ai_config_collection.update_one({"_id": str(interaction.guild_id)}, {"$set": update_data}, upsert=True)
        await interaction.followup.send(f"✅ **Chat Configured:** {channel.mention} ({frequency}).")

    @configuration_group.command(name="bot", description="Turn the bot On or Off for this server.")
    @app_commands.choices(status=[
        app_commands.Choice(name="ON (Enable)", value=1),
        app_commands.Choice(name="OFF (Disable)", value=0)
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_bot(self, interaction: discord.Interaction, status: int):
        disabled = False if status == 1 else True
        ai_config_collection.update_one({"_id": str(interaction.guild_id)}, {"$set": {"bot_disabled": disabled}}, upsert=True)
        state_str = "Enabled" if status == 1 else "Disabled"
        await interaction.response.send_message(f"✅ AnTiMa is now **{state_str}**.", ephemeral=True)

    @configuration_group.command(name="rpg", description="Set the channel for RPG Adventures.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_rpg(self, interaction: discord.Interaction, channel: discord.TextChannel):
        ai_config_collection.update_one({"_id": str(interaction.guild_id)}, {"$set": {"rpg_channel_id": channel.id}}, upsert=True)
        await interaction.response.send_message(f"✅ **RPG Channel Set:** {channel.mention}", ephemeral=True)

    @configuration_group.command(name="group", description="Allow AI to reply to group conversations?")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Allow Group Replies", value=1),
        app_commands.Choice(name="Direct Mentions Only", value=0)
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_group(self, interaction: discord.Interaction, mode: int):
        enabled = True if mode == 1 else False
        ai_config_collection.update_one({"_id": str(interaction.guild.id)}, {"$set": {"group_chat_enabled": enabled}}, upsert=True)
        await interaction.response.send_message(f"✅ Group Replies: **{'Allowed' if enabled else 'Blocked'}**.", ephemeral=True)

    @configuration_group.command(name="limits", description="[Creator Only] Set Rate Limits.")
    @app_commands.choices(
        scope=[app_commands.Choice(name="User", value="user"), app_commands.Choice(name="Server", value="server")],
        feature=[app_commands.Choice(name="Chat", value="antima_gen"), app_commands.Choice(name="RPG", value="rpg_gen")]
    )
    async def config_limits(self, interaction: discord.Interaction, scope: str, target_id: str, feature: str, limit: int):
        if interaction.user.id != CREATOR_ID:
            return await interaction.response.send_message("❌ Creator only.", ephemeral=True)
        if not target_id.isdigit():
            return await interaction.response.send_message("❌ ID must be numeric.", ephemeral=True)
        
        limiter.set_override(target_id, scope, feature, limit)
        await interaction.response.send_message(f"✅ Limit Set: {scope} {target_id} -> {limit} reqs/min for {feature}.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigurationCog(bot))