import discord
from discord.ext import commands
import logging

logger = logging.getLogger(__name__)

class ReactionRolesCog(commands.Cog, name="ReactionRolesCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Store emoji-role-message mappings per guild.
        # For a real bot, you should save/load this from a database or JSON file.
        self.emoji_role_map = {}

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member.bot:
            return

        guild_id = payload.guild_id
        message_id = payload.message_id
        emoji = str(payload.emoji)

        if guild_id in self.emoji_role_map and message_id in self.emoji_role_map[guild_id]:
            role_id = self.emoji_role_map[guild_id][message_id].get(emoji)
            if role_id:
                guild = self.bot.get_guild(guild_id)
                role = guild.get_role(role_id)
                if payload.member and role:
                    await payload.member.add_roles(role, reason="Reaction Role")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return

        guild_id = payload.guild_id
        message_id = payload.message_id
        emoji = str(payload.emoji)
        
        if guild_id in self.emoji_role_map and message_id in self.emoji_role_map[guild_id]:
            role_id = self.emoji_role_map[guild_id][message_id].get(emoji)
            if role_id:
                guild = self.bot.get_guild(guild_id)
                member = guild.get_member(payload.user_id) # Can't use payload.member here
                role = guild.get_role(role_id)
                if member and role:
                    await member.remove_roles(role, reason="Reaction Role")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRolesCog(bot))