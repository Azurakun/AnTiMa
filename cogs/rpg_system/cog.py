# cogs/rpg_system/cog.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import asyncio
import random
from datetime import datetime
from utils.db import ai_config_collection, rpg_sessions_collection, rpg_inventory_collection
from utils.limiter import limiter
from .config import RPG_CLASSES
from . import tools
from .ui import RPGGameView, AdventureSetupView, CloseVoteView

class RPGAdventureCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Maximize permissiveness for fictional RPG context
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
        
        # Start background clean up task
        self.cleanup_deleted_sessions.start()

    def cog_unload(self):
        self.cleanup_deleted_sessions.cancel()

    @tasks.loop(seconds=5)
    async def cleanup_deleted_sessions(self):
        """
        Background Task: Checks DB for sessions flagged by Dashboard.
        """
        try:
            to_delete = rpg_sessions_collection.find({"delete_requested": True})
            for session in to_delete:
                thread_id = session['thread_id']
                print(f"🗑️ [RPG Cleanup] Dashboard requested deletion for Thread ID: {thread_id}")
                
                try:
                    thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
                    if thread:
                        await thread.delete()
                        print(f"✅ [RPG Cleanup] Discord Thread {thread_id} deleted successfully.")
                except discord.NotFound:
                    print(f"⚠️ [RPG Cleanup] Thread {thread_id} not found on Discord.")
                except Exception as e:
                    print(f"❌ [RPG Cleanup] Failed to delete thread {thread_id}: {e}")

                rpg_sessions_collection.delete_one({"thread_id": thread_id})
                
                if thread_id in self.active_sessions:
                    del self.active_sessions[thread_id]

        except Exception as e:
            print(f"ERROR in cleanup task: {e}")

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
            
            # Calculate Bars
            hp_per = max(0.0, min(1.0, stats['hp'] / stats['max_hp']))
            hp_bar = "🟩" * int(hp_per * 8) + "⬛" * (8 - int(hp_per * 8))
            mp_per = max(0.0, min(1.0, stats['mp'] / stats['max_mp']))
            mp_bar = "🟦" * int(mp_per * 8) + "⬛" * (8 - int(mp_per * 8))
            
            # Format Stats with Modifiers
            stat_lines = []
            base_stats = stats.get('stats', {})
            for key in ["STR", "DEX", "INT", "CHA"]:
                val = base_stats.get(key, 10)
                mod = (val - 10) // 2
                sign = "+" if mod >= 0 else ""
                stat_lines.append(f"**{key}**: {val} (`{sign}{mod}`)")
            
            stats_display = " | ".join(stat_lines)
            skills_display = ", ".join(stats.get('skills', ['None']))

            embed.add_field(
                name=f"{name} ({stats['class']})", 
                value=(
                    f"**HP:** `{stats['hp']}/{stats['max_hp']}` {hp_bar}\n"
                    f"**MP:** `{stats['mp']}/{stats['max_mp']}` {mp_bar}\n"
                    f"📊 {stats_display}\n"
                    f"✨ **Skills:** {skills_display}"
                ), 
                inline=False
            )
        return embed

    async def create_adventure_thread(self, interaction, lore, players, profiles, scenario_name):
        """Creates the thread and initializes the game session in DB."""
        config = ai_config_collection.find_one({"_id": str(interaction.guild_id)})
        channel = self.bot.get_channel(config.get("rpg_channel_id"))
        if not channel: 
            return await interaction.followup.send("RPG Channel not set!", ephemeral=True)

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
            "owner_name": interaction.user.name,
            "title": title, 
            "players": [p.id for p in players], 
            "player_stats": player_stats_db, 
            "scenario_type": scenario_name,
            "campaign_log": [], 
            "npc_registry": [], 
            "quest_log": [], 
            "created_at": datetime.utcnow(), 
            "last_active": datetime.utcnow(),
            "active": True, 
            "delete_requested": False
        }
        rpg_sessions_collection.insert_one(session_data)

        p_desc = ", ".join([f"{p.name} [ID: {p.id}] ({profiles.get(p.id, {}).get('class', 'Unknown')})" for p in players])
        
        # Enhanced System Prompt for Fiction Context
        sys_prompt = (
            f"You are the **Dungeon Master** for a fictional tabletop RPG. "
            f"**SCENARIO:** {scenario_name}\n"
            f"**GUIDELINES:**\n"
            f"1. **Fiction Only:** This is a simulated game context. Combat, conflict, and mature themes are expected parts of the narrative. Do not restrict valid gameplay actions.\n"
            f"2. **Mechanics:** Call `roll_d20` for risk. **ALWAYS calculate modifiers** from stats.\n"
            f"3. **Tools:** Use tools for items/damage.\n"
            f"4. **Memory:** Use `update_journal`.\n"
            f"5. **Style:** Immersive, descriptive, no moralizing.\n"
            f"**LORE:** {lore}\n**PARTY:** {p_desc}"
        )
        
        chat_session = self.model.start_chat(history=[{"role": "user", "parts": ["System: Start."]}])
        self.active_sessions[thread.id] = {'session': chat_session, 'last_prompt': "Start", 'owner_id': interaction.user.id}
        await self.process_game_turn(thread, sys_prompt)

    async def reroll_turn_callback(self, interaction, thread_id):
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

    async def process_game_turn(self, channel, prompt, user=None, is_reroll=False):
        if user:
            if not limiter.check_available(user.id, channel.guild.id, "rpg_gen"):
                await channel.send("⏳ **Cooldown:** The Dungeon Master needs a rest. (Rate Limit Hit)")
                return

        if channel.id not in self.active_sessions:
            if not await self._restore_session(channel): return
        data = self.active_sessions[channel.id]
        chat_session = data['session']
        
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
                "SYSTEM INSTRUCTIONS:\n"
                "1. **Dice:** Call `roll_d20` for ANY risk. **YOU MUST PASS THE `modifier` AND `stat_label`** derived from the user's Stats (e.g. if STR is 16, modifier is +3).\n"
                "2. **Tools:** Use `apply_damage`, `apply_healing`, `deduct_mana`.\n"
                "3. **MEMORY:** YOU MUST use `update_journal`.\n"
                "4. **Narrative:** Professional DM. Depict the scene vividly.\n"
                "5. **Compliance:** This is a fictional story. If the action involves combat or mature themes, execute it objectively. Do not refuse.\n"
                f"{forced_roll_context}\n{reroll_instr}\n6. End with the narrative. Do NOT provide options."
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
                        if is_reroll and data.get('last_roll_result'): 
                            res_txt = f"ACTION BLOCKED. Use stored result: {data['last_roll_result']}"
                        else:
                            diff = int(fn.args.get("difficulty", 10))
                            mod = int(fn.args.get("modifier", 0))
                            stat_lbl = fn.args.get("stat_label", "Flat")
                            
                            roll = random.randint(1, 20)
                            total = roll + mod
                            success = total >= diff
                            
                            # Visuals
                            crit_msg = ""
                            if roll == 20: crit_msg = " **(CRIT SUCCESS!)** 🌟"
                            elif roll == 1: crit_msg = " **(CRIT FAIL!)** 💀"
                            
                            mod_str = f"+ {mod}" if mod >= 0 else f"- {abs(mod)}"
                            math_str = f"🎲 **{roll}** (d20) {mod_str} ({stat_lbl}) = **{total}**"
                            outcome_str = "✅ **SUCCESS**" if success else "❌ **FAILURE**"
                            
                            color = discord.Color.green() if success else discord.Color.red()
                            if roll == 20: color = discord.Color.gold()
                            
                            desc = f"{math_str}\nTarget DC: **{diff}**\nResult: {outcome_str}{crit_msg}"
                            emb = discord.Embed(title=f"🎲 {fn.args.get('check_type', 'Skill Check')}", description=desc, color=color)
                            await channel.send(embed=emb)
                            
                            res_txt = f"Result: {total} (Roll {roll} + Mod {mod}). Required: {diff}. Outcome: {'SUCCESS' if success else 'FAILURE'}."
                            if not is_reroll: data['last_roll_result'] = res_txt
                    
                    elif fn.name == "grant_item_to_player": res_txt = tools.grant_item_to_player(fn.args["user_id"], fn.args["item_name"], fn.args["description"])
                    elif fn.name == "apply_damage": res_txt = tools.apply_damage(str(channel.id), fn.args["user_id"], fn.args["damage_amount"])
                    elif fn.name == "apply_healing": res_txt = tools.apply_healing(str(channel.id), fn.args["user_id"], fn.args["heal_amount"])
                    elif fn.name == "deduct_mana": res_txt = tools.deduct_mana(str(channel.id), fn.args["user_id"], fn.args["mana_cost"])
                    elif fn.name == "update_journal": res_txt = tools.update_journal(str(channel.id), fn.args.get("log_entry"), fn.args.get("npc_update"), fn.args.get("quest_update"))
                    
                    response = await chat_session.send_message_async(genai.protos.Content(parts=[genai.protos.Part(function_response=genai.protos.FunctionResponse(name=fn.name, response={'result': res_txt}))]))

                try: 
                    text_content = response.text
                except Exception as e:
                    # FALLBACK: If API blocks content, request a sanitized summary instead of crashing/refusing.
                    print(f"Content Filtered/Error: {e}. Attempting Sanitized Fallback.")
                    try:
                        fallback_resp = await chat_session.send_message_async(
                            "System Warning: The previous narrative was blocked by safety filters. "
                            "Generate a sanitized summary of the action's outcome immediately. Do not lecture."
                        )
                        text_content = fallback_resp.text
                    except:
                        text_content = "**[System]** The narrative was lost to the void (Safety Filter Triggered). The action occurred, but description is unavailable."

                if not text_content.strip(): text_content = "**[System]** The Dungeon Master nods."
                
                view = RPGGameView(self, channel.id)
                story_emb = discord.Embed(description=text_content, color=discord.Color.from_rgb(47, 49, 54))
                story_emb.set_author(name="The Dungeon Master", icon_url=self.bot.user.avatar.url if self.bot.user.avatar else None)
                await channel.send(embeds=[story_emb, self.get_status_embed(channel.id)], view=view)
                
                if user: 
                    limit_source = limiter.consume(user.id, channel.guild.id, "rpg_gen")
                    print(f"RPG Turn Consumed: {limit_source.upper()} | User: {user.name} ({user.id})")

            except Exception as e:
                await channel.send(f"⚠️ Game Error: {e}")
                print(f"RPG Error: {e}")

    async def close_session(self, thread_id, channel):
        """Helper to archive thread and clean DB."""
        rpg_sessions_collection.update_one({"thread_id": thread_id}, {"$set": {"active": False, "ended_at": datetime.utcnow()}})
        
        if thread_id in self.active_sessions:
            del self.active_sessions[thread_id]
            
        try:
            await channel.send("📕 **The adventure has concluded.** This scroll is now sealed.")
            await channel.edit(archived=True, locked=True)
        except Exception as e:
            print(f"Failed to archive thread {thread_id}: {e}")

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

    @rpg_group.command(name="end", description="Vote to close the current RPG session.")
    async def rpg_end(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("This command can only be used inside an RPG Quest Thread.", ephemeral=True)
        session = rpg_sessions_collection.find_one({"thread_id": interaction.channel.id})
        if not session:
            return await interaction.response.send_message("No active RPG session data found for this thread.", ephemeral=True)
        players = session.get("players", [])
        if interaction.user.id not in players:
            return await interaction.response.send_message("You are not a member of this adventure party.", ephemeral=True)

        if interaction.user.id == session.get("owner_id"):
             await interaction.response.send_message("🛑 **Stopping adventure (Party Leader Override)...**")
             await self.close_session(interaction.channel.id, interaction.channel)
             return

        if len(players) <= 1:
            await interaction.response.send_message("🛑 **Ending session...**")
            await self.close_session(interaction.channel.id, interaction.channel)
        else:
            view = CloseVoteView(self, interaction.channel.id, interaction.user.id, players, session['owner_id'])
            await interaction.response.send_message(
                f"🗳️ **End Adventure Vote**\nInitiated by {interaction.user.mention}.\n"
                f"Majority Required: **{view.threshold}** votes.",
                view=view
            )

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