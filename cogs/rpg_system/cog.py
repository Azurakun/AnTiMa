# cogs/rpg_system/cog.py
import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import asyncio
import random
from datetime import datetime
from utils.db import ai_config_collection, rpg_sessions_collection, rpg_inventory_collection
from utils.limiter import limiter
from .config import RPG_CLASSES
from . import tools
from .ui import RPGGameView, AdventureSetupView

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
                tools=[
                    tools.grant_item_to_player, tools.apply_damage, 
                    tools.apply_healing, tools.deduct_mana, 
                    tools.roll_d20, tools.update_journal
                ],
                safety_settings=safety_settings
            )
            self.active_sessions = {} 
        except Exception as e:
            print(f"Failed to load Gemini for RPG: {e}")

    async def _restore_session(self, channel):
        """Restores AI memory from DB Logs + Chat History."""
        session_db = rpg_sessions_collection.find_one({"thread_id": channel.id})
        if not session_db: return None
        history_msgs = []
        try:
            async for msg in channel.history(limit=10):
                if not msg.author.bot or (msg.author == self.bot.user and "🎲" not in msg.content):
                    history_msgs.append(f"{msg.author.name}: {msg.content}")
        except: pass
        history_msgs.reverse()
        
        chat_session = self.model.start_chat(history=[{"role": "user", "parts": ["System: Restore Game."]}])
        prime_prompt = (
            f"SYSTEM: RESTORING SESSION.\n"
            f"STATS: {session_db.get('player_stats', {})}\n"
            f"STORY: {session_db.get('campaign_log', [])[-10:]}\n"
            f"CONTEXT: {' '.join(history_msgs)}\n"
            "Resume story."
        )
        try: await chat_session.send_message_async(prime_prompt)
        except: pass
        self.active_sessions[channel.id] = {'session': chat_session, 'owner_id': session_db['owner_id'], 'last_prompt': "Resume"}
        return True

    def get_status_embed(self, thread_id):
        session = rpg_sessions_collection.find_one({"thread_id": int(thread_id)})
        if not session: return None
        embed = discord.Embed(title="📊 Party Status", color=discord.Color.dark_grey())
        for uid, stats in session["player_stats"].items():
            user = self.bot.get_user(int(uid))
            name = user.display_name if user else f"Player {uid}"
            hp_per = max(0.0, min(1.0, stats['hp'] / stats['max_hp']))
            hp_bar = "🟩" * int(hp_per * 8) + "⬛" * (8 - int(hp_per * 8))
            mp_per = max(0.0, min(1.0, stats['mp'] / stats['max_mp']))
            mp_bar = "🟦" * int(mp_per * 8) + "⬛" * (8 - int(mp_per * 8))
            embed.add_field(name=f"{name} ({stats['class']})", value=f"**HP:** `{stats['hp']}` {hp_bar}\n**MP:** `{stats['mp']}` {mp_bar}", inline=False)
        return embed

    async def reroll_turn_callback(self, interaction, thread_id):
        """Called by the UI button to reroll."""
        session_db = rpg_sessions_collection.find_one({"thread_id": thread_id})
        if not session_db or interaction.user.id != session_db['owner_id']:
             return await interaction.followup.send("Only the party leader can reroll!", ephemeral=True)
        
        if thread_id not in self.active_sessions: return
        data = self.active_sessions[thread_id]
        
        if 'history_snapshot' in data: data['session'].history = list(data['history_snapshot'])
        else:
            try: data['session'].rewind()
            except: pass
            
        await interaction.channel.send("🎲 **Rewinding Time...**")
        await self.process_game_turn(interaction.channel, data.get('last_prompt', "Continue"), is_reroll=True)

    async def reroll_turn(self, thread_id, channel):
        """Internal helper for rerolling."""
        pass

    async def process_game_turn(self, channel, prompt, user=None, is_reroll=False):
        # 1. Rate Limit Check (Peek)
        if user:
            if not limiter.check_available(user.id, channel.guild.id, "rpg_gen"):
                await channel.send("⏳ **Cooldown:** The Dungeon Master needs a rest. (Rate Limit Hit)")
                return

        # 2. Restore Session if needed
        if channel.id not in self.active_sessions:
            if not await self._restore_session(channel): return
        data = self.active_sessions[channel.id]
        chat_session = data['session']
        
        # 3. Update Dashboard Activity
        rpg_sessions_collection.update_one({"thread_id": channel.id}, {"$set": {"last_active": datetime.utcnow()}})
        
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
                f"User Action: {prompt}\n[WORLD STATE]:\nSTORY: {campaign_log}\nSTATS: {stats_context}\n"
                "SYSTEM INSTRUCTIONS:\n1. **Dice:** Call `roll_d20` for ANY risk.\n2. **Tools:** Use `apply_damage`, `apply_healing`, `deduct_mana`.\n"
                "3. **MEMORY:** YOU MUST use `update_journal`.\n4. **Narrative:** Professional DM.\n"
                f"{forced_roll_context}\n{reroll_instr}\n5. End with the narrative. Do NOT provide options."
            )
            
            try:
                response = await chat_session.send_message_async(full_prompt)
                turns = 0
                text_content = ""
                
                while response.parts and response.parts[0].function_call and turns < 6:
                    turns += 1
                    fn = response.parts[0].function_call
                    res_txt = "Error"
                    
                    if fn.name == "roll_d20":
                        if is_reroll and data.get('last_roll_result'): res_txt = f"ACTION BLOCKED. Use stored result: {data['last_roll_result']}"
                        else:
                            diff = int(fn.args.get("difficulty", 10))
                            roll = random.randint(1, 20)
                            success = roll >= diff
                            flavor = f"Rolled {roll} vs DC {diff}" + (" (CRIT!)" if roll==20 else " (FAIL!)" if roll==1 else "")
                            emb = discord.Embed(title=f"🎲 {fn.args.get('check_type', 'Check')}", description=f"**{flavor}**", color=discord.Color.green() if success else discord.Color.red())
                            await channel.send(embed=emb)
                            res_txt = f"{flavor}. Outcome: {'SUCCESS' if success else 'FAILURE'}."
                            if not is_reroll: data['last_roll_result'] = res_txt
                    elif fn.name == "grant_item_to_player": res_txt = tools.grant_item_to_player(fn.args["user_id"], fn.args["item_name"], fn.args["description"])
                    elif fn.name == "apply_damage": res_txt = tools.apply_damage(str(channel.id), fn.args["user_id"], fn.args["damage_amount"])
                    elif fn.name == "apply_healing": res_txt = tools.apply_healing(str(channel.id), fn.args["user_id"], fn.args["heal_amount"])
                    elif fn.name == "deduct_mana": res_txt = tools.deduct_mana(str(channel.id), fn.args["user_id"], fn.args["mana_cost"])
                    elif fn.name == "update_journal": res_txt = tools.update_journal(str(channel.id), fn.args.get("log_entry"), fn.args.get("npc_update"), fn.args.get("quest_update"))
                    
                    response = await chat_session.send_message_async(genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn.name, response={'result': res_txt}))]))

                try: text_content = response.text
                except: 
                    try: text_content = (await chat_session.send_message_async("System: Tools done. Generate narrative.")).text
                    except: text_content = "**[System]** The action was processed, but narrative was filtered."

                if not text_content.strip(): text_content = "**[System]** The Dungeon Master nods."
                
                view = RPGGameView(self, channel.id)
                story_emb = discord.Embed(description=text_content, color=discord.Color.from_rgb(47, 49, 54))
                story_emb.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url if self.bot.user.avatar else None)
                await channel.send(embeds=[story_emb, self.get_status_embed(channel.id)], view=view)
                
                # 4. Consume Limit & Log Detailed Info
                if user: 
                    limit_source = limiter.consume(user.id, channel.guild.id, "rpg_gen")
                    print(f"RPG Turn Consumed: {limit_source.upper()} | User: {user.name} ({user.id}) | Guild: {channel.guild.name} ({channel.guild.id})")

            except Exception as e:
                await channel.send(f"⚠️ Game Error: {e}")
                print(f"RPG Error: {e}")

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
            "thread_id": thread.id, "guild_id": interaction.guild_id, "owner_id": interaction.user.id, "owner_name": interaction.user.name,
            "title": title, "players": [p.id for p in players], "player_stats": player_stats_db, "scenario_type": scenario_name,
            "campaign_log": [], "npc_registry": [], "quest_log": [], "created_at": datetime.utcnow(), "last_active": datetime.utcnow()
        }
        rpg_sessions_collection.insert_one(session_data)

        p_desc = ", ".join([f"{p.name} [ID: {p.id}] ({profiles.get(p.id, {}).get('class', 'Unknown')})" for p in players])
        sys_prompt = f"You are the **Dungeon Master**. Professional tone.\n**SCENARIO:** {scenario_name}\n**GUIDELINES:**\n1. Call `roll_d20` for risk.\n2. Use tools.\n3. Update Journal.\n4. Narrative style.\n**LORE:** {lore}\n**PARTY:** {p_desc}"
        chat_session = self.model.start_chat(history=[{"role": "user", "parts": ["System: Start."]}])
        self.active_sessions[thread.id] = {'session': chat_session, 'last_prompt': "Start", 'owner_id': interaction.user.id}
        await self.process_game_turn(thread, sys_prompt)

    # --- COMMANDS ---
    rpg_group = app_commands.Group(name="rpg", description="⚔️ Play immersive role-playing adventures.")

    @rpg_group.command(name="start", description="Open the RPG Lobby to create a new adventure.")
    async def rpg_start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False) 
        config = ai_config_collection.find_one({"_id": str(interaction.guild_id)})
        if not config or "rpg_channel_id" not in config: 
            return await interaction.followup.send("⚠️ Admin must set channel first using `/config rpg`.", ephemeral=True)
        
        view = AdventureSetupView(self.bot, interaction.user)
        view.message = await interaction.followup.send(content=f"⚔️ **RPG Lobby Open!** {interaction.user.mention} is host.", embed=view._get_party_embed(), view=view)

    @rpg_group.command(name="inventory", description="Check your character's items.")
    async def rpg_inventory(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        data = rpg_inventory_collection.find_one({"user_id": target.id})
        embed = discord.Embed(title=f"🎒 {target.display_name}'s Inventory", color=discord.Color.gold())
        if data and data.get("items"):
            for item in data["items"]: embed.add_field(name=item['name'], value=item['desc'], inline=False)
        else: embed.description = "Your backpack is empty."
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not isinstance(message.channel, discord.Thread): return
        session_exists = rpg_sessions_collection.find_one({"thread_id": message.channel.id})
        if session_exists:
            if message.author.id not in session_exists.get("players", []): return
            prompt = f"{message.author.name}: {message.content}"
            await self.process_game_turn(message.channel, prompt, message.author)