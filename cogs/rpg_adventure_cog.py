# cogs/rpg_adventure_cog.py
import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import re
import asyncio
import random
from datetime import datetime
from utils.db import ai_config_collection, rpg_sessions_collection, rpg_inventory_collection

# --- CONFIGURATION ---

RPG_CLASSES = {
    "Warrior": {"hp": 120, "mp": 20, "stats": {"STR": 16, "DEX": 10, "INT": 8, "CHA": 10}, "skills": ["Greatslash", "Taunt"]},
    "Mage": {"hp": 60, "mp": 100, "stats": {"STR": 6, "DEX": 12, "INT": 18, "CHA": 10}, "skills": ["Fireball", "Teleport"]},
    "Rogue": {"hp": 80, "mp": 50, "stats": {"STR": 10, "DEX": 18, "INT": 12, "CHA": 14}, "skills": ["Backstab", "Stealth"]},
    "Cleric": {"hp": 90, "mp": 80, "stats": {"STR": 12, "DEX": 8, "INT": 14, "CHA": 16}, "skills": ["Heal", "Smite"]},
    "Student": {"hp": 100, "mp": 100, "stats": {"STR": 10, "DEX": 10, "INT": 12, "CHA": 16}, "skills": ["Persuade", "Study", "Drama"]}, 
    "Detective": {"hp": 90, "mp": 70, "stats": {"STR": 10, "DEX": 12, "INT": 16, "CHA": 12}, "skills": ["Investigate", "Deduce", "Shoot"]},
    "Freelancer": {"hp": 100, "mp": 50, "stats": {"STR": 12, "DEX": 12, "INT": 12, "CHA": 10}, "skills": ["Improvise", "Run"]}
}

SCENARIOS = [
    {"label": "The Cyber-Dungeon", "value": "Cyberpunk Fantasy", "desc": "Hackers & Dragons in a neon city.", "genre": "[Sci-Fi]"},
    {"label": "The Haunted High School", "value": "High School Horror", "desc": "Survive the ghosts of the old building.", "genre": "[Horror]"},
    {"label": "Isekai Trash", "value": "Generic Isekai", "desc": "Hit by a truck, now you're a hero.", "genre": "[Comedy]"},
    {"label": "Sakura Academy", "value": "Slice of Life", "desc": "You are the only male student in an elite all-girls school.", "genre": "[Romance]"}
]

# --- AI TOOLS ---
def grant_item_to_player(user_id: str, item_name: str, description: str):
    try:
        rpg_inventory_collection.update_one(
            {"user_id": int(user_id)},
            {"$push": {"items": {"name": item_name, "desc": description, "obtained_at": datetime.now()}}},
            upsert=True
        )
        return f"System: Added {item_name} to player {user_id}'s inventory."
    except Exception as e: return f"System Error: {e}"

def update_player_stats(thread_id: str, user_id: str, hp_change: int, mp_change: int):
    try:
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session: return "Error: Session not found."
        uid = str(user_id)
        if uid not in session["player_stats"]: return f"Error: Player {uid} not found."
        stats = session["player_stats"][uid]
        old_hp = stats["hp"]
        stats["hp"] = max(0, min(stats["max_hp"], stats["hp"] + int(hp_change)))
        stats["mp"] = max(0, min(stats["max_mp"], stats["mp"] + int(mp_change)))
        rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, {"$set": {f"player_stats.{uid}": stats}})
        return f"System: Player {uid} HP {old_hp}->{stats['hp']}, MP change {mp_change}."
    except Exception as e: return f"System Error: {e}"

def apply_damage(thread_id: str, user_id: str, damage_amount: int):
    return update_player_stats(thread_id, user_id, hp_change=-int(damage_amount), mp_change=0)

def apply_healing(thread_id: str, user_id: str, heal_amount: int):
    return update_player_stats(thread_id, user_id, hp_change=int(heal_amount), mp_change=0)

def deduct_mana(thread_id: str, user_id: str, mana_cost: int):
    return update_player_stats(thread_id, user_id, hp_change=0, mp_change=-int(mana_cost))

def roll_d20(check_type: str, difficulty: int):
    return random.randint(1, 20) 

def update_journal(thread_id: str, log_entry: str, npc_update: str = None, quest_update: str = None):
    try:
        updates = {}
        if log_entry:
            entry = f"[{datetime.now().strftime('%H:%M')}] {log_entry}"
            updates["$push"] = {"campaign_log": entry}
        if npc_update:
            if "$push" not in updates: updates["$push"] = {}
            updates["$push"]["npc_registry"] = npc_update
        if quest_update:
            if "$push" not in updates: updates["$push"] = {}
            updates["$push"]["quest_log"] = quest_update
        if updates:
            rpg_sessions_collection.update_one({"thread_id": int(thread_id)}, updates)
            return "System: Journal updated."
        return "System: No updates."
    except Exception as e: return f"System Error: {e}"

# --- UI COMPONENTS ---

class RPGGameView(discord.ui.View):
    def __init__(self, cog, thread_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.thread_id = thread_id

    @discord.ui.button(label="🎲 Reroll Story", style=discord.ButtonStyle.danger, row=0)
    async def reroll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable button immediately to prevent spam
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await self.reroll_callback(interaction)

    async def reroll_callback(self, interaction: discord.Interaction):
        session_db = rpg_sessions_collection.find_one({"thread_id": self.thread_id})
        if not session_db or interaction.user.id != session_db['owner_id']:
             return await interaction.followup.send("Only the leader can reroll!", ephemeral=True)
        
        # We don't use defer here because we edited the message above
        await self.cog.reroll_turn(self.thread_id, interaction.channel)

# --- CREATION SYSTEM ---

class CustomCharModal(discord.ui.Modal, title="Design Your Character"):
    char_name = discord.ui.TextInput(label="Character Name / Class", placeholder="e.g. Cyber-Samurai, Idol, Detective", max_length=30)
    char_desc = discord.ui.TextInput(label="Backstory / Vibe", style=discord.TextStyle.paragraph, placeholder="Looking for adventure...", max_length=200)
    
    def __init__(self, parent_view, archetype):
        super().__init__()
        self.parent_view = parent_view
        self.archetype = archetype 

    async def on_submit(self, interaction: discord.Interaction):
        base_stats = {"STR": 12, "DEX": 12, "INT": 12, "CHA": 12}
        hp, mp = 100, 50

        if "STR" in self.archetype: 
            base_stats = {"STR": 16, "DEX": 12, "INT": 8, "CHA": 12}
            hp, mp = 120, 20
        elif "DEX" in self.archetype: 
            base_stats = {"STR": 10, "DEX": 16, "INT": 12, "CHA": 10}
            hp, mp = 90, 60
        elif "INT" in self.archetype: 
            base_stats = {"STR": 8, "DEX": 12, "INT": 16, "CHA": 12}
            hp, mp = 60, 100
        elif "CHA" in self.archetype: 
            base_stats = {"STR": 10, "DEX": 10, "INT": 12, "CHA": 16}
            hp, mp = 100, 80
        
        custom_data = {
            "class": self.char_name.value,
            "hp": hp, "max_hp": hp, "mp": mp, "max_mp": mp,
            "stats": base_stats,
            "skills": ["Custom Action", "Improvise"]
        }
        self.parent_view.selected_profiles[interaction.user.id] = custom_data
        await interaction.response.defer()
        await self.parent_view.update_view_message(interaction)

class ArchetypeSelect(discord.ui.Select):
    def __init__(self, parent_view):
        options = [
            discord.SelectOption(label="Warrior Build", description="Strong & Tough (High STR)", value="Warrior (STR)", emoji="⚔️"),
            discord.SelectOption(label="Rogue Build", description="Fast & Sneaky (High DEX)", value="Rogue (DEX)", emoji="🗡️"),
            discord.SelectOption(label="Mage Build", description="Smart & Magical (High INT)", value="Mage (INT)", emoji="🔮"),
            discord.SelectOption(label="Leader Build", description="Charming & Social (High CHA)", value="Leader (CHA)", emoji="👑"),
            discord.SelectOption(label="Balanced Build", description="Jack of all trades", value="Balanced", emoji="⚖️")
        ]
        super().__init__(placeholder="Select Base Stats...", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CustomCharModal(self.parent_view, self.values[0]))

class ArchetypeView(discord.ui.View):
    def __init__(self, parent_view):
        super().__init__(timeout=60)
        self.add_item(ArchetypeSelect(parent_view))

# --- MAIN SETUP VIEW ---

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
        self.players = [author]
        self.selected_profiles = {} 
        self.message = None

    def _get_party_embed(self):
        embed = discord.Embed(title="⚔️ Adventure Setup Lobby", color=discord.Color.dark_red())
        s_title = self.selected_scenario_name or "Not Set"
        s_desc = (self.selected_lore[:80] + "...") if self.selected_lore else "Waiting for Host..."
        embed.add_field(name=f"🌍 Scenario: {s_title}", value=s_desc, inline=False)
        party_desc = []
        all_ready = True
        for p in self.players:
            profile = self.selected_profiles.get(p.id)
            if profile:
                role = profile['class']
                status = f"**{role}** ✅"
            else:
                status = "⏳ Choosing..."
                all_ready = False
            party_desc.append(f"{p.mention}: {status}")
        embed.add_field(name="👥 Party Members", value="\n".join(party_desc), inline=False)
        self.start_btn.disabled = (self.selected_lore is None) or (not all_ready)
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

    @discord.ui.select(placeholder="Player: Choose Premade Class", row=2, options=[
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

    @discord.ui.button(label="📝 Create Custom OC", style=discord.ButtonStyle.primary, row=3)
    async def create_char_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.players: return await interaction.response.send_message("Not in party.", ephemeral=True)
        await interaction.response.send_message("Select your Stat Archetype:", view=ArchetypeView(self), ephemeral=True)

    @discord.ui.button(label="Host: Custom Lore", style=discord.ButtonStyle.secondary, row=3)
    async def custom_lore(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return await interaction.response.send_message("Host only.", ephemeral=True)
        await interaction.response.send_modal(LoreModal(self))

    @discord.ui.button(label="🚀 START", style=discord.ButtonStyle.success, row=3, disabled=True)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return
        await interaction.response.defer()
        self.stop()
        await self.bot.get_cog("RPGAdventureCog").create_adventure_thread(
            interaction, self.selected_lore, self.players, self.selected_profiles, self.selected_scenario_name
        )

# --- MAIN COG ---

class RPGAdventureCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        try:
            self.model = genai.GenerativeModel(
                'gemini-2.5-pro',
                tools=[grant_item_to_player, apply_damage, apply_healing, deduct_mana, roll_d20, update_journal],
                safety_settings=safety_settings
            )
            self.active_sessions = {} 
        except Exception as e:
            print(f"Failed to load Gemini for RPG: {e}")

    async def _restore_session(self, channel):
        session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
        if not session_db: return None
        history_msgs = []
        try:
            async for msg in channel.history(limit=10):
                if not msg.author.bot or (msg.author == self.bot.user and "🎲" not in msg.content):
                    history_msgs.append(f"{msg.author.name}: {msg.content}")
        except: pass
        history_msgs.reverse()
        chat_context = "\n".join(history_msgs)
        chat_session = self.model.start_chat(history=[{"role": "user", "parts": ["System: Restore Game."]}])
        stats_context = str(session_db.get("player_stats", {}))
        prime_prompt = f"SYSTEM: RESTORING SESSION. STATS: {stats_context}. CONTEXT: {chat_context}"
        try: await chat_session.send_message_async(prime_prompt)
        except: pass
        self.active_sessions[channel.id] = {'session': chat_session, 'owner_id': session_db['owner_id'], 'last_prompt': "Resume"}
        return True

    def get_status_embed(self, thread_id):
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session: return None
        embed = discord.Embed(title="📊 Party Status", color=discord.Color.dark_grey())
        is_slice_of_life = session.get("scenario_type") == "Slice of Life"
        hp_label = "Composure" if is_slice_of_life else "HP"
        mp_label = "Social Battery" if is_slice_of_life else "MP"
        for uid, stats in session["player_stats"].items():
            user = self.bot.get_user(int(uid))
            name = user.display_name if user else f"Player {uid}"
            max_hp = max(1, stats['max_hp'])
            hp_per = max(0.0, min(1.0, stats['hp'] / max_hp))
            hp_bar = "🟩" * int(hp_per * 8) + "⬛" * (8 - int(hp_per * 8))
            max_mp = max(1, stats['max_mp'])
            mp_per = max(0.0, min(1.0, stats['mp'] / max_mp))
            mp_bar = "🟦" * int(mp_per * 8) + "⬛" * (8 - int(mp_per * 8))
            s = stats['stats']
            def mod(score): return (score - 10) // 2
            embed.add_field(
                name=f"{name} ({stats['class']})",
                value=(
                    f"**{hp_label}:** `{stats['hp']}` {hp_bar}\n"
                    f"**{mp_label}:** `{stats['mp']}` {mp_bar}\n"
                    f"STR{mod(s['STR']):+d} DEX{mod(s['DEX']):+d} INT{mod(s['INT']):+d} CHA{mod(s['CHA']):+d}"
                ),
                inline=False
            )
        return embed

    async def process_game_turn(self, channel, prompt, user=None, is_reroll=False):
        if channel.id not in self.active_sessions:
            if not await self._restore_session(channel): return
        data = self.active_sessions[channel.id]
        chat_session = data['session']
        
        forced_roll_context = ""
        if not is_reroll:
            try: data['history_snapshot'] = list(chat_session.history)
            except: pass
            data['last_prompt'] = prompt
            data['last_roll_result'] = None
        else:
            if data.get('last_roll_result'):
                forced_roll_context = f"\n**LOCKED FATE:** Dice Result: {data['last_roll_result']}. DO NOT ROLL AGAIN."

        async with channel.typing():
            session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
            stats_context = str(session_db.get("player_stats", {}))
            campaign_log = "\n".join(session_db.get("campaign_log", [])[-10:])
            reroll_instr = "IMPORTANT: Reroll requested. Generate a DIFFERENT outcome." if is_reroll else ""
            
            full_prompt = (
                f"User Action: {prompt}\n"
                f"[WORLD STATE]:\nSTORY: {campaign_log}\nSTATS: {stats_context}\n"
                "SYSTEM INSTRUCTIONS:\n"
                "1. **Dice:** Call `roll_d20` for ANY risk.\n"
                "2. **Tools:** Use `apply_damage`, `apply_healing`, `deduct_mana`.\n"
                "3. **Narrative:** Professional DM. Vivid, atmospheric. **Bold** enemies.\n"
                f"{forced_roll_context}\n"
                f"{reroll_instr}\n"
                "4. End with narrative. NO OPTIONS."
            )
            
            try:
                response = await chat_session.send_message_async(full_prompt)
                turns = 0
                text_content = ""
                
                # --- FIX: ROBUST TOOL LOOP ---
                while response.parts and response.parts[0].function_call and turns < 6:
                    turns += 1
                    fn = response.parts[0].function_call
                    print(f"RPG Tool: {fn.name}")
                    res_txt = "Error"
                    
                    if fn.name == "roll_d20":
                        if is_reroll and data.get('last_roll_result'):
                            res_txt = f"ACTION BLOCKED. Use stored result: {data['last_roll_result']}"
                        else:
                            diff = int(fn.args.get("difficulty", 10))
                            roll = random.randint(1, 20)
                            success = roll >= diff
                            flavor = f"Rolled {roll} vs DC {diff}" + (" (CRIT!)" if roll==20 else " (FAIL!)" if roll==1 else "")
                            emb = discord.Embed(title=f"🎲 {fn.args.get('check_type', 'Check')}", description=f"**{flavor}**", color=discord.Color.green() if success else discord.Color.red())
                            await channel.send(embed=emb)
                            res_txt = f"{flavor}. Outcome: {'SUCCESS' if success else 'FAILURE'}."
                            if not is_reroll: data['last_roll_result'] = res_txt

                    elif fn.name == "grant_item_to_player":
                        res_txt = grant_item_to_player(fn.args["user_id"], fn.args["item_name"], fn.args["description"])
                    elif fn.name == "apply_damage":
                        res_txt = apply_damage(str(channel.id), fn.args["user_id"], fn.args["damage_amount"])
                    elif fn.name == "apply_healing":
                        res_txt = apply_healing(str(channel.id), fn.args["user_id"], fn.args["heal_amount"])
                    elif fn.name == "deduct_mana":
                        res_txt = deduct_mana(str(channel.id), fn.args["user_id"], fn.args["mana_cost"])
                    elif fn.name == "update_journal":
                        res_txt = update_journal(str(channel.id), fn.args.get("log_entry"), fn.args.get("npc_update"), fn.args.get("quest_update"))
                    
                    # Feed result back to model
                    response = await chat_session.send_message_async(
                        genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn.name, response={'result': res_txt}))])
                    )

                # --- FIX: FORCE TEXT GENERATION ---
                try: 
                    text_content = response.text
                except ValueError:
                    # If response was purely functional or blocked, check history or force generation
                    print("RPG: No text in response. Forcing narrative...")
                    try:
                        # Ask specifically for the story part now that tools are done
                        final_resp = await chat_session.send_message_async("System: Tools execution finished. Now generate the narrative description of the outcome.")
                        text_content = final_resp.text
                    except ValueError:
                        # Absolute fallback if even that fails
                        text_content = "**[System]** The action was processed, but the narrative description was filtered. Please check your stats for changes."

                if not text_content.strip():
                     text_content = "**[System]** The Dungeon Master nods, but speaks no words. (Empty response received)."

                view = RPGGameView(self, channel.id)
                story_emb = discord.Embed(description=text_content, color=discord.Color.from_rgb(47, 49, 54))
                story_emb.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url if self.bot.user.avatar else None)
                await channel.send(embeds=[story_emb, self.get_status_embed(channel.id)], view=view)
                    
            except Exception as e:
                await channel.send(f"⚠️ Game Error: {e}")
                print(f"RPG Error: {e}")

    async def reroll_turn(self, thread_id, channel):
        if thread_id not in self.active_sessions: return
        data = self.active_sessions[thread_id]
        if 'history_snapshot' in data: data['session'].history = list(data['history_snapshot'])
        else:
            try: data['session'].rewind()
            except: pass
        await channel.send("🎲 **Rewinding Time...**")
        await self.process_game_turn(channel, data.get('last_prompt', "Continue"), is_reroll=True)

    @app_commands.command(name="setrpgchannel", description="[Admin] Set RPG channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setrpgchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        ai_config_collection.update_one({"_id": str(interaction.guild_id)}, {"$set": {"rpg_channel_id": channel.id}}, upsert=True)
        await interaction.response.send_message(f"✅ Set to {channel.mention}", ephemeral=True)

    @app_commands.command(name="startadventure", description="Begin a new campaign.")
    async def startadventure(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False) 
        config = ai_config_collection.find_one({"_id": str(interaction.guild_id)})
        if not config or "rpg_channel_id" not in config: return await interaction.followup.send("⚠️ Admin must set channel first.", ephemeral=True)
        view = AdventureSetupView(self.bot, interaction.user)
        view.message = await interaction.followup.send(content=f"⚔️ **RPG Lobby Open!** {interaction.user.mention} is host.", embed=view._get_party_embed(), view=view)

    async def create_adventure_thread(self, interaction, lore, players, profiles, scenario_name):
        config = ai_config_collection.find_one({"_id": str(interaction.guild_id)})
        channel = self.bot.get_channel(config.get("rpg_channel_id"))
        if not channel: return

        try:
            prompt = f"Generate a unique 5-word title for an RPG adventure. Scenario: {scenario_name}. Lore: {lore[:100]}."
            resp = await self.model.generate_content_async(prompt)
            title = resp.text.strip().replace('"', '')[:50]
        except: title = f"Quest: {interaction.user.name}"

        thread = await channel.create_thread(name=title, type=discord.ChannelType.private_thread, auto_archive_duration=10080)
        for p in players: await thread.add_user(p)

        player_stats_db = {}
        for p in players:
            data = profiles.get(p.id, RPG_CLASSES["Freelancer"])
            player_stats_db[str(p.id)] = data

        session_data = {
            "thread_id": thread.id,
            "guild_id": interaction.guild_id,
            "owner_id": interaction.user.id,
            "players": [p.id for p in players],
            "player_stats": player_stats_db,
            "scenario_type": scenario_name,
            "campaign_log": [], # Init empty memory
            "npc_registry": [],
            "quest_log": []
        }
        rpg_sessions_collection.insert_one(session_data)

        p_desc = ", ".join([f"{p.name} [ID: {p.id}] ({profiles.get(p.id, {}).get('class', 'Unknown')})" for p in players])
        
        sys_prompt = (
            "You are the **Dungeon Master**. Professional, immersive, neutral tone.\n"
            f"**SCENARIO:** {scenario_name}\n"
            "**GUIDELINES:**\n"
            "1. **Universal Dice Logic:** Check for failure in ALL contexts. If ANY risk exists, Call `roll_d20(type, difficulty)`.\n"
            "2. **Tools:** Use `apply_damage`, `apply_healing`, `deduct_mana`.\n"
            "3. **MEMORY:** YOU MUST use `update_journal` to save NPCs, Quests, and Plot Points permanently.\n"
            "4. **Format:** Vivid, atmospheric, PG-13. **Bold** enemies.\n"
            "5. End with the narrative. Do NOT provide options."
        )

        chat_session = self.model.start_chat(history=[{"role": "user", "parts": ["System: Start."]}])
        self.active_sessions[thread.id] = {'session': chat_session, 'last_prompt': "Start", 'owner_id': interaction.user.id}
        await self.process_game_turn(thread, sys_prompt)

    @app_commands.command(name="inventory", description="Check RPG inventory.")
    async def inventory(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        data = rpg_inventory_collection.find_one({"user_id": target.id})
        embed = discord.Embed(title=f"🎒 {target.display_name}", color=discord.Color.gold())
        if data and data.get("items"):
            for item in data["items"]: embed.add_field(name=item['name'], value=item['desc'], inline=False)
        else: embed.description = "Empty."
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not isinstance(message.channel, discord.Thread): return
        session_exists = rpg_sessions_collection.find_one({"thread_id": message.channel.id})
        if session_exists:
            if message.author.id not in session_exists.get("players", []): return
            prompt = f"{message.author.name}: {message.content}"
            await self.process_game_turn(message.channel, prompt, message.author)

async def setup(bot: commands.Bot):
    await bot.add_cog(RPGAdventureCog(bot))