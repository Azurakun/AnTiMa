# cogs/ticket_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import uuid

class CloseTicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.channel
        guild = interaction.guild
        user_name = channel.name.replace('ticket-', '')
        
        # Simple permission check (can be improved, but relies on name match or admin)
        if not (interaction.user.guild_permissions.manage_channels or user_name in interaction.user.name.lower()):
            await interaction.response.send_message("You don't have permission to close this ticket.", ephemeral=True)
            return

        await interaction.response.send_message("Archiving ticket...", ephemeral=True)
        
        archive_category = discord.utils.get(guild.categories, name="Ticket Archives")
        owner_role = guild.get_role(1171743624241872936)
        
        # Set up permissions for the archive
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        
        if owner_role:
            overwrites[owner_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            
        if not archive_category:
            archive_category = await guild.create_category("Ticket Archives", overwrites=overwrites)
            
        try:
            # Generate a short unique ID for identification
            short_id = uuid.uuid4().hex[:6]
            
            # Sync permissions with category removes the ticket creator's access
            await channel.edit(
                name=f"closed-{user_name}-{short_id}",
                category=archive_category,
                sync_permissions=True,
                reason=f"Ticket closed by {interaction.user}"
            )
            await channel.send(f"🔒 Ticket has been closed and archived by {interaction.user.mention}.")
            self.disabled = True
            await interaction.message.edit(view=self.view)
        except Exception as e:
            await interaction.followup.send(f"Failed to archive: {e}", ephemeral=True)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseTicketButton())


class TicketButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket_btn")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        
        existing_channel = discord.utils.get(guild.text_channels, name=f"ticket-{user.name.lower()}")
        if existing_channel:
            await interaction.followup.send(f"You already have a ticket open: {existing_channel.mention}", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        
        for role in guild.roles:
            if role.permissions.administrator or role.permissions.manage_guild:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        category = interaction.channel.category
        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{user.name}",
            category=category,
            overwrites=overwrites,
            reason="User requested a ticket"
        )
        
        close_view = CloseTicketView()
        
        has_indo = any(role.id == 1369320870509674547 for role in user.roles)
        has_overseas = any(role.id == 1368603436660166728 for role in user.roles)
        owner_role = guild.get_role(1171743624241872936)
        owner_mention = owner_role.mention if owner_role else ""
        
        if has_indo and not has_overseas:
            welcome_msg = f"Selamat datang {user.mention}! Ada yang bisa kami bantu hari ini? Mohon jelaskan secara rinci.\n{owner_mention}"
        else:
            welcome_msg = f"Welcome {user.mention}! How can we help you today? Please explain in detail.\n{owner_mention}"
        
        await ticket_channel.send(welcome_msg, view=close_view)
        await interaction.followup.send(f"Ticket created: {ticket_channel.mention}", ephemeral=True)


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketButton())


class TicketCog(commands.Cog, name="Ticket"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ticket_group = app_commands.Group(name="ticket", description="🎫 Ticket System Tools")

    @ticket_group.command(name="setup", description="[Admin] Set up the ticket system panel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ticket_setup(self, interaction: discord.Interaction):
        view = TicketView()
        embed = discord.Embed(
            title="Support Tickets",
            description="Click the button below to open a ticket and contact support.",
            color=discord.Color.blue()
        )
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Ticket panel set up successfully.", ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(TicketView())
        self.bot.add_view(CloseTicketView())

async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
