import discord
from discord import app_commands
from discord.ext import commands
import logging
import json
import os

logger = logging.getLogger(__name__)
JSON_FILE = "join_roles.json"

class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.join_role_map = self._load_data()
        logger.info("WelcomeCog loaded. Join roles data loaded.")

    # --- Data Persistence ---
    def _load_data(self):
        """Loads the guild-to-role mapping from a JSON file."""
        if os.path.exists(JSON_FILE):
            with open(JSON_FILE, "r") as f:
                return json.load(f)
        return {}

    def _save_data(self):
        """Saves the guild-to-role mapping to a JSON file."""
        with open(JSON_FILE, "w") as f:
            json.dump(self.join_role_map, f, indent=4)

    # --- Commands ---
    @app_commands.command(name="setjoinrole", description="Set the role to automatically give to new members.")
    @app_commands.describe(role="The role to assign to new members")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def setjoinrole(self, interaction: discord.Interaction, role: discord.Role):
        """Sets the autorole for the server."""
        guild_id = str(interaction.guild.id)
        self.join_role_map[guild_id] = role.id
        self._save_data()
        
        await interaction.response.send_message(
            f"✅ Success! New members will now automatically receive the **{role.name}** role.",
            ephemeral=True
        )

    @app_commands.command(name="clearjoinrole", description="Stop automatically giving a role to new members.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def clearjoinrole(self, interaction: discord.Interaction):
        """Clears the autorole for the server."""
        guild_id = str(interaction.guild.id)
        if guild_id in self.join_role_map:
            del self.join_role_map[guild_id]
            self._save_data()
            await interaction.response.send_message(
                f"🗑️ The autorole setting has been cleared. New members will no longer get a role automatically.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "ℹ️ No autorole is currently set up for this server.",
                ephemeral=True
            )

    # --- Event Listener ---
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handles the event when a new member joins a guild."""
        # Ignore bots
        if member.bot:
            return

        guild_id = str(member.guild.id)
        
        # Check if a join role is configured for this server
        if guild_id in self.join_role_map:
            role_id = self.join_role_map[guild_id]
            role = member.guild.get_role(role_id)

            if role:
                try:
                    await member.add_roles(role, reason="Auto-assigned on join")
                    logger.info(f"Assigned role '{role.name}' to new member '{member.name}' in guild '{member.guild.name}'.")
                except discord.Forbidden:
                    logger.error(f"Failed to assign role to {member.name} in {member.guild.name}. Missing Permissions.")
                except Exception as e:
                    logger.error(f"An unexpected error occurred while assigning a role: {e}")

# This setup function is required for the cog to be loaded
async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))