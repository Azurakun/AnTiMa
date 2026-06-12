# cogs/admin_cog.py
import discord
from discord import app_commands
from discord.ext import commands
from utils.db import rpg_world_state_collection
from utils.ai_client import reload_provider, AI_PROVIDER, MAIN_MODEL, FAST_MODEL, _PROVIDERS
import json

async def send_discohook_message(interaction: discord.Interaction, data: dict, target_channel: discord.TextChannel, responded: bool = False):
    async def send_reply(msg):
        if responded:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    try:
        # Recursively remove None values (null in JSON) as discord.py throws errors for None ints (like color)
        def remove_none(obj):
            if isinstance(obj, dict):
                return {k: remove_none(v) for k, v in obj.items() if v is not None}
            elif isinstance(obj, list):
                return [remove_none(v) for v in obj if v is not None]
            return obj
        
        data = remove_none(data)
        
        # Discohook 'Copy JSON' can sometimes wrap in {"messages": [{"data": {...}}]} 
        # or just be the direct message object. We handle both.
        if "messages" in data and isinstance(data["messages"], list):
            if len(data["messages"]) > 0:
                data = data["messages"][0].get("data", {})
                
        content = data.get("content", None)
        if content == "": content = None
        
        embeds_data = data.get("embeds", [])
        embeds = [discord.Embed.from_dict(ed) for ed in embeds_data][:10]
            
        if not content and not embeds:
            return await send_reply("❌ JSON must contain valid 'content' or 'embeds'.")
            
        await target_channel.send(content=content, embeds=embeds)
        await send_reply(f"✅ Successfully sent via Discohook format to {target_channel.mention}")
        
    except Exception as e:
        await send_reply(f"❌ Error sending message: {e}")


class DiscohookModal(discord.ui.Modal, title='Discohook Importer'):
    json_input = discord.ui.TextInput(
        label='Discohook JSON',
        style=discord.TextStyle.paragraph,
        placeholder='Paste the JSON export from Discohook here...',
        required=True,
        max_length=4000
    )

    def __init__(self, target_channel: discord.TextChannel):
        super().__init__()
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            data = json.loads(self.json_input.value)
            await send_discohook_message(interaction, data, self.target_channel, responded=False)
        except json.JSONDecodeError:
            await interaction.response.send_message("❌ Invalid JSON format. Make sure you copied it correctly.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error sending message: {e}", ephemeral=True)

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

    @mod_group.command(name="discohook", description="Send a custom embed using Discohook JSON.")
    @app_commands.describe(channel="Optional: Channel to send the message in", file="Optional: Upload a JSON file if the code exceeds 4000 chars.")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def discohook(self, interaction: discord.Interaction, channel: discord.TextChannel = None, file: discord.Attachment = None):
        target = channel or interaction.channel
        
        if file:
            await interaction.response.defer(ephemeral=True)
            if not file.filename.endswith(('.json', '.txt')):
                return await interaction.followup.send("❌ Please upload a .json or .txt file.", ephemeral=True)
            
            try:
                content = await file.read()
                data = json.loads(content.decode('utf-8'))
                await send_discohook_message(interaction, data, target, responded=True)
            except json.JSONDecodeError:
                await interaction.followup.send("❌ Invalid JSON format in the uploaded file.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ Error reading file: {e}", ephemeral=True)
        else:
            await interaction.response.send_modal(DiscohookModal(target_channel=target))

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

    @mod_group.command(name="global_ai_toggle", description="[Owner] Disable or Enable all AI features globally.")
    @app_commands.choices(action=[app_commands.Choice(name="Disable AI", value="disable"), app_commands.Choice(name="Enable AI", value="enable")])
    async def global_ai_toggle(self, interaction: discord.Interaction, action: str):
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            
        try:
            if action == "disable":
                if 'cogs.ai_chat.cog' in self.bot.extensions:
                    await self.bot.unload_extension('cogs.ai_chat.cog')
                if 'cogs.rpg_system' in self.bot.extensions:
                    await self.bot.unload_extension('cogs.rpg_system')
                await interaction.response.send_message("✅ All AI features have been globally **DISABLED**.", ephemeral=True)
            else:
                if 'cogs.ai_chat.cog' not in self.bot.extensions:
                    await self.bot.load_extension('cogs.ai_chat.cog')
                if 'cogs.rpg_system' not in self.bot.extensions:
                    await self.bot.load_extension('cogs.rpg_system')
                await interaction.response.send_message("✅ All AI features have been globally **ENABLED**.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ Error toggling AI: {e}", ephemeral=True)

    @mod_group.command(name="switch_ai_provider", description="[Owner] Switch AI provider without restarting the bot.")
    @app_commands.choices(provider=[
        app_commands.Choice(name="Gemini (Google Free Tier)", value="gemini"),
        app_commands.Choice(name="Groq (Ultra-Fast Llama)", value="groq"),
        app_commands.Choice(name="OpenRouter (Free Models)", value="openrouter"),
        app_commands.Choice(name="DeepSeek (Generous Free Tier)", value="deepseek"),
        app_commands.Choice(name="Ollama (Local)", value="ollama"),
    ])
    async def switch_ai_provider(self, interaction: discord.Interaction, provider: str):
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)

        try:
            old_provider = AI_PROVIDER
            new_provider = reload_provider(provider)

            embed = discord.Embed(
                title="🔄 AI Provider Switched",
                color=discord.Color.green()
            )
            embed.add_field(name="Previous", value=f"`{old_provider}`", inline=True)
            embed.add_field(name="New", value=f"`{new_provider}`", inline=True)
            embed.add_field(name="Main Model", value=f"`{MAIN_MODEL}`", inline=False)
            embed.add_field(name="Fast Model", value=f"`{FAST_MODEL}`", inline=False)

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to switch provider: {e}", ephemeral=True)

    @mod_group.command(name="ai_status", description="[Owner] Show current AI provider and available providers.")
    async def ai_status(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)

        embed = discord.Embed(title="🤖 AI Provider Status", color=discord.Color.blue())
        embed.add_field(name="Active Provider", value=f"`{AI_PROVIDER}`", inline=False)
        embed.add_field(name="Main Model", value=f"`{MAIN_MODEL}`", inline=False)
        embed.add_field(name="Fast Model", value=f"`{FAST_MODEL}`", inline=False)

        providers_list = "\n".join([
            f"{'✅' if p == AI_PROVIDER else '⬜'} **{p}** — `{c['main_model']}`"
            for p, c in _PROVIDERS.items()
        ])
        embed.add_field(name="Available Providers", value=providers_list, inline=False)

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