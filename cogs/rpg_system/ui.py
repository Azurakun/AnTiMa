# cogs/rpg_system/ui.py
from __future__ import annotations
import discord
import functools
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from utils.db import rpg_sessions_collection, rpg_web_tokens_collection

if TYPE_CHECKING:
    from .engine import RPGEngine

# --- STANDARD GAME UI (FALLBACK) ---
class RPGGameView(discord.ui.View):
    def __init__(self, cog, thread_id: int):
        super().__init__(timeout=None) # Persists indefinitely until replaced
        self.cog = cog
        self.thread_id = thread_id

    @discord.ui.button(label="Reroll Turn", style=discord.ButtonStyle.secondary, emoji="🎲", custom_id="rpg_reroll_btn")
    async def reroll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # The logic for rerolling is handled back in the main cog.py to keep UI clean
        await self.cog.reroll_turn_callback(interaction, self.thread_id)


# --- DYNAMIC ACTION UI ---
class DynamicActionView(discord.ui.View):
    def __init__(self, engine: RPGEngine, channel: discord.Thread, user: discord.Member, actions: list[str]):
        super().__init__(timeout=300)  # 5 minute timeout for the buttons
        self.engine = engine
        self.channel = channel
        self.user = user
        self.message = None

        # Dynamically create a button for each action proposed by the AI
        for action_text in actions:
            label = action_text if len(action_text) <= 80 else action_text[:77] + "..."
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"rpg_action_{action_text[:30]}")
            
            # Use a partial to "freeze" the action_text for the callback
            button.callback = functools.partial(self.action_callback, action=action_text)
            self.add_item(button)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        await interaction.response.defer()

        # Disable buttons on the original message to prevent double-clicks
        for item in self.children:
            item.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.NotFound:
            pass
        
        self.stop()

        # The user's choice is posted as a new message for clarity in the chat log
        await self.channel.send(f"**{interaction.user.display_name}** chose to: *{action}*")

        # Re-construct the prompt and send it to the engine
        prompt = f"{interaction.user.name}: {action}"
        await self.engine.process_turn(
            channel=self.channel,
            prompt=prompt,
            user=interaction.user,
            message_id=None
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


# --- ADVENTURE SETUP UI ---
class AdventureSetupView(discord.ui.View):
    def __init__(self, bot, owner: discord.Member):
        super().__init__(timeout=600) # 10 minute timeout for lobby
        self.bot = bot
        self.owner = owner
        self.players = [owner]
        self.message = None

    def _get_party_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚔️ Adventure Lobby", description="Gathering the party...", color=discord.Color.blue())
        players_list = "\n".join([f"- {p.mention}" for p in self.players])
        embed.add_field(name="Current Party Members", value=players_list, inline=False)
        embed.set_footer(text="The Host can start the setup via the Web Dashboard when ready.")
        return embed

    @discord.ui.button(label="Join Party", style=discord.ButtonStyle.blurple, emoji="🙋")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.players:
            self.players.append(interaction.user)
            await interaction.response.edit_message(embed=self._get_party_embed(), view=self)
        else:
            await interaction.response.send_message("You are already in the party!", ephemeral=True)

    @discord.ui.button(label="Start Web Setup", style=discord.ButtonStyle.green, emoji="🌐")
    async def start_web_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner.id:
            return await interaction.response.send_message("⚠️ Only the host can initiate the game setup.", ephemeral=True)
        
        # Generate token and link for the Web Dashboard
        token = str(uuid.uuid4())
        rpg_web_tokens_collection.insert_one({
            "token": token, 
            "user_id": interaction.user.id, 
            "guild_id": interaction.guild_id, 
            "status": "pending", 
            "created_at": datetime.utcnow()
        })
        
        # Hardcoding the URL structure to match your dashboard.py logic
        url = f"http://0.0.0.0:8000/rpg/setup?token={token}"
        
        await interaction.response.send_message(f"**Host:** Click [here]({url}) to configure the adventure and characters!", ephemeral=True)
        
        # Disable buttons once setup begins
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        self.stop()


# --- CLOSE VOTE UI ---
class CloseVoteView(discord.ui.View):
    def __init__(self, cog, thread_id: int, voter_id: int, players: list[int], owner_id: int):
        super().__init__(timeout=86400) # 24 hour timeout for voting
        self.cog = cog
        self.thread_id = thread_id
        self.players = players
        self.owner_id = owner_id
        self.votes = {voter_id}

    def _get_vote_str(self) -> str:
        required_votes = max(1, len(self.players))
        return f"Votes to end: {len(self.votes)} / {required_votes}"

    @discord.ui.button(label="Vote to End Session", style=discord.ButtonStyle.danger, emoji="🛑")
    async def vote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Allow the owner to bypass the vote entirely
        if interaction.user.id == self.owner_id:
            self.votes.add(interaction.user.id)
            await self._execute_closure(interaction)
            return

        if interaction.user.id not in self.players:
            return await interaction.response.send_message("You are not part of this adventure.", ephemeral=True)
            
        self.votes.add(interaction.user.id)
        
        # Check if we have a majority/unanimous vote
        if len(self.votes) >= len(self.players):
            await self._execute_closure(interaction)
        else:
            await interaction.response.edit_message(content=f"Vote to end? ({self._get_vote_str()})", view=self)

    async def _execute_closure(self, interaction: discord.Interaction):
        # Update database state
        rpg_sessions_collection.update_one({"thread_id": self.thread_id}, {"$set": {"active": False}})
        
        # Try to lock and archive the thread
        channel = interaction.guild.get_channel(self.thread_id) or interaction.guild.get_thread(self.thread_id)
        if channel:
            await channel.send("📕 **Adventure Archived by Party Vote (or Game Master mandate).**")
            try:
                await channel.edit(locked=True, archived=True)
            except discord.Forbidden:
                pass # Bot might lack permissions to lock threads in some servers
        
        # Disable the view
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Session closed.", view=self)
        self.stop()