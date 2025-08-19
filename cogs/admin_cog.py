import discord
from discord import app_commands
from discord.ext import commands
import logging

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
        # We'll need a way to share the emoji_role_map. For now, let's assume the other cog can access it.
        # A better way would be to use a database, but for now, we find the cog.
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
        
        # You would ideally save this to a file or database here so it persists restarts.
        
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

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))