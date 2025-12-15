# cogs/admin_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import logging
from utils.db import logs_collection # Import the logs collection
from datetime import datetime, timedelta # Import for date calculations

logger = logging.getLogger(__name__)

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setemojirole", description="Link an emoji with a role on a specific message")
    @app_commands.describe(
        message_id="ID of the message to track reactions on",
        emoji="Emoji to react with",
        role="Role to give when reacted"
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def setemojirole(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        # The reaction_roles_cog will handle the actual logic. This command just sets it up.
        reaction_cog = self.bot.get_cog("ReactionRolesCog")
        if not reaction_cog:
            await interaction.response.send_message("Reaction Roles system is not loaded.", ephemeral=True)
            return

        try:
            msg_id = int(message_id)
        except ValueError:
            await interaction.response.send_message("Invalid message ID format.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        reaction_cog.emoji_role_map.setdefault(guild_id, {}).setdefault(msg_id, {})[emoji] = role.id
        
        await interaction.response.send_message(f"Linked emoji {emoji} with role {role.name} on message ID {msg_id}.", ephemeral=True)

    @app_commands.command(name="msg", description="Send a message to a specific channel")
    @app_commands.describe(
        channel_id="The ID of the channel to send the message to",
        message="The message content",
        mention_user="User to mention (optional)",
        mention_role="Role to mention (optional)",
        embed_title="Embed title (optional)",
        embed_color="Embed color in HEX, e.g. #ff5733 (optional)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def send_message(
        self, interaction: discord.Interaction, channel_id: str, message: str,
        mention_user: discord.User = None, mention_role: discord.Role = None,
        embed_title: str = None, embed_color: str = None
    ):
        try:
            message = message.replace("\\n", "\n")
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                await interaction.response.send_message("Invalid channel ID.", ephemeral=True)
                return

            mention_text = ""
            if mention_user: mention_text += mention_user.mention + " "
            if mention_role: mention_text += mention_role.mention + " "

            if embed_title or embed_color:
                color = discord.Color.default()
                if embed_color:
                    try:
                        color = discord.Color(int(embed_color.lstrip("#"), 16))
                    except ValueError:
                        await interaction.response.send_message("Invalid color format.", ephemeral=True)
                        return
                embed = discord.Embed(title=embed_title or "Message", description=message, color=color)
                await channel.send(content=mention_text or None, embed=embed)
            else:
                await channel.send(content=f"{mention_text}{message}".strip())

            await interaction.response.send_message(f"Message sent to <#{channel_id}>.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            await interaction.response.send_message("Failed to send the message.", ephemeral=True)

    @app_commands.command(name="purgelogs", description="Deletes log data from the database.")
    @app_commands.describe(
        days="Delete logs older than this many days. Leave blank to delete ALL logs.",
        confirm="You must set this to true to confirm the deletion."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def purgelogs(self, interaction: discord.Interaction, confirm: bool, days: int = None):
        """
        Deletes log data. Can delete all data or data older than a specified number of days.
        Requires administrator permissions and explicit confirmation.
        """
        if not confirm:
            await interaction.response.send_message(
                "⚠️ **Confirmation required!** You must set the `confirm` option to `True` to proceed with deleting logs.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if days is None:
                # Delete all documents in the collection
                result = logs_collection.delete_many({})
                message = f"🗑️ **All log data has been deleted.** ({result.deleted_count} documents removed)."
                logger.warning(f"Admin {interaction.user} purged all log data.")
            else:
                # Calculate the cutoff date
                cutoff_date = datetime.utcnow() - timedelta(days=days)
                # The _id is formatted like 'YYYY-MM-DD-HH-M', so we can do a string comparison
                cutoff_id_str = f"{cutoff_date.strftime('%Y-%m-%d-%H')}-{cutoff_date.minute // 10}"
                
                # Delete documents where the _id is less than the cutoff string
                result = logs_collection.delete_many({"_id": {"$lt": cutoff_id_str}})
                message = f"🗑️ **Logs older than {days} days have been deleted.** ({result.deleted_count} documents removed)."
                logger.warning(f"Admin {interaction.user} purged logs older than {days} days.")

            await interaction.followup.send(message)

        except Exception as e:
            logger.error(f"Error purging logs: {e}")
            await interaction.followup.send("❌ An error occurred while trying to purge the logs.")

    @app_commands.command(name="listservers", description="Lists all servers the bot has currently joined.")
    @app_commands.checks.has_permissions(administrator=True)
    async def listservers(self, interaction: discord.Interaction):
        """Lists all joined guilds (servers) and their IDs."""
        await interaction.response.defer(ephemeral=True)
        
        guilds = self.bot.guilds
        if not guilds:
            await interaction.followup.send("I haven't joined any servers yet.")
            return

        # Build the message
        lines = [f"**🤖 Joined Servers ({len(guilds)})**"]
        for guild in guilds:
            lines.append(f"• **{guild.name}** (ID: `{guild.id}`)")
        
        full_text = "\n".join(lines)

        # Discord message limit is 2000 chars. Split if necessary.
        if len(full_text) > 2000:
            chunks = []
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 > 2000:
                    chunks.append(current_chunk)
                    current_chunk = line + "\n"
                else:
                    current_chunk += line + "\n"
            if current_chunk:
                chunks.append(current_chunk)
            
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=True)
        else:
            await interaction.followup.send(full_text, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))