# cogs/admin_cog.py
import discord
from discord import app_commands
from discord.ext import commands
from utils.db import rpg_world_state_collection

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

    # --- EMERGENCY FIXES ---
    @app_commands.command(name="fix_bloat", description="[Admin] Fix RPG Lag: Reset all non-companion NPCs to background status.")
    @app_commands.checks.has_permissions(administrator=True)
    async def fix_bloat(self, interaction: discord.Interaction):
        """
        Emergency command to cure 'Stuck on Typing' lag.
        It forces all NPCs in the current thread to 'background' status unless they are marked as 'companion' or 'party'.
        """
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("⚠️ Please run this command inside the laggy Adventure Thread.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        try:
            # MongoDB Update: Set status='background' for all NPCs where role does NOT contain 'companion' or 'party'
            result = rpg_world_state_collection.update_many(
                {"thread_id": interaction.channel_id},
                {"$set": {"npcs.$[elem].status": "background"}},
                array_filters=[{"elem.attributes.role": {"$not": {"$regex": "companion|party", "$options": "i"}}}]
            )
            
            if result.modified_count > 0:
                msg = f"✅ **Success:** Reset {result.modified_count} NPCs to background status.\n📉 **Context Bloat:** Reduced.\n🚀 **Next Turn:** Should be much faster."
            else:
                msg = "ℹ️ **No changes made.** Population was already optimized or no Matching NPCs found."
                
            await interaction.followup.send(msg, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ **Fix Failed:** {str(e)}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))