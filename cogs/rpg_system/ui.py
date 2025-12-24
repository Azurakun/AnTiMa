# cogs/rpg_system/ui.py
import discord
from .config import RPG_CLASSES, SCENARIOS

class RPGGameView(discord.ui.View):
    def __init__(self, cog, thread_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.thread_id = thread_id

    @discord.ui.button(label="🎲 Reroll Story", style=discord.ButtonStyle.danger, row=0)
    async def reroll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await self.cog.reroll_turn_callback(interaction, self.thread_id)

class CustomCharModal(discord.ui.Modal, title="Design Your Character"):
    char_name = discord.ui.TextInput(label="Character Name / Class", placeholder="e.g. Cyber-Samurai, Idol", max_length=30)
    char_desc = discord.ui.TextInput(label="Backstory / Vibe", style=discord.TextStyle.paragraph, max_length=200)
    
    def __init__(self, parent_view, archetype):
        super().__init__()
        self.parent_view = parent_view
        self.archetype = archetype 

    async def on_submit(self, interaction: discord.Interaction):
        base_stats = {"STR": 12, "DEX": 12, "INT": 12, "CHA": 12}
        hp, mp = 100, 50

        if "STR" in self.archetype: base_stats, hp, mp = {"STR": 16, "DEX": 12, "INT": 8, "CHA": 12}, 120, 20
        elif "DEX" in self.archetype: base_stats, hp, mp = {"STR": 10, "DEX": 16, "INT": 12, "CHA": 10}, 90, 60
        elif "INT" in self.archetype: base_stats, hp, mp = {"STR": 8, "DEX": 12, "INT": 16, "CHA": 12}, 60, 100
        elif "CHA" in self.archetype: base_stats, hp, mp = {"STR": 10, "DEX": 10, "INT": 12, "CHA": 16}, 100, 80
        
        custom_data = {
            "class": self.char_name.value, "hp": hp, "max_hp": hp, "mp": mp, "max_mp": mp,
            "stats": base_stats, "skills": ["Custom Action", "Improvise"]
        }
        self.parent_view.selected_profiles[interaction.user.id] = custom_data
        await interaction.response.defer()
        await self.parent_view.update_view_message(interaction)

class ArchetypeSelect(discord.ui.Select):
    def __init__(self, parent_view):
        options = [
            discord.SelectOption(label="Warrior Build", description="High STR", value="Warrior (STR)", emoji="⚔️"),
            discord.SelectOption(label="Rogue Build", description="High DEX", value="Rogue (DEX)", emoji="🗡️"),
            discord.SelectOption(label="Mage Build", description="High INT", value="Mage (INT)", emoji="🔮"),
            discord.SelectOption(label="Leader Build", description="High CHA", value="Leader (CHA)", emoji="👑"),
            discord.SelectOption(label="Balanced Build", description="Average Stats", value="Balanced", emoji="⚖️")
        ]
        super().__init__(placeholder="Select Base Stats...", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CustomCharModal(self.parent_view, self.values[0]))

class ArchetypeView(discord.ui.View):
    def __init__(self, parent_view):
        super().__init__(timeout=60)
        self.add_item(ArchetypeSelect(parent_view))

class LoreModal(discord.ui.Modal, title="Write Your Legend"):
    lore_input = discord.ui.TextInput(label="World Setting", style=discord.TextStyle.paragraph, max_length=1000)
    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view
    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.selected_lore = self.lore_input.value
        self.parent_view.selected_scenario_name = "Custom Scenario"
        await interaction.response.defer()
        await self.parent_view.update_view_message(interaction)

class AdventureSetupView(discord.ui.View):
    def __init__(self, bot, author):
        super().__init__(timeout=600)
        self.bot = bot
        self.author = author
        self.selected_lore = None
        self.selected_scenario_name = None
        self.story_mode = False # Default to Standard
        self.players = [author]
        self.selected_profiles = {} 
        self.message = None

    def _get_party_embed(self):
        embed = discord.Embed(title="⚔️ Adventure Setup Lobby", color=discord.Color.dark_red())
        s_title = self.selected_scenario_name or "Not Set"
        s_desc = (self.selected_lore[:80] + "...") if self.selected_lore else "Waiting for Host..."
        embed.add_field(name=f"🌍 Scenario: {s_title}", value=s_desc, inline=False)
        
        mode_str = "📖 Story Mode (No Dice/Stats)" if self.story_mode else "🎲 Standard RPG (Dice + Stats)"
        embed.add_field(name="⚙️ Game Mode", value=mode_str, inline=False)
        
        party_desc = []
        for p in self.players:
            profile = self.selected_profiles.get(p.id)
            status = f"**{profile['class']}** ✅" if profile else "⏳ Choosing..."
            party_desc.append(f"{p.mention}: {status}")
        embed.add_field(name="👥 Party Members", value="\n".join(party_desc), inline=False)
        
        self.start_btn.disabled = (self.selected_lore is None)
        return embed

    async def update_view_message(self, interaction):
        embed = self._get_party_embed()
        try: await self.message.edit(embed=embed, view=self)
        except: pass

    @discord.ui.select(placeholder="Host: Select Scenario", row=0, options=[
        discord.SelectOption(label=s['label'], value=str(i), description=f"{s['genre']} {s['desc']}") for i, s in enumerate(SCENARIOS)
    ])
    async def select_scenario(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user != self.author: return await interaction.response.send_message("Host only.", ephemeral=True)
        idx = int(select.values[0])
        self.selected_lore = SCENARIOS[idx]['desc']
        self.selected_scenario_name = SCENARIOS[idx]['label']
        await interaction.response.defer()
        await self.update_view_message(interaction)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Host: Invite Players", max_values=3, row=1)
    async def invite_players(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        if interaction.user != self.author: return await interaction.response.send_message("Host only.", ephemeral=True)
        new_players = [self.author]
        for user in select.values:
            if user != self.author and not user.bot: new_players.append(user)
        self.players = new_players
        current_ids = [p.id for p in self.players]
        self.selected_profiles = {k:v for k,v in self.selected_profiles.items() if k in current_ids}
        await interaction.response.defer()
        await self.update_view_message(interaction)

    @discord.ui.select(placeholder="Host: Game Style", row=2, options=[
        discord.SelectOption(label="Standard RPG", value="standard", description="Use Dice, HP, MP, and Stats.", emoji="🎲"),
        discord.SelectOption(label="Story Mode", value="story", description="Narrative only. No math or stats.", emoji="📖")
    ])
    async def select_gamemode(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user != self.author: return await interaction.response.send_message("Host only.", ephemeral=True)
        self.story_mode = True if select.values[0] == "story" else False
        await interaction.response.defer()
        await self.update_view_message(interaction)

    @discord.ui.select(placeholder="Player: Choose Premade Class", row=3, options=[
        discord.SelectOption(label=k, description=f"{v['stats']['STR']} STR, {v['stats']['INT']} INT") for k, v in RPG_CLASSES.items()
    ])
    async def select_premade(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user not in self.players: return await interaction.response.send_message("Not in party.", ephemeral=True)
        class_name = select.values[0]
        base = RPG_CLASSES[class_name]
        data = {
            "class": class_name, "hp": base['hp'], "max_hp": base['hp'], "mp": base['mp'], "max_mp": base['mp'],
            "stats": base['stats'].copy(), "skills": base['skills'].copy()
        }
        self.selected_profiles[interaction.user.id] = data
        await interaction.response.defer()
        await self.update_view_message(interaction)

    @discord.ui.button(label="📝 Create Custom OC", style=discord.ButtonStyle.primary, row=4)
    async def create_char_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.players: return await interaction.response.send_message("Not in party.", ephemeral=True)
        await interaction.response.send_message("Select your Stat Archetype:", view=ArchetypeView(self), ephemeral=True)

    @discord.ui.button(label="Host: Custom Lore", style=discord.ButtonStyle.secondary, row=4)
    async def custom_lore(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return await interaction.response.send_message("Host only.", ephemeral=True)
        await interaction.response.send_modal(LoreModal(self))

    @discord.ui.button(label="🚀 START", style=discord.ButtonStyle.success, row=4, disabled=True)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return
        await interaction.response.defer()
        self.stop()
        await self.bot.get_cog("RPGAdventureCog").create_adventure_thread(
            interaction, self.selected_lore, self.players, self.selected_profiles, self.selected_scenario_name, self.story_mode
        )

# --- VOTING SYSTEM ---
class CloseVoteView(discord.ui.View):
    def __init__(self, cog, thread_id, initiator_id, player_ids, owner_id):
        super().__init__(timeout=300)
        self.cog = cog
        self.thread_id = thread_id
        self.player_ids = player_ids
        self.owner_id = owner_id  
        self.votes = {initiator_id}
        self.threshold = (len(player_ids) // 2) + 1

    @discord.ui.button(label="End Adventure", style=discord.ButtonStyle.danger)
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.player_ids:
            return await interaction.response.send_message("Spectators cannot vote.", ephemeral=True)
        self.votes.add(interaction.user.id)
        if len(self.votes) >= self.threshold:
            await interaction.response.edit_message(content="🔒 **Majority reached.** The adventure is ending...", view=None)
            self.stop()
            await self.cog.close_session(self.thread_id, interaction.channel)
        else:
            await interaction.response.send_message(f"Vote recorded! ({len(self.votes)}/{self.threshold} needed)", ephemeral=True)

    @discord.ui.button(label="Keep Playing", style=discord.ButtonStyle.secondary)
    async def vote_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.player_ids:
            return await interaction.response.send_message("Spectators cannot vote.", ephemeral=True)
        await interaction.response.edit_message(content=f"❌ **Vote Cancelled.** {interaction.user.mention} wants to continue the adventure!", view=None)
        self.stop()

    @discord.ui.button(label="⚠ Force End (Host)", style=discord.ButtonStyle.danger, row=1)
    async def force_end_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Only the Party Leader can force end.", ephemeral=True)
        await interaction.response.edit_message(content="🛑 **Party Leader forced the session to end.**", view=None)
        self.stop()
        await self.cog.close_session(self.thread_id, interaction.channel)