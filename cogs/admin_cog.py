# cogs/admin_cog.py
import discord
from discord import app_commands
from discord.ext import commands

class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- TOP LEVEL GROUP: /mod ---
    mod_group = app_commands.Group(name="mod", description="🛡️ Moderation Tools")

    @mod_group.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="The user to kick", reason="Reason for kicking")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You cannot kick yourself.", ephemeral=True)
        try:
            await user.kick(reason=reason)
            await interaction.response.send_message(f"✅ **{user}** has been kicked.\n📝 Reason: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I do not have permission to kick this user.", ephemeral=True)

    @mod_group.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(user="The user to ban", reason="Reason for banning", delete_days="Days of messages to delete (0-7)")
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided", delete_days: int = 0):
        if user.id == interaction.user.id:
            return await interaction.response.send_message("❌ You cannot ban yourself.", ephemeral=True)
        try:
            await user.ban(reason=reason, delete_message_days=min(max(delete_days, 0), 7))
            await interaction.response.send_message(f"🔨 **{user}** has been banned.\n📝 Reason: {reason}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I do not have permission to ban this user.", ephemeral=True)

    @mod_group.command(name="purge", description="Delete a number of messages.")
    @app_commands.describe(amount="Number of messages to delete")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)

    # --- OWNER COMMANDS ---
    @app_commands.command(name="listservers", description="[Owner] List all servers the bot is connected to.")
    async def listservers(self, interaction: discord.Interaction):
        # Dynamically checks if the user is the bot owner (set in Developer Portal)
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)

        guilds = self.bot.guilds
        embed = discord.Embed(title=f"📊 Server List ({len(guilds)})", color=discord.Color.gold())
        
        description = ""
        for guild in guilds:
            line = f"• **{guild.name}** (ID: `{guild.id}`) - {guild.member_count} Members\n"
            # Prevent embed limits
            if len(description) + len(line) > 4000:
                description += "... (List truncated due to size)"
                break
            description += line
            
        embed.description = description or "No servers found."
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))